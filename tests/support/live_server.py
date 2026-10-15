"""Real uvicorn harness for live gateway tests.

Neither Starlette's TestClient nor httpx's ASGITransport streams an
infinite SSE response (both buffer the full body), so live tests boot
a real server on 127.0.0.1 with an OS-assigned port.
"""

from __future__ import annotations

import threading
import time
from types import TracebackType
from typing import Any

import uvicorn


class LiveGateway:
    def __init__(self, app: Any, port: int = 0) -> None:
        # A fixed port lets restart tests bring a new server up where
        # the old one died, so the client's reconnect path is real.
        self._server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
        )
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self.base_url = ""
        self.port = 0

    def __enter__(self) -> LiveGateway:
        self._thread.start()
        deadline = time.monotonic() + 15
        while not self._server.started:
            if time.monotonic() > deadline:
                raise RuntimeError("gateway did not start")
            time.sleep(0.01)
        sockets = self._server.servers[0].sockets if self._server.servers else []
        if not sockets:
            raise RuntimeError("gateway has no sockets")
        port = sockets[0].getsockname()[1]
        self.port = port
        self.base_url = f"http://127.0.0.1:{port}"
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=15)
