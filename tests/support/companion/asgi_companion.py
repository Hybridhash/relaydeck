"""In-process ASGI server and config file for companion tests."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from relaydeck.companion.config import CompanionConfig
from relaydeck.companion.private import read_private, write_json


def config_file(tmp_path: Path, **overrides: Any) -> Path:
    for name, text in (
        ("cookie", "synthetic-cookie-key-for-c0-tests!!"),
        ("cursor", "synthetic-cursor-key-for-c0-tests!!"),
    ):
        target = tmp_path / name
        target.write_text(text)
        target.chmod(0o600)
    values = {
        "advertised_origin": "https://test-mac.synthetic-tailnet.ts.net",
        "cookie_secret_file": "cookie",
        "cursor_secret_file": "cursor",
        "state_dir": "state",
        "connector_credentials": "connector.json",
        "herdr_socket": "missing-herdr.sock",
        **overrides,
    }
    target = tmp_path / "companion.toml"
    target.write_text("\n".join(f"{k} = {json.dumps(v)}" for k, v in values.items()))
    target.chmod(0o600)
    return target


class AsgiServer:
    def __init__(self, app: Any) -> None:
        self.app = app
        self.started = False
        self.should_exit = False
        self.closed = False

    async def serve(self) -> None:
        # Drive startup and shutdown through the ASGI lifespan protocol, as a real server does:
        # the companion wraps its app in plain ASGI middleware that has no FastAPI router.
        inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        outbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        await inbox.put({"type": "lifespan.startup"})
        task = asyncio.create_task(
            self.app({"type": "lifespan", "asgi": {"version": "3.0"}}, inbox.get, outbox.put)
        )
        try:
            started = await outbox.get()
            if started["type"] != "lifespan.startup.complete":
                raise RuntimeError(started.get("message", "lifespan startup failed"))
            self.started = True
            while not self.should_exit:
                await asyncio.sleep(0.01)
            await inbox.put({"type": "lifespan.shutdown"})
            stopped = await outbox.get()
            if stopped["type"] != "lifespan.shutdown.complete":
                raise RuntimeError(stopped.get("message", "lifespan shutdown failed"))
        finally:
            await task
        self.closed = True


class ProcessResources:
    """Child-process ownership fixture, not an application serving proof."""

    def __init__(self, config: CompanionConfig, **kwargs: Any) -> None:
        self.config = config
        self.closed = False

    async def start(self, stop: asyncio.Event) -> None:
        assert not stop.is_set()
        path = self.config.state_dir / "synthetic-starts.json"
        count = json.loads(read_private(path))["count"] if path.exists() else 0
        write_json(path, {"count": count + 1})

    def report(self) -> dict[str, Any]:
        return {"state": "ready", "synthetic": True, "failed": False}

    async def close(self) -> None:
        self.closed = True
        write_json(self.config.state_dir / "synthetic-closed.json", {"closed": True})
