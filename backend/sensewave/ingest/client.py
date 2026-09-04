"""Upstream WebSocket transport.

RuView ships ``ruview.client.SensingClient``, and we use it — but it cannot
authenticate. Its constructor is ``(url, *, ping_interval, ping_timeout,
max_size)``: there is no token or header parameter, so a stock instance cannot
send the Bearer header that ``/ws/sensing`` expects on the upgrade. See
docs/ARCHITECTURE.md, "Upstream contract discrepancies".

``AuthenticatedSensingClient`` subclasses it and overrides only the connect step
to attach the header, inheriting the rest of the lifecycle. If the optional
``ruview`` extra is not installed we fall back to a minimal client with the same
surface, so tests and a bare backend run without the upstream wheel.

Neither class reconnects — RuView's client deliberately leaves that to the
application, and the retry policy lives in ``worker.py``.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import websockets

from sensewave.logging import get_logger

log = get_logger(__name__)

try:  # pragma: no cover - exercised by whichever path the environment has
    from ruview.client import SensingClient as _UpstreamSensingClient

    RUVIEW_CLIENT_AVAILABLE = True
except Exception:  # ImportError, or ruview present without [client] extras
    RUVIEW_CLIENT_AVAILABLE = False

    class _UpstreamSensingClient:  # type: ignore[no-redef]
        """Minimal stand-in matching RuView's constructor and lifecycle."""

        def __init__(
            self,
            url: str,
            *,
            ping_interval: float = 20.0,
            ping_timeout: float = 20.0,
            max_size: int = 16 * 1024 * 1024,
        ) -> None:
            self.url = url
            self._ping_interval = ping_interval
            self._ping_timeout = ping_timeout
            self._max_size = max_size
            self._ws: Any = None

        async def close(self) -> None:
            if self._ws is not None:
                try:
                    await self._ws.close()
                except Exception as e:  # pragma: no cover - best effort
                    log.debug("upstream.close_error", error=repr(e))
                self._ws = None


def _connect_kwargs(headers: dict[str, str]) -> dict[str, Any]:
    """websockets renamed ``extra_headers`` to ``additional_headers`` in v14.

    Pick whichever the installed version accepts rather than pinning, so this
    works against both the v12 floor in pyproject and current releases.
    """
    import inspect

    try:
        params = inspect.signature(websockets.connect).parameters
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return {"additional_headers": headers}
    if "additional_headers" in params:
        return {"additional_headers": headers}
    if "extra_headers" in params:
        return {"extra_headers": headers}
    return {}


class AuthenticatedSensingClient(_UpstreamSensingClient):  # type: ignore[misc]
    """RuView's SensingClient, plus the Bearer header it cannot send itself."""

    def __init__(self, url: str, token: str = "", **kwargs: Any) -> None:
        super().__init__(url, **kwargs)
        self._token = token

    async def __aenter__(self) -> AuthenticatedSensingClient:
        headers: dict[str, str] = {}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        else:
            # Not fatal: a server started without auth accepts anonymous
            # upgrades. Worth a warning because it is a deployment smell.
            log.warning("upstream.no_token", url=self.url)

        self._ws = await websockets.connect(
            self.url,
            ping_interval=self._ping_interval,
            ping_timeout=self._ping_timeout,
            max_size=self._max_size,
            **_connect_kwargs(headers),
        )
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self.close()

    async def raw_frames(self) -> AsyncIterator[dict[str, Any]]:
        """Yield each frame as a plain dict.

        We deliberately do not use ``SensingClient.stream()``: its decoder knows
        only ``connection_established``, ``edge_vitals`` and ``pose_data``, and
        returns an untyped passthrough for everything else — including
        ``SensingUpdate``. Validation against our own strict model happens in
        the worker, so the dict is what we want here.
        """
        if self._ws is None:
            raise RuntimeError("client not connected; use `async with`")
        async for frame in self._ws:
            if isinstance(frame, bytes):
                frame = frame.decode("utf-8", errors="replace")
            try:
                obj = json.loads(frame)
            except json.JSONDecodeError as e:
                log.warning("upstream.bad_json", error=str(e))
                continue
            if isinstance(obj, dict):
                yield obj
            else:
                log.warning("upstream.non_dict_frame", got=type(obj).__name__)
