"""SenseWave's own REST + WebSocket API.

Browser clients connect to ``/api/ws/live`` here, never to RuView directly.
That is what gives us one place for auth, buffering and reconnect policy, and
it is why the ingest worker holds the only upstream socket.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from sensewave.config import Settings, get_settings
from sensewave.db import get_session
from sensewave.logging import get_logger
from sensewave.models import Node, Reading, Room
from sensewave.schemas.api import (
    HealthOut,
    HistoryOut,
    HistoryPoint,
    NodeOut,
    ReadingOut,
    RoomLatestOut,
    RoomOut,
    VitalsOut,
)

log = get_logger(__name__)
router = APIRouter(prefix="/api")

# Metrics exposed by /history. Vitals metrics carry a confidence column so the
# chart can draw a confidence band; the others have none and report null.
_HISTORY_METRICS: dict[str, tuple[Any, Any | None]] = {
    "motion_band_power": (Reading.motion_band_power, None),
    "breathing_band_power": (Reading.breathing_band_power, None),
    "mean_rssi": (Reading.mean_rssi, None),
    "presence_confidence": (Reading.presence_confidence, None),
    "estimated_persons": (Reading.estimated_persons, None),
    "breathing_rate_bpm": (Reading.breathing_rate_bpm, Reading.breathing_confidence),
    "heart_rate_bpm": (Reading.heart_rate_bpm, Reading.heartbeat_confidence),
}


def _vitals_from_reading(reading: Reading, settings: Settings) -> VitalsOut:
    return VitalsOut.build(
        breathing_rate_bpm=reading.breathing_rate_bpm,
        breathing_confidence=reading.breathing_confidence,
        heart_rate_bpm=reading.heart_rate_bpm,
        heartbeat_confidence=reading.heartbeat_confidence,
        signal_quality=reading.signal_quality,
        suppressed=reading.vitals_suppressed,
        min_confidence=settings.min_vitals_confidence,
    )


def _node_out(node: Node, now: dt.datetime, stale_after: float) -> NodeOut:
    """Staleness is upstream's flag OR our own last_seen window, never neither.

    Product rule 3: past the window with no frame the node is STALE and its
    readings grey out. We do not fall back to showing the last known value as
    though it were live.
    """
    by_clock = node.last_seen is None or (now - node.last_seen).total_seconds() > stale_after
    return NodeOut(
        node_id=node.node_id,
        name=node.name,
        room_id=node.room_id,
        rssi_dbm=node.rssi_dbm,
        frame_rate_hz=node.frame_rate_hz,
        novelty_score=node.novelty_score,
        last_seen=node.last_seen,
        upstream_stale=node.upstream_stale,
        stale=bool(node.upstream_stale or by_clock),
    )


@router.get("/health", response_model=HealthOut)
async def health(request: Request, session: AsyncSession = Depends(get_session)) -> HealthOut:
    db_status = "ok"
    try:
        await session.execute(select(1))
    except Exception as e:  # pragma: no cover - exercised when the DB is down
        db_status = f"error: {e!r}"

    worker = getattr(request.app.state, "ingest_worker", None)
    pubsub = getattr(request.app.state, "pubsub", None)
    return HealthOut(
        status="ok" if db_status == "ok" else "degraded",
        database=db_status,
        storage_mode=getattr(request.app.state, "storage_mode", None),
        upstream=worker.stats.snapshot() if worker else {},
        ws_clients=pubsub.subscriber_count if pubsub else 0,
    )


@router.get("/rooms", response_model=list[RoomOut])
async def list_rooms(session: AsyncSession = Depends(get_session)) -> list[RoomOut]:
    rows = (
        await session.execute(select(Node.room_id, func.count(Node.id)).group_by(Node.room_id))
    ).all()
    counts: dict[int | None, int] = {room_id: count for room_id, count in rows}
    rooms = (await session.execute(select(Room).order_by(Room.id))).scalars().all()
    return [
        RoomOut(
            id=r.id,
            site_id=r.site_id,
            name=r.name,
            monitoring_enabled=r.monitoring_enabled,
            privacy_mode=r.privacy_mode,
            node_count=int(counts.get(r.id, 0)),
        )
        for r in rooms
    ]


@router.get("/rooms/{room_id}/latest", response_model=RoomLatestOut)
async def room_latest(
    room_id: int,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> RoomLatestOut:
    room = await session.get(Room, room_id)
    if room is None:
        raise HTTPException(status_code=404, detail="room not found")

    nodes = (
        (await session.execute(select(Node).where(Node.room_id == room_id).order_by(Node.node_id)))
        .scalars()
        .all()
    )
    now = dt.datetime.now(dt.UTC)
    node_outs = [_node_out(n, now, settings.stale_after_seconds) for n in nodes]

    reading = (
        await session.execute(
            select(Reading).where(Reading.room_id == room_id).order_by(Reading.time.desc()).limit(1)
        )
    ).scalar_one_or_none()

    # A room is stale unless at least one of its nodes is currently reporting.
    if not node_outs:
        stale, reason = True, "no nodes assigned to this room"
    elif all(n.stale for n in node_outs):
        stale, reason = True, f"no node frame within {settings.stale_after_seconds:.0f}s"
    else:
        stale, reason = False, None

    reading_out: ReadingOut | None = None
    if reading is not None:
        reading_out = ReadingOut(
            time=reading.time,
            presence=reading.presence,
            presence_confidence=reading.presence_confidence,
            motion_level=reading.motion_level,
            motion_band_power=reading.motion_band_power,
            breathing_band_power=reading.breathing_band_power,
            mean_rssi=reading.mean_rssi,
            estimated_persons=reading.estimated_persons,
            vitals=_vitals_from_reading(reading, settings),
        )

    return RoomLatestOut(
        room=RoomOut(
            id=room.id,
            site_id=room.site_id,
            name=room.name,
            monitoring_enabled=room.monitoring_enabled,
            privacy_mode=room.privacy_mode,
            node_count=len(node_outs),
        ),
        reading=reading_out,
        nodes=node_outs,
        stale=stale,
        stale_reason=reason,
    )


@router.get("/rooms/{room_id}/history", response_model=HistoryOut)
async def room_history(
    room_id: int,
    metric: str = Query(default="motion_band_power"),
    from_: dt.datetime | None = Query(default=None, alias="from"),
    to: dt.datetime | None = Query(default=None),
    limit: int = Query(default=5000, ge=1, le=50000),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> HistoryOut:
    if metric not in _HISTORY_METRICS:
        raise HTTPException(
            status_code=400,
            detail=f"unknown metric {metric!r}; expected one of {sorted(_HISTORY_METRICS)}",
        )
    if await session.get(Room, room_id) is None:
        raise HTTPException(status_code=404, detail="room not found")

    value_col, conf_col = _HISTORY_METRICS[metric]
    cols = [Reading.time, value_col] + ([conf_col] if conf_col is not None else [])
    stmt = select(*cols).where(Reading.room_id == room_id)
    if from_ is not None:
        stmt = stmt.where(Reading.time >= from_)
    if to is not None:
        stmt = stmt.where(Reading.time <= to)
    rows = (await session.execute(stmt.order_by(Reading.time).limit(limit))).all()

    # Rows are returned exactly as stored. Missing samples are absent, and a
    # stored NULL stays NULL -- the client draws a gap. No carry-forward, no
    # interpolation (rule 2).
    points = [
        HistoryPoint(
            time=r[0],
            value=None if r[1] is None else float(r[1]),
            confidence=(float(r[2]) if conf_col is not None and r[2] is not None else None),
        )
        for r in rows
    ]
    return HistoryOut(
        room_id=room_id,
        metric=metric,
        min_confidence=settings.min_vitals_confidence if conf_col is not None else None,
        points=points,
    )


@router.get("/nodes", response_model=list[NodeOut])
async def list_nodes(
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> list[NodeOut]:
    nodes = (await session.execute(select(Node).order_by(Node.node_id))).scalars().all()
    now = dt.datetime.now(dt.UTC)
    return [_node_out(n, now, settings.stale_after_seconds) for n in nodes]


@router.websocket("/ws/live")
async def ws_live(websocket: WebSocket) -> None:
    """Fan-out of the ingest stream to browser clients.

    No RuView ws-ticket is involved: tickets exist for talking to RuView, and
    browsers do not. Phase 2 puts SenseWave's own JWT check here.
    """
    pubsub = websocket.app.state.pubsub
    await websocket.accept()
    try:
        async with pubsub.subscribe() as queue:
            await websocket.send_json(
                {"type": "connected", "server_time": dt.datetime.now(dt.UTC).isoformat()}
            )
            while True:
                message = await queue.get()
                await websocket.send_json(message)
    except WebSocketDisconnect:
        log.info("ws.client_disconnected")
    except Exception as e:  # pragma: no cover - transport-level failures
        log.warning("ws.client_error", error=repr(e))
