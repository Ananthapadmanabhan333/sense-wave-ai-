"""A stand-in for RuView's ``/ws/sensing``, for tests only.

This is the *only* place in the repo allowed to emit sensing data that no sensor
produced, and it lives under tests/ deliberately. It replays frames from a
recorded JSONL fixture; it does not model anything and it does not generate
plausible physiology. Product rule 2 is about what ships, and nothing here
ships.

It also records the Authorization header it was handed, so a test can assert
that the ingest worker actually authenticates on the upgrade -- the thing
RuView's own SensingClient cannot do.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import websockets
from websockets.asyncio.server import Server, ServerConnection, serve

FRAMES_PATH = Path(__file__).parent / "frames" / "recorded_frames.jsonl"


def load_frames(path: Path = FRAMES_PATH) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


class FakeUpstream:
    """Serves recorded frames over a real WebSocket on an ephemeral port."""

    def __init__(
        self,
        frames: list[dict[str, Any]] | None = None,
        *,
        interval: float = 0.0,
        repeat: bool = False,
        close_after: int | None = None,
    ) -> None:
        self.frames = frames if frames is not None else load_frames()
        self.interval = interval
        self.repeat = repeat
        # Drop the connection after N frames, to exercise reconnect/backoff.
        self.close_after = close_after

        self._server: Server | None = None
        self.port: int = 0
        self.connection_count = 0
        self.auth_headers: list[str | None] = []

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}/ws/sensing"

    async def __aenter__(self) -> FakeUpstream:
        self._server = await serve(self._handler, "127.0.0.1", 0)
        self.port = next(iter(self._server.sockets)).getsockname()[1]
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def _handler(self, ws: ServerConnection) -> None:
        self.connection_count += 1
        self.auth_headers.append(ws.request.headers.get("Authorization"))
        sent = 0
        try:
            while True:
                for frame in self.frames:
                    await ws.send(json.dumps(frame))
                    sent += 1
                    if self.close_after is not None and sent >= self.close_after:
                        await ws.close()
                        return
                    if self.interval:
                        await asyncio.sleep(self.interval)
                if not self.repeat:
                    return
        except websockets.exceptions.ConnectionClosed:
            return
