"""Ingest worker against a fake upstream: DB writes, fan-out, consent, reconnect."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from sqlalchemy import func, select

from sensewave.ingest.pubsub import PubSub
from sensewave.ingest.worker import IngestWorker
from sensewave.models import Node, Reading
from tests.fake_upstream import FakeUpstream, load_frames

FRAMES = load_frames()
MALFORMED = [
    json.loads(line)
    for line in (Path(__file__).parent / "frames" / "malformed_frames.jsonl")
    .read_text(encoding="utf-8")
    .splitlines()
    if line.strip()
]


async def _drain(worker: IngestWorker, frames: list[dict]) -> None:
    for f in frames:
        await worker.handle_frame(f)


async def test_replays_recorded_frames_into_readings(sessionmaker, seeded, settings_factory):
    """The headline Phase 1 assertion: recorded frames land in the database."""
    worker = IngestWorker(settings_factory(), sessionmaker, PubSub())
    async with FakeUpstream(FRAMES) as upstream:
        worker._settings.upstream_ws_url = upstream.url
        await asyncio.wait_for(worker.consume_once(), timeout=10)

    async with sessionmaker() as s:
        total = (await s.execute(select(func.count()).select_from(Reading))).scalar_one()
        assert total > 0, "no readings persisted from the recorded stream"
        rows = (
            (await s.execute(select(Reading).where(Reading.room_id == seeded["normal"])))
            .scalars()
            .all()
        )
    assert rows, "room 1 (node 1) received nothing"
    assert worker.stats.frames_received == len(FRAMES)
    assert worker.stats.frames_invalid == 0


async def test_sends_bearer_token_on_upgrade(sessionmaker, seeded, settings_factory):
    """RuView's own SensingClient cannot do this; ours must."""
    worker = IngestWorker(settings_factory(upstream_token="test-token-abc"), sessionmaker, PubSub())
    async with FakeUpstream(FRAMES[:2]) as upstream:
        worker._settings.upstream_ws_url = upstream.url
        await asyncio.wait_for(worker.consume_once(), timeout=10)
        assert upstream.auth_headers == ["Bearer test-token-abc"]


async def test_malformed_frames_are_dropped_not_coerced(sessionmaker, seeded, settings_factory):
    worker = IngestWorker(settings_factory(), sessionmaker, PubSub())
    await _drain(worker, MALFORMED)

    assert worker.stats.frames_invalid == len(MALFORMED)
    assert worker.stats.readings_written == 0
    async with sessionmaker() as s:
        assert (await s.execute(select(func.count()).select_from(Reading))).scalar_one() == 0


async def test_null_vitals_stay_null(sessionmaker, seeded, settings_factory):
    """null means not-detected. It must never be stored as 0."""
    worker = IngestWorker(settings_factory(), sessionmaker, PubSub())
    await _drain(worker, [FRAMES[0]])  # br=None, hr=None

    async with sessionmaker() as s:
        r = (await s.execute(select(Reading))).scalars().one()
    assert r.breathing_rate_bpm is None
    assert r.heart_rate_bpm is None
    # The confidence still arrives, so the UI can say why there is no number.
    assert r.signal_quality == pytest.approx(0.18)


async def test_rate_and_confidence_are_stored_together(sessionmaker, seeded, settings_factory):
    worker = IngestWorker(settings_factory(), sessionmaker, PubSub())
    await _drain(worker, [FRAMES[4]])  # br=13.6/0.87, hr=63.4/0.66

    async with sessionmaker() as s:
        r = (await s.execute(select(Reading))).scalars().one()
    assert r.breathing_rate_bpm == pytest.approx(13.6)
    assert r.breathing_confidence == pytest.approx(0.87)
    assert r.heart_rate_bpm == pytest.approx(63.4)
    assert r.heartbeat_confidence == pytest.approx(0.66)


