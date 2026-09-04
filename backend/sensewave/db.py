"""Async engine/session wiring, plus TimescaleDB detection.

We prefer a hypertable on ``readings``. Plain PostgreSQL is a supported
fallback: docker-compose runs timescale/timescaledb-ha, but a local dev machine
without Docker runs a vanilla server (see docs/RUNBOOK.md). The migration
creates a hypertable when the extension is available and a BRIN-indexed plain
table when it is not, and the backend logs which path it took at startup so the
deployment is never ambiguous.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from sensewave.config import get_settings
from sensewave.logging import get_logger

log = get_logger(__name__)

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        _engine = create_async_engine(get_settings().database_url, pool_pre_ping=True, future=True)
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(get_engine(), expire_on_commit=False)
    return _sessionmaker


async def get_session() -> AsyncIterator[AsyncSession]:
    async with get_sessionmaker()() as session:
        yield session


async def timescale_available(engine: AsyncEngine) -> bool:
    async with engine.connect() as conn:
        row = await conn.execute(
            text("SELECT 1 FROM pg_available_extensions WHERE name = 'timescaledb'")
        )
        return row.first() is not None


async def report_storage_mode(engine: AsyncEngine) -> str:
    mode = "timescaledb-hypertable" if await timescale_available(engine) else "plain-postgres"
    log.info("db.storage_mode", mode=mode)
    return mode


async def dispose_engine() -> None:
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None
