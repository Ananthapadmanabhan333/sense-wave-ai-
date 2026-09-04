"""API surface: health, rooms, latest, history, and the /ws/live fan-out."""

from __future__ import annotations

import asyncio
import datetime as dt

import pytest

from sensewave.models import Reading


async def _add_reading(sessionmaker, room_id: int, **kw) -> None:  # type: ignore[no-untyped-def]
    async with sessionmaker() as s:
        s.add(Reading(room_id=room_id, **kw))
        await s.commit()


async def test_health_reports_storage_mode_and_upstream(client, seeded):
    r = await client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["database"] == "ok"
    # pgserver ships vanilla Postgres, so this run is the documented fallback.
    assert body["storage_mode"] in {"plain-postgres", "timescaledb-hypertable"}
    assert "frames_received" in body["upstream"]


async def test_list_rooms_includes_consent_state(client, seeded):
    rooms = (await client.get("/api/rooms")).json()
    by_name = {r["name"]: r for r in rooms}
    assert by_name["Living Room"]["monitoring_enabled"] is True
    assert by_name["Bedroom"]["privacy_mode"] is True
    assert by_name["Bathroom"]["monitoring_enabled"] is False
    assert by_name["Living Room"]["node_count"] == 1


async def test_latest_flags_low_confidence_instead_of_returning_a_bare_number(
    client, seeded, sessionmaker
):
    """Product rule 1: a number below the floor must be marked, not just sent."""
    now = dt.datetime.now(dt.UTC)
    await _add_reading(
        sessionmaker,
        seeded["normal"],
        time=now,
        presence=True,
        presence_confidence=0.9,
        breathing_rate_bpm=14.2,
        breathing_confidence=0.31,  # below the 0.5 floor
        heart_rate_bpm=None,
        heartbeat_confidence=0.0,
        signal_quality=0.44,
    )
    body = (await client.get(f"/api/rooms/{seeded['normal']}/latest")).json()
    vitals = body["reading"]["vitals"]

    assert vitals["breathing_is_low_confidence"] is True
    assert vitals["breathing_confidence"] == pytest.approx(0.31)
    # A null rate stays null and is separately flagged -- never 0.
    assert vitals["heart_rate_bpm"] is None
    assert vitals["heartbeat_is_low_confidence"] is True


async def test_latest_marks_confident_reading_as_such(client, seeded, sessionmaker):
    await _add_reading(
        sessionmaker,
        seeded["normal"],
        time=dt.datetime.now(dt.UTC),
        breathing_rate_bpm=13.6,
        breathing_confidence=0.87,
        heart_rate_bpm=63.4,
        heartbeat_confidence=0.66,
        signal_quality=0.79,
    )
    vitals = (await client.get(f"/api/rooms/{seeded['normal']}/latest")).json()["reading"]["vitals"]
    assert vitals["breathing_is_low_confidence"] is False
    assert vitals["breathing_rate_bpm"] == pytest.approx(13.6)
    assert vitals["heartbeat_is_low_confidence"] is False


async def test_room_with_no_recent_node_frame_is_stale(client, seeded, sessionmaker):
    """Product rule 3: never present a stale room as live."""
    from sqlalchemy import select

    from sensewave.models import Node

    async with sessionmaker() as s:
        node = (await s.execute(select(Node).where(Node.node_id == 1))).scalar_one()
        node.last_seen = dt.datetime.now(dt.UTC) - dt.timedelta(seconds=120)
        await s.commit()

    body = (await client.get(f"/api/rooms/{seeded['normal']}/latest")).json()
    assert body["stale"] is True
    assert "no node frame within" in body["stale_reason"]
    assert body["nodes"][0]["stale"] is True


async def test_room_with_fresh_node_is_not_stale(client, seeded, sessionmaker):
    from sqlalchemy import select

    from sensewave.models import Node

    async with sessionmaker() as s:
        node = (await s.execute(select(Node).where(Node.node_id == 1))).scalar_one()
        node.last_seen = dt.datetime.now(dt.UTC)
        await s.commit()

    body = (await client.get(f"/api/rooms/{seeded['normal']}/latest")).json()
    assert body["stale"] is False
    assert body["stale_reason"] is None


async def test_upstream_stale_flag_alone_makes_a_node_stale(client, seeded, sessionmaker):
    """Even with a fresh clock, upstream's verdict wins."""
    from sqlalchemy import select

    from sensewave.models import Node

    async with sessionmaker() as s:
        node = (await s.execute(select(Node).where(Node.node_id == 1))).scalar_one()
        node.last_seen = dt.datetime.now(dt.UTC)
        node.upstream_stale = True
        await s.commit()

    body = (await client.get(f"/api/rooms/{seeded['normal']}/latest")).json()
    assert body["nodes"][0]["stale"] is True
    assert body["stale"] is True


