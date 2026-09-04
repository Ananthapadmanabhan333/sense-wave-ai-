"""The single upstream consumer.

Exactly one of these runs per deployment. It owns the only WebSocket to RuView,
validates every frame, enforces the consent rules, decimates to
``SENSEWAVE_INGEST_HZ``, writes to ``readings``, and republishes to the
in-process PubSub that ``/api/ws/live`` fans out from.

Failure policy, in order of preference: drop one frame, drop one connection,
never take the process down. A bad frame is counted and logged with the field
that failed; a dead upstream is retried with capped exponential backoff and
full jitter, and every state change is logged.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import random
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sensewave.config import Settings
from sensewave.ingest.client import AuthenticatedSensingClient
from sensewave.ingest.pubsub import PubSub
from sensewave.logging import get_logger
from sensewave.models import Node, Reading, Room
from sensewave.schemas.upstream import SensingUpdate

log = get_logger(__name__)


@dataclass
class IngestStats:
    """Counters surfaced by /api/health and, in Phase 4, by Prometheus."""

    frames_received: int = 0
    frames_invalid: int = 0
    readings_written: int = 0
    upstream_reconnects: int = 0
    vitals_suppressed: int = 0
    connected: bool = False
    last_frame_at: dt.datetime | None = None
    last_error: str | None = None

    def snapshot(self) -> dict[str, Any]:
        return {
            "frames_received": self.frames_received,
            "frames_invalid": self.frames_invalid,
            "readings_written": self.readings_written,
            "upstream_reconnects": self.upstream_reconnects,
            "vitals_suppressed": self.vitals_suppressed,
            "connected": self.connected,
            "last_frame_at": self.last_frame_at.isoformat() if self.last_frame_at else None,
            "last_error": self.last_error,
        }


@dataclass
class _RoomGate:
    """Cached consent + decimation state for one room."""

    monitoring_enabled: bool = True
    privacy_mode: bool = False
    last_written: float = field(default=0.0)


class IngestWorker:
    def __init__(
        self,
        settings: Settings,
        sessionmaker: async_sessionmaker[AsyncSession],
        pubsub: PubSub,
    ) -> None:
        self._settings = settings
        self._sessionmaker = sessionmaker
        self._pubsub = pubsub
        self.stats = IngestStats()
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()
        # node_id -> room_id, refreshed lazily; unmapped nodes are ignored.
        self._node_room: dict[int, int] = {}
        self._room_gate: dict[int, _RoomGate] = {}
        self._routing_loaded = False

    # --- lifecycle ----------------------------------------------------

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self.run(), name="sensewave-ingest")

    async def stop(self) -> None:
        self._stopping.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def consume_once(self) -> bool:
        """Hold one upstream connection until it closes.

        Returns True if the connection was actually established, which is what
        the retry loop uses to decide whether to reset its backoff. Raises on
        connect failure so the caller can back off.
        """
        url = self._settings.upstream_ws_url
        token = self._settings.resolved_upstream_token()
        connected = False
        log.info("upstream.connecting", url=url)
        try:
            async with AuthenticatedSensingClient(url, token=token) as client:
                connected = True
                self.stats.connected = True
                self.stats.last_error = None
                log.info("upstream.connected", url=url)
                async for raw in client.raw_frames():
                    if self._stopping.is_set():
                        break
                    await self.handle_frame(raw)
        finally:
            if connected:
                log.info("upstream.disconnected", url=url)
            self.stats.connected = False
        return connected

    async def run(self) -> None:
        """Connect/consume/retry until stopped."""
        backoff = self._settings.reconnect_backoff_initial

        while not self._stopping.is_set():
            try:
                if await self.consume_once():
                    # We did reach the server, so the next outage starts its
                    # backoff from scratch rather than inheriting the old ramp.
                    backoff = self._settings.reconnect_backoff_initial
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.stats.last_error = repr(e)
                log.warning("upstream.error", url=self._settings.upstream_ws_url, error=repr(e))

            if self._stopping.is_set():
                break

            self.stats.upstream_reconnects += 1
            # Full jitter: sleep uniformly in [0, backoff]. Prevents a fleet of
            # backends from retrying in lockstep after an upstream restart.
            delay = random.uniform(0, backoff)
            log.info("upstream.reconnect_scheduled", delay_s=round(delay, 3), backoff_s=backoff)
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=delay)
                break  # stop() was called while we waited
            except TimeoutError:
                pass
            backoff = min(backoff * 2, self._settings.reconnect_backoff_max)

        log.info("upstream.stopped")

    # --- frame handling -----------------------------------------------

    async def handle_frame(self, raw: dict[str, Any]) -> None:
        self.stats.frames_received += 1

        try:
            update = SensingUpdate.model_validate(raw)
        except ValidationError as e:
            self.stats.frames_invalid += 1
            first = e.errors()[0]
            # Name the offending field: a rising drop count with a consistent
            # field is how upstream contract drift announces itself.
            log.warning(
                "frame.invalid",
                field=".".join(str(p) for p in first["loc"]),
                reason=first["msg"],
                frames_invalid=self.stats.frames_invalid,
            )
            return

        self.stats.last_frame_at = dt.datetime.now(dt.UTC)

        async with self._sessionmaker() as session:
            await self._ensure_routing(session)
            await self._update_node_health(session, update)

            for room_id in self._rooms_for(update):
                await self._persist_and_publish(session, room_id, update)
            await session.commit()

    def _rooms_for(self, update: SensingUpdate) -> list[int]:
        """Rooms this frame speaks for, via its node ids.

        A node we have never been told about is ignored rather than guessed at
        -- an operator maps it on the Nodes page. Unmapped data is not silently
        attributed to some default room.
        """
        seen: list[int] = []
        node_ids = {n.node_id for n in update.nodes} | {n.node_id for n in update.node_features}
        for nid in sorted(node_ids):
            room_id = self._node_room.get(nid)
            if room_id is not None and room_id not in seen:
                seen.append(room_id)
        return seen

    async def _persist_and_publish(
        self, session: AsyncSession, room_id: int, update: SensingUpdate
    ) -> None:
        gate = self._room_gate.get(room_id, _RoomGate())

        # --- Consent rule 7, enforced here and not in the UI -----------
        if not gate.monitoring_enabled:
            return

        vitals = update.vital_signs
        suppressed = False
        if gate.privacy_mode:
            # Presence and motion survive; vitals never reach the database.
            vitals = None
            suppressed = True
            self.stats.vitals_suppressed += 1

        payload = self._to_payload(room_id, update, vitals, suppressed)

        # Fan out at full upstream rate -- the dashboard wants 10 Hz liveness.
        await self._pubsub.publish(payload)

        # Persist by decimation at SENSEWAVE_INGEST_HZ. We keep a real frame,
        # never an average: rule 2 forbids inventing a value that was never
        # measured.
        now = asyncio.get_running_loop().time()
        if gate.last_written and now - gate.last_written < self._settings.ingest_min_interval:
            return
        gate.last_written = now
        self._room_gate[room_id] = gate

        stmt = pg_insert(Reading).values(
            time=dt.datetime.fromtimestamp(update.timestamp, tz=dt.UTC),
            room_id=room_id,
            presence=update.classification.presence,
            presence_confidence=update.classification.confidence,
            motion_level=update.classification.motion_level,
            motion_band_power=update.features.motion_band_power,
            breathing_band_power=update.features.breathing_band_power,
            mean_rssi=update.features.mean_rssi,
            estimated_persons=update.estimated_persons,
            breathing_rate_bpm=vitals.breathing_rate_bpm if vitals else None,
            breathing_confidence=vitals.breathing_confidence if vitals else None,
            heart_rate_bpm=vitals.heart_rate_bpm if vitals else None,
            heartbeat_confidence=vitals.heartbeat_confidence if vitals else None,
            signal_quality=vitals.signal_quality if vitals else None,
            vitals_suppressed=suppressed,
        )
        # Two frames can share a (time, room) key at upstream resolution; the
        # first one written wins rather than the insert raising.
        stmt = stmt.on_conflict_do_nothing(index_elements=["time", "room_id"])
        await session.execute(stmt)
        self.stats.readings_written += 1

    def _to_payload(
        self,
        room_id: int,
        update: SensingUpdate,
        vitals: Any,
        suppressed: bool,
    ) -> dict[str, Any]:
        """The shape browser clients receive on /api/ws/live.

        Vitals are always emitted as a rate/confidence pair. There is no field
        here that a client could render as a bare number without also having
        been handed the confidence to gate it on.
        """
        return {
            "type": "reading",
            "room_id": room_id,
            "timestamp": update.timestamp,
            "source": update.source,
            "tick": update.tick,
            "presence": update.classification.presence,
            "presence_confidence": update.classification.confidence,
            "motion_level": update.classification.motion_level,
            "motion_band_power": update.features.motion_band_power,
            "mean_rssi": update.features.mean_rssi,
            "estimated_persons": update.estimated_persons,
            "vitals_suppressed": suppressed,
            "vitals": None
            if vitals is None
            else {
                "breathing_rate_bpm": vitals.breathing_rate_bpm,
                "breathing_confidence": vitals.breathing_confidence,
                "heart_rate_bpm": vitals.heart_rate_bpm,
                "heartbeat_confidence": vitals.heartbeat_confidence,
                "signal_quality": vitals.signal_quality,
            },
            "nodes": [
                {
                    "node_id": nf.node_id,
                    "rssi_dbm": nf.rssi_dbm,
                    "frame_rate_hz": nf.frame_rate_hz,
                    "novelty_score": nf.novelty_score,
                    "last_seen_ms": nf.last_seen_ms,
                    "stale": nf.stale,
                }
                for nf in update.node_features
            ],
        }

    # --- routing + node health ----------------------------------------

    async def _ensure_routing(self, session: AsyncSession, force: bool = False) -> None:
        if self._routing_loaded and not force:
            return
        nodes = (await session.execute(select(Node))).scalars().all()
        self._node_room = {n.node_id: n.room_id for n in nodes if n.room_id is not None}
        rooms = (await session.execute(select(Room))).scalars().all()
        for room in rooms:
            gate = self._room_gate.get(room.id, _RoomGate())
            gate.monitoring_enabled = room.monitoring_enabled
            gate.privacy_mode = room.privacy_mode
            self._room_gate[room.id] = gate
        self._routing_loaded = True
        log.info("ingest.routing_loaded", nodes=len(self._node_room), rooms=len(self._room_gate))

    def invalidate_routing(self) -> None:
        """Called when a room's consent flags or a node's mapping changes."""
        self._routing_loaded = False

    async def _update_node_health(self, session: AsyncSession, update: SensingUpdate) -> None:
        """Refresh per-node health from ``node_features``.

        ``last_seen_ms`` and ``stale`` come from the server and we store them as
        given -- product rule 3 makes upstream the source of truth for staleness
        rather than our own arrival clock.
        """
        if not update.node_features:
            return
        now = dt.datetime.now(dt.UTC)
        for nf in update.node_features:
            node = (
                await session.execute(select(Node).where(Node.node_id == nf.node_id))
            ).scalar_one_or_none()
            if node is None:
                # Auto-register so the node shows up on the Nodes page as
                # unassigned. It contributes no readings until an operator maps
                # it to a room -- unless auto_map_room_id is set, which is an
                # explicit operator opt-in for the simulate demo.
                node = Node(
                    node_id=nf.node_id,
                    name=f"node-{nf.node_id}",
                    room_id=self._settings.auto_map_room_id,
                )
                session.add(node)
                if self._settings.auto_map_room_id is not None:
                    await session.flush()
                    self.invalidate_routing()
                    log.info(
                        "node.auto_mapped",
                        node_id=nf.node_id,
                        room_id=self._settings.auto_map_room_id,
                    )
            node.rssi_dbm = nf.rssi_dbm
            node.frame_rate_hz = nf.frame_rate_hz
            node.novelty_score = nf.novelty_score
            node.upstream_stale = nf.stale
            node.last_seen = now
