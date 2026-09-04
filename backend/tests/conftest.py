"""Test fixtures.

The database is a real PostgreSQL server, started per-session from the
``pgserver`` wheel so the suite runs with no Docker and no system Postgres. The
schema is built by running the actual Alembic migration rather than
``create_all`` -- that way the migration itself is under test, including its
TimescaleDB-vs-plain branch.

No test connects to a live sensing-server. Upstream is always ``FakeUpstream``.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

BACKEND_DIR = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def pg_uri(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    import pgserver
    import psycopg

    pgdata = tmp_path_factory.mktemp("pgdata")
    server = pgserver.get_server(str(pgdata))
    try:
        # Not server.psql(): that shells out to the bundled psql via a single
        # command string, which breaks when the install path contains a space
        # (as "SenseWave AI" does). psycopg talks to the same server directly.
        with psycopg.connect(server.get_uri(), autocommit=True) as conn:
            conn.execute("CREATE DATABASE sensewave_test")
        yield server.get_uri(database="sensewave_test")
    finally:
        server.cleanup()


@pytest.fixture(scope="session")
def database_url(pg_uri: str) -> str:
    """asyncpg URL for the app; Alembic rewrites it to psycopg itself."""
    return pg_uri.replace("postgresql://", "postgresql+asyncpg://")


@pytest.fixture(scope="session", autouse=True)
def migrated(database_url: str) -> Iterator[None]:
    """Run the real migration once per session."""
    import os

    os.environ["SENSEWAVE_DATABASE_URL"] = database_url
    from sensewave.config import get_settings

    get_settings.cache_clear()

    from alembic.config import Config

    from alembic import command

    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    command.upgrade(cfg, "head")
    yield


@pytest_asyncio.fixture
async def engine(database_url: str, migrated: None) -> AsyncIterator[object]:
    eng = create_async_engine(database_url, future=True)
    try:
        yield eng
    finally:
        await eng.dispose()


@pytest_asyncio.fixture
async def sessionmaker(engine: object) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)  # type: ignore[arg-type]


@pytest_asyncio.fixture(autouse=True)
async def clean_tables(engine: object) -> AsyncIterator[None]:
    """Every test starts from an empty schema."""
    async with engine.begin() as conn:  # type: ignore[attr-defined]
        await conn.execute(
            text("TRUNCATE readings, events, nodes, rooms, sites, users RESTART IDENTITY CASCADE")
        )
    yield


@pytest_asyncio.fixture
async def seeded(sessionmaker: async_sessionmaker[AsyncSession]) -> dict[str, int]:
    """One site, three rooms, two mapped nodes.

    Room 1: normal. Room 2: privacy mode (vitals suppressed).
    Room 3: monitoring disabled (nothing persisted at all).
    """
    from sensewave.models import Node, Room, Site

    async with sessionmaker() as s:
        site = Site(name="Test Site")
        s.add(site)
        await s.flush()

        normal = Room(site_id=site.id, name="Living Room")
        private = Room(site_id=site.id, name="Bedroom", privacy_mode=True)
        off = Room(site_id=site.id, name="Bathroom", monitoring_enabled=False)
        s.add_all([normal, private, off])
        await s.flush()

        s.add_all(
            [
                Node(node_id=1, room_id=normal.id, name="node-1"),
                Node(node_id=2, room_id=private.id, name="node-2"),
                Node(node_id=3, room_id=off.id, name="node-3"),
            ]
        )
        await s.commit()
        return {
            "site": site.id,
            "normal": normal.id,
            "private": private.id,
            "off": off.id,
        }


@pytest.fixture
def settings_factory(database_url: str):  # type: ignore[no-untyped-def]
    """Settings pointed at the test DB, with knobs a test can override."""
    from sensewave.config import Settings

    def make(**overrides: object) -> Settings:
        base: dict[str, object] = {
            "database_url": database_url,
            "ingest_hz": 1000.0,  # effectively no decimation unless a test asks
            "stale_after_seconds": 10.0,
            "reconnect_backoff_initial": 0.01,
            "reconnect_backoff_max": 0.05,
            "min_vitals_confidence": 0.5,
        }
        base.update(overrides)
        return Settings(**base)  # type: ignore[arg-type]

    return make


@pytest.fixture
def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


@pytest_asyncio.fixture
async def app(migrated: None, engine: object):  # type: ignore[no-untyped-def]
    """The real app with ingest disabled -- tests drive frames themselves."""
    from sensewave.main import create_app

    application = create_app(start_ingest=False)
    async with application.router.lifespan_context(application):
        yield application


@pytest_asyncio.fixture
async def client(app):  # type: ignore[no-untyped-def]
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
