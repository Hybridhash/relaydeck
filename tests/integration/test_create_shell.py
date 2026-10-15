"""New Terminal submits shell.create, live.

Shell creation never invokes agent.start: the recorded kind is
shell.create with a workspace target and an optional label, and
agent-shaped payloads are rejected.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from relaydeck.client.features.create.commands import shell_create_command
from relaydeck.client.features.sync.transport import (
    HttpxGatewayTransport,
    TransportError,
)
from relaydeck.gateway.app import GatewayState, create_app
from relaydeck.gateway.backend import FakeBackend
from tests.support.live_server import LiveGateway

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "gateway"
FULL_CAPS = frozenset(
    {
        "sessions.read",
        "terminal.view",
        "terminal.control",
        "agents.prompt",
        "agents.start",
        "shells.create",
        "terminal.attach",
    }
)


def _seed() -> tuple[GatewayState, FakeBackend]:
    backend = FakeBackend(now_ms=lambda: int(time.time() * 1000))
    snapshot: dict[str, Any] = json.loads((FIXTURES / "snapshot.json").read_text())
    backend.snapshots["home-mac"] = snapshot
    state = GatewayState(
        backend=backend,
        cursor_secret=b"create-shell-test-secret-32bytes!",
        now_ms=lambda: int(time.time() * 1000),
    )
    return state, backend


def _transport(base_url: str, http: httpx.AsyncClient, token: str) -> HttpxGatewayTransport:
    async def get_token() -> str:
        return token

    return HttpxGatewayTransport(base_url=base_url, token=get_token, client=http)


def test_create_shell_accepted_live() -> None:
    async def scenario() -> None:
        state, backend = _seed()
        access, _ = state.sessions.create(
            "acct-1", FULL_CAPS, frozenset({"home-mac"}), state.now_ms()
        )
        with LiveGateway(create_app(state)) as live:
            async with httpx.AsyncClient(base_url=live.base_url, timeout=10.0) as http:
                transport = _transport(live.base_url, http, access)
                body = shell_create_command(
                    command_id="cmd-shell-001",
                    host_id="home-mac",
                    host_epoch="epoch-7",
                    workspace_id="w1",
                    label="dev",
                )
                record = await transport.submit_command(body)
                assert record["kind"] == "shell.create"
                assert record["status"] == "accepted"
                stored = backend.commands["cmd-shell-001"]
                assert stored["payload"] == {"label": "dev"}
                assert stored["target"] == {"workspace_id": "w1"}

    asyncio.run(scenario())


def test_create_shell_rejects_agent_fields() -> None:
    async def scenario() -> None:
        state, _ = _seed()
        access, _ = state.sessions.create(
            "acct-1", FULL_CAPS, frozenset({"home-mac"}), state.now_ms()
        )
        with LiveGateway(create_app(state)) as live:
            async with httpx.AsyncClient(base_url=live.base_url, timeout=10.0) as http:
                transport = _transport(live.base_url, http, access)
                body = shell_create_command(
                    command_id="cmd-shell-002",
                    host_id="home-mac",
                    host_epoch="epoch-7",
                    workspace_id="w1",
                )
                body["payload"] = {"kind": "tool-a", "name": "web"}
                with pytest.raises(TransportError) as exc:
                    await transport.submit_command(body)
                assert exc.value.code == "forbidden"

    asyncio.run(scenario())


def test_create_shell_requires_workspace() -> None:
    async def scenario() -> None:
        state, _ = _seed()
        access, _ = state.sessions.create(
            "acct-1", FULL_CAPS, frozenset({"home-mac"}), state.now_ms()
        )
        with LiveGateway(create_app(state)) as live:
            async with httpx.AsyncClient(base_url=live.base_url, timeout=10.0) as http:
                transport = _transport(live.base_url, http, access)
                body = shell_create_command(
                    command_id="cmd-shell-003",
                    host_id="home-mac",
                    host_epoch="epoch-7",
                    workspace_id="",
                )
                with pytest.raises(TransportError) as exc:
                    await transport.submit_command(body)
                assert exc.value.code == "forbidden"

    asyncio.run(scenario())


def test_create_shell_missing_capability() -> None:
    async def scenario() -> None:
        state, _ = _seed()
        access, _ = state.sessions.create(
            "acct-1",
            frozenset({"sessions.read", "agents.start"}),
            frozenset({"home-mac"}),
            state.now_ms(),
        )
        with LiveGateway(create_app(state)) as live:
            async with httpx.AsyncClient(base_url=live.base_url, timeout=10.0) as http:
                transport = _transport(live.base_url, http, access)
                body = shell_create_command(
                    command_id="cmd-shell-004",
                    host_id="home-mac",
                    host_epoch="epoch-7",
                    workspace_id="w1",
                )
                with pytest.raises(TransportError) as exc:
                    await transport.submit_command(body)
                assert exc.value.code == "unsupported_capability"

    asyncio.run(scenario())


def test_create_shell_disconnected_host() -> None:
    async def scenario() -> None:
        state, backend = _seed()
        backend.offline_hosts.add("home-mac")
        access, _ = state.sessions.create(
            "acct-1", FULL_CAPS, frozenset({"home-mac"}), state.now_ms()
        )
        with LiveGateway(create_app(state)) as live:
            async with httpx.AsyncClient(base_url=live.base_url, timeout=10.0) as http:
                transport = _transport(live.base_url, http, access)
                body = shell_create_command(
                    command_id="cmd-shell-005",
                    host_id="home-mac",
                    host_epoch="epoch-7",
                    workspace_id="w1",
                )
                with pytest.raises(TransportError) as exc:
                    await transport.submit_command(body)
                assert exc.value.code == "host_offline"

    asyncio.run(scenario())
