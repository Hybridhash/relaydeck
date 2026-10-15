"""Two-host transport and agent key for open-terminal tests."""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from typing import Any

from relaydeck.client.features.sync.sse import SseFrame
from relaydeck.client.features.sync.transport import GatewayTransport
from relaydeck.domain.models import scope_key
from tests.support.metadata_page import snapshot_page
from tests.support.paths import TESTS_DIR

FIXTURES = TESTS_DIR / "fixtures" / "gateway"


AGENT_ID = "a1"


# Rows/routes/selections carry host-scoped keys; the ops wire
# stays bare (see test_wire_boundary_carries_bare_upstream_ids).
AGENT_KEY = scope_key("h1", AGENT_ID)


def _snapshot(host_id: str) -> dict[str, Any]:
    doc: dict[str, Any] = json.loads((FIXTURES / "snapshot.json").read_text())
    doc["host"]["id"] = host_id
    doc["host"]["epoch"] = f"{host_id}-e1"
    for key in ("workspaces", "tabs", "panes"):
        for item in doc[key]:
            item["host_id"] = host_id
    template = dict(doc["agents"][0])
    template["host_id"] = host_id
    template["terminal_id"] = AGENT_ID
    template["name"] = AGENT_ID
    doc["agents"] = [template]
    doc["cursor"] = f"c-{host_id}"
    doc["host"]["agent_catalog"] = {
        "status": "ready",
        "source": "synthetic",
        "observed_ms": 0,
        "entries": [{"kind": "tool-a", "available": True, "reason": "installed"}],
    }
    return doc


def _host_entry(host_id: str) -> dict[str, Any]:
    return {
        "host_id": host_id,
        "label": host_id,
        "epoch": f"{host_id}-e1",
        "reachable": True,
        "upstream": {"protocol": 22, "version": "0.9.1", "compatible": True},
    }


class _TwoHostTransport(GatewayTransport):
    async def me(self) -> dict[str, Any]:
        raise NotImplementedError

    async def hosts(self) -> dict[str, Any]:
        return {"hosts": [_host_entry("h1"), _host_entry("h2")]}

    async def snapshot(self, host_id: str) -> dict[str, Any]:
        return _snapshot(host_id)

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
        return snapshot_page(_snapshot(host_id), limit, page_token, project, status, query)

    async def stream(self, host_id: str, cursor: str) -> AsyncGenerator[SseFrame]:
        for _ in ():
            yield SseFrame("metadata", "{}")

    def __init__(self) -> None:
        self.submitted: list[dict[str, Any]] = []

    async def submit_command(self, command: dict[str, Any]) -> dict[str, Any]:
        self.submitted.append(command)
        return {"command_id": command["command_id"], "status": "accepted"}

    async def command_status(self, host_id: str, command_id: str) -> dict[str, Any] | None:
        command = next(command for command in self.submitted if command["command_id"] == command_id)
        return {
            **{
                key: command[key] for key in ("protocol", "command_id", "host_id", "kind", "target")
            },
            "status": "succeeded",
            "result": {"type": "agent_started", "agent_id": AGENT_ID},
        }

    async def terminal_ticket(self, lease_id: str) -> dict[str, Any]:
        raise NotImplementedError