async def test_room_with_no_nodes_is_stale_with_a_reason(client, seeded, sessionmaker):
    from sqlalchemy import select

    from sensewave.models import Node

    async with sessionmaker() as s:
        node = (await s.execute(select(Node).where(Node.node_id == 1))).scalar_one()
        node.room_id = None
        await s.commit()

    body = (await client.get(f"/api/rooms/{seeded['normal']}/latest")).json()
    assert body["stale"] is True
    assert body["stale_reason"] == "no nodes assigned to this room"


async def test_history_preserves_gaps_and_does_not_interpolate(client, seeded, sessionmaker):
    """Product rule 2: a hole in the data is a hole in the series."""
    t0 = dt.datetime.now(dt.UTC) - dt.timedelta(minutes=10)
    for i, br in enumerate([13.5, None, None, 14.1]):
        await _add_reading(
            sessionmaker,
            seeded["normal"],
            time=t0 + dt.timedelta(seconds=i),
            breathing_rate_bpm=br,
            breathing_confidence=0.8 if br is not None else 0.05,
        )

    body = (
        await client.get(
            f"/api/rooms/{seeded['normal']}/history", params={"metric": "breathing_rate_bpm"}
        )
    ).json()
    values = [p["value"] for p in body["points"]]
    assert values == [13.5, None, None, 14.1], "gaps must survive as nulls"
    # The confidence series comes back alongside so the chart can band it.
    assert body["points"][0]["confidence"] == pytest.approx(0.8)
    assert body["min_confidence"] == pytest.approx(0.5)


async def test_history_rejects_unknown_metric(client, seeded):
    r = await client.get(f"/api/rooms/{seeded['normal']}/history", params={"metric": "nonsense"})
    assert r.status_code == 400
    assert "unknown metric" in r.json()["detail"]


async def test_history_respects_time_window(client, seeded, sessionmaker):
    base = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
    for i in range(5):
        await _add_reading(
            sessionmaker,
            seeded["normal"],
            time=base + dt.timedelta(minutes=i),
            motion_band_power=float(i),
        )
    body = (
        await client.get(
            f"/api/rooms/{seeded['normal']}/history",
            params={
                "metric": "motion_band_power",
                "from": (base + dt.timedelta(minutes=1)).isoformat(),
                "to": (base + dt.timedelta(minutes=3)).isoformat(),
            },
        )
    ).json()
    assert [p["value"] for p in body["points"]] == [1.0, 2.0, 3.0]


async def test_latest_404_for_unknown_room(client, seeded):
    assert (await client.get("/api/rooms/9999/latest")).status_code == 404


async def _serve(app):  # type: ignore[no-untyped-def]
    """Run the real app under uvicorn on an ephemeral port."""
    import uvicorn

    cfg = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", lifespan="off")
    srv = uvicorn.Server(cfg)
    task = asyncio.create_task(srv.serve())
    for _ in range(200):
        if srv.started:
            break
        await asyncio.sleep(0.05)
    assert srv.started, "uvicorn did not start"
    return srv, task, srv.servers[0].sockets[0].getsockname()[1]


async def test_ws_live_streams_published_readings(app, seeded, sessionmaker, settings_factory):
    """Browser clients get the ingest stream from us, never from RuView.

    Driven through a real uvicorn server and a real WebSocket client rather than
    a test transport, so this covers the same path as the acceptance check
    (`wscat -c ws://localhost:8000/api/ws/live`).
    """
    import json

    import websockets

    from sensewave.ingest.worker import IngestWorker
    from tests.fake_upstream import load_frames

    srv, task, port = await _serve(app)
    try:
        async with websockets.connect(f"ws://127.0.0.1:{port}/api/ws/live") as ws:
            hello = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
            assert hello["type"] == "connected"

            # connect() returns at the handshake, before the route has
            # registered its subscription; give the server a loop turn to get
            # there. Harness-only -- nothing in the product depends on it.
            await asyncio.sleep(0.3)
            assert app.state.pubsub.subscriber_count == 1

            worker = IngestWorker(settings_factory(), sessionmaker, app.state.pubsub)
            await worker.handle_frame(load_frames()[4])

            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
    finally:
        srv.should_exit = True
        try:
            await asyncio.wait_for(task, timeout=5)
        except TimeoutError:
            pass

    assert msg["type"] == "reading"
    assert msg["room_id"] == seeded["normal"]
    assert msg["vitals"]["breathing_rate_bpm"] == pytest.approx(13.6)
    assert msg["vitals"]["breathing_confidence"] == pytest.approx(0.87)
    # Rule 4: pose never reaches a client.
    assert "pose_keypoints" not in msg
