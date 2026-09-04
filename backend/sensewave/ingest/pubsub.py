"""In-process fan-out from the single ingest worker to many WS clients.

Exactly one process holds the upstream WebSocket (see docs/ARCHITECTURE.md).
Browser clients attach here, never to RuView. Subscribers get a bounded queue:
a slow client drops its own oldest frames and is counted, rather than applying
backpressure to ingest and stalling every other client.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from sensewave.logging import get_logger

log = get_logger(__name__)


class PubSub:
    def __init__(self, queue_size: int = 64) -> None:
        self._queue_size = queue_size
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._lock = asyncio.Lock()
        self.dropped_frames = 0

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    async def publish(self, message: dict[str, Any]) -> None:
        async with self._lock:
            targets = list(self._subscribers)
        for q in targets:
            try:
                q.put_nowait(message)
            except asyncio.QueueFull:
                # Drop this subscriber's oldest frame, not the newest: a live
                # dashboard wants the freshest state, and stale means stale.
                try:
                    q.get_nowait()
                    q.put_nowait(message)
                except (asyncio.QueueEmpty, asyncio.QueueFull):  # pragma: no cover
                    pass
                self.dropped_frames += 1

    @asynccontextmanager
    async def subscribe(self) -> AsyncIterator[asyncio.Queue[dict[str, Any]]]:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=self._queue_size)
        async with self._lock:
            self._subscribers.add(q)
        log.info("pubsub.subscribe", subscribers=len(self._subscribers))
        try:
            yield q
        finally:
            async with self._lock:
                self._subscribers.discard(q)
            log.info("pubsub.unsubscribe", subscribers=len(self._subscribers))