async def test_privacy_mode_suppresses_vitals_but_keeps_presence(
    sessionmaker, seeded, settings_factory
):
    """Consent rule 7, enforced at ingest -- vitals never reach the database."""
    worker = IngestWorker(settings_factory(), sessionmaker, PubSub())
    await _drain(worker, [FRAMES[8]])  # two-node frame: rooms 1 and 2

    async with sessionmaker() as s:
        private = (
            await s.execute(select(Reading).where(Reading.room_id == seeded["private"]))
        ).scalar_one()
        normal = (
            await s.execute(select(Reading).where(Reading.room_id == seeded["normal"]))
        ).scalar_one()

    assert private.breathing_rate_bpm is None
    assert private.heart_rate_bpm is None
    assert private.vitals_suppressed is True
    # Presence and motion survive privacy mode.
    assert private.presence is True
    assert private.motion_band_power == pytest.approx(0.130)
    # The same frame in a normal room keeps its vitals.
    assert normal.breathing_rate_bpm == pytest.approx(15.1)
    assert normal.vitals_suppressed is False


async def test_monitoring_disabled_persists_nothing(sessionmaker, seeded, settings_factory):
    worker = IngestWorker(settings_factory(), sessionmaker, PubSub())
    frame = dict(FRAMES[4])
    frame["nodes"] = [{**frame["nodes"][0], "node_id": 3}]
    frame["node_features"] = [{**frame["node_features"][0], "node_id": 3}]
    await _drain(worker, [frame])

    async with sessionmaker() as s:
        count = (
            await s.execute(
                select(func.count()).select_from(Reading).where(Reading.room_id == seeded["off"])
            )
        ).scalar_one()
    assert count == 0


async def test_unmapped_node_is_registered_but_contributes_no_readings(
    sessionmaker, seeded, settings_factory
):
    worker = IngestWorker(settings_factory(), sessionmaker, PubSub())
    frame = dict(FRAMES[4])
    frame["nodes"] = [{**frame["nodes"][0], "node_id": 99}]
    frame["node_features"] = [{**frame["node_features"][0], "node_id": 99}]
    await _drain(worker, [frame])

    async with sessionmaker() as s:
        node = (await s.execute(select(Node).where(Node.node_id == 99))).scalar_one()
        readings = (await s.execute(select(func.count()).select_from(Reading))).scalar_one()
    assert node.room_id is None, "unmapped node must not be auto-assigned to a room"
    assert readings == 0


async def test_upstream_stale_flag_is_stored_verbatim(sessionmaker, seeded, settings_factory):
    """Rule 3: the server's staleness verdict is authoritative."""
    worker = IngestWorker(settings_factory(), sessionmaker, PubSub())
    await _drain(worker, [FRAMES[6]])  # node reports stale=True

    async with sessionmaker() as s:
        node = (await s.execute(select(Node).where(Node.node_id == 1))).scalar_one()
    assert node.upstream_stale is True


async def test_decimation_limits_write_rate(sessionmaker, seeded, settings_factory):
    """10 Hz in, 1 Hz out -- by keeping real frames, never by averaging."""
    worker = IngestWorker(settings_factory(ingest_hz=1.0), sessionmaker, PubSub())
    # Ten frames delivered inside one wall-clock second.
    await _drain(worker, [FRAMES[4]] * 10)

    async with sessionmaker() as s:
        rows = (
            (await s.execute(select(Reading).where(Reading.room_id == seeded["normal"])))
            .scalars()
            .all()
        )
    assert len(rows) == 1, f"expected 1 decimated write, got {len(rows)}"
    # The stored value is a value that actually arrived, not a mean.
    assert rows[0].breathing_rate_bpm == pytest.approx(13.6)


async def test_fanout_reaches_subscribers(sessionmaker, seeded, settings_factory):
    pubsub = PubSub()
    worker = IngestWorker(settings_factory(), sessionmaker, pubsub)
    async with pubsub.subscribe() as queue:
        await _drain(worker, [FRAMES[4]])
        msg = await asyncio.wait_for(queue.get(), timeout=2)

    assert msg["type"] == "reading"
    assert msg["room_id"] == seeded["normal"]
    # Fan-out always pairs rate with confidence.
    assert msg["vitals"]["breathing_rate_bpm"] == pytest.approx(13.6)
    assert msg["vitals"]["breathing_confidence"] == pytest.approx(0.87)


