"""Scripted sync transport and documents."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from typing import Any

from relaydeck.client.features.sync.sse import SseFrame
from relaydeck.client.features.sync.transport import GatewayTransport, TransportError
from tests.support.paths import TESTS_DIR

FIXTURES = TESTS_DIR / "fixtures" / "gateway"


def _snapshot_doc() -> dict[str, Any]:
    doc: dict[str, Any] = json.loads((FIXTURES / "snapshot.json").read_text())
    doc["host"]["id"] = "h1"
    doc["host"]["epoch"] = "e1"
    for key in ("workspaces", "tabs", "panes", "agents"):
        for item in doc[key]:
            item["host_id"] = "h1"
    doc["cursor"] = "c0"
    return doc


def _event_doc(seq: int, **kw: Any) -> dict[str, Any]:
    doc: dict[str, Any] = json.loads((FIXTURES / "metadata-event.json").read_text())
    doc.update(
        {
            "event_id": f"ev-{seq}",
            "host_id": "h1",
            "host_epoch": "e1",
            "sequence": seq,
            "cursor": f"c{seq}",
        }
    )
    doc.update(kw)
    return doc


class _ScriptedTransport(GatewayTransport):
    def __init__(self) -> None:
        self.snapshots = 0
        self.scripts: list[Any] = []
        self.cursors: list[str] = []
        self.finished = False

    async def me(self) -> dict[str, Any]:
        raise NotImplementedError

    async def hosts(self) -> dict[str, Any]:
        raise NotImplementedError

    async def agents(
        self,
        host_id: str,
        limit: int = 50,
        page_token: str = "",
        *,
        project: str = "",
        status: str = "",
        query: str = "",
    ) -> dict[str, Any]:
        raise NotImplementedError

    async def snapshot(self, host_id: str) -> dict[str, Any]:
        self.snapshots += 1
        assert host_id == "h1"
        return _snapshot_doc()

    async def stream(self, host_id: str, cursor: str) -> AsyncGenerator[SseFrame]:
        self.cursors.append(cursor)
        script = self.scripts.pop(0) if self.scripts else []
        for item in script:
            if isinstance(item, TransportError):
                raise item
            yield item
            await asyncio.sleep(0)
        self.finished = not self.scripts

    async def submit_command(self, command: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    async def command_status(self, host_id: str, command_id: str) -> dict[str, Any] | None:
        raise NotImplementedError

    async def terminal_ticket(self, lease_id: str) -> dict[str, Any]:
        raise NotImplementedError


def _meta(seq: int, **kw: Any) -> SseFrame:
    return SseFrame("metadata", json.dumps(_event_doc(seq, **kw)))
