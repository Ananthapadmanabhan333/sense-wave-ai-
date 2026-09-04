"""FastAPI application factory.

One process, one upstream WebSocket. The ingest worker starts with the app and
is the only thing in the system that talks to RuView.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator

from fastapi import FastAPI

from sensewave.api.routes import router
from sensewave.config import get_settings
from sensewave.db import dispose_engine, get_engine, get_sessionmaker, report_storage_mode
from sensewave.ingest.pubsub import PubSub
from sensewave.ingest.worker import IngestWorker
from sensewave.logging import configure_logging, get_logger

log = get_logger(__name__)

DISCLAIMER = (
    "SenseWave reports contactless RF estimates. Breathing and heart rate are "
    "not clinically validated and this is not a medical device."
)


def create_app(*, start_ingest: bool = True) -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.pubsub = PubSub()
        try:
            app.state.storage_mode = await report_storage_mode(get_engine())
        except Exception as e:
            # A DB that is not up yet must not stop the app from serving
            # /api/health, which is how an operator finds out why.
            app.state.storage_mode = None
            log.warning("db.unavailable_at_startup", error=repr(e))

        worker = IngestWorker(settings, get_sessionmaker(), app.state.pubsub)
        app.state.ingest_worker = worker
        if start_ingest:
            worker.start()
            log.info("ingest.started", upstream=settings.upstream_ws_url)
        try:
            yield
        finally:
            await worker.stop()
            await dispose_engine()

    app = FastAPI(
        title="SenseWave AI",
        version="0.1.0",
        description=(
            "Monitoring layer over the RuView sensing-server.\n\n"
            f"**{DISCLAIMER}** Occupants are modelled as anonymous counts; "
            "SenseWave does not identify individuals and does not expose pose."
        ),
        lifespan=lifespan,
    )
    app.include_router(router)
    return app


app = create_app()