async def test_fanout_happens_even_when_decimated_away(sessionmaker, seeded, settings_factory):
    """The DB is decimated to 1 Hz; the live stream is not."""
    pubsub = PubSub()
    worker = IngestWorker(settings_factory(ingest_hz=1.0), sessionmaker, pubsub)
    async with pubsub.subscribe() as queue:
        await _drain(worker, [FRAMES[4]] * 5)
        received = [await asyncio.wait_for(queue.get(), timeout=2) for _ in range(5)]
    assert len(received) == 5
    assert worker.stats.readings_written == 1


async def test_pose_keypoints_are_ignored(sessionmaker, seeded, settings_factory):
    """Rule 4: pose is not available and nothing downstream may consume it."""
    pubsub = PubSub()
    worker = IngestWorker(settings_factory(), sessionmaker, pubsub)
    async with pubsub.subscribe() as queue:
        await _drain(worker, [FRAMES[9]])  # carries pose_keypoints
        msg = await asyncio.wait_for(queue.get(), timeout=2)
    assert "pose_keypoints" not in msg
    assert "keypoints" not in json.dumps(msg)


async def test_unknown_future_field_does_not_break_ingest(sessionmaker, seeded, settings_factory):
    worker = IngestWorker(settings_factory(), sessionmaker, PubSub())
    await _drain(worker, [FRAMES[10]])
    assert worker.stats.frames_invalid == 0
    assert worker.stats.readings_written == 1


async def test_reconnects_with_backoff_after_upstream_drop(sessionmaker, seeded, settings_factory):
    """Upstream closes the socket; the worker comes back on its own."""
    settings = settings_factory(reconnect_backoff_initial=0.01, reconnect_backoff_max=0.02)
    worker = IngestWorker(settings, sessionmaker, PubSub())
    async with FakeUpstream(FRAMES[:2], repeat=True, close_after=2) as upstream:
        settings.upstream_ws_url = upstream.url
        worker.start()
        # Wait until the worker has been forced through at least two connects.
        for _ in range(200):
            if upstream.connection_count >= 3:
                break
            await asyncio.sleep(0.05)
        await worker.stop()

    assert upstream.connection_count >= 3, (
        f"expected repeated reconnects, saw {upstream.connection_count}"
    )
    assert worker.stats.upstream_reconnects >= 2


async def test_auto_map_is_off_by_default(sessionmaker, seeded, settings_factory):
    """Default behaviour: SenseWave does not guess which room a sensor is in."""
    worker = IngestWorker(settings_factory(), sessionmaker, PubSub())
    frame = dict(FRAMES[4])
    frame["nodes"] = [{**frame["nodes"][0], "node_id": 77}]
    frame["node_features"] = [{**frame["node_features"][0], "node_id": 77}]
    await _drain(worker, [frame])

    async with sessionmaker() as s:
        node = (await s.execute(select(Node).where(Node.node_id == 77))).scalar_one()
    assert node.room_id is None


async def test_auto_map_assigns_new_nodes_when_explicitly_enabled(
    sessionmaker, seeded, settings_factory
):
    """Opt-in only: this is what makes the zero-hardware simulate demo work."""
    worker = IngestWorker(
        settings_factory(auto_map_room_id=seeded["normal"]), sessionmaker, PubSub()
    )
    frame = dict(FRAMES[4])
    frame["nodes"] = [{**frame["nodes"][0], "node_id": 78}]
    frame["node_features"] = [{**frame["node_features"][0], "node_id": 78}]
    await _drain(worker, [frame])

    async with sessionmaker() as s:
        node = (await s.execute(select(Node).where(Node.node_id == 78))).scalar_one()
    assert node.room_id == seeded["normal"]
