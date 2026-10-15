"""Start Agent submits agent.start, live.

The client command builder, transport and gateway policy are driven
end to end: accepted submissions record agent.start with kind/name/
argv, while unknown kinds, missing capabilities, stale epochs and
disconnected hosts fail explicitly.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from relaydeck.client.features.create.commands import agent_start_command
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
        cursor_secret=b"start-agent-test-secret-32bytes!",
        now_ms=lambda: int(time.time() * 1000),
    )
    return state, backend


def _transport(base_url: str, http: httpx.AsyncClient, token: str) -> HttpxGatewayTransport:
    async def get_token() -> str:
        return token

    return HttpxGatewayTransport(base_url=base_url, token=get_token, client=http)


def test_start_agent_accepted_live() -> None:
    async def scenario() -> None:
        state, backend = _seed()
        access, _ = state.sessions.create(
            "acct-1", FULL_CAPS, frozenset({"home-mac"}), state.now_ms()
        )
        with LiveGateway(create_app(state)) as live:
            async with httpx.AsyncClient(base_url=live.base_url, timeout=10.0) as http:
                transport = _transport(live.base_url, http, access)
                body = agent_start_command(
                    command_id="cmd-start-001",
                    host_id="home-mac",
                    host_epoch="epoch-7",
                    workspace_id="w1",
                    kind="agent-a",
                    name="web",
                    argv=["--model", "o3"],
                )
                record = await transport.submit_command(body)
                assert record["kind"] == "agent.start"
                assert record["status"] == "accepted"
                stored = backend.commands["cmd-start-001"]
                assert stored["payload"] == {
                    "kind": "agent-a",
                    "name": "web",
                    "argv": ["--model", "o3"],
                }
                assert stored["target"] == {"workspace_id": "w1"}
                status = await transport.command_status("home-mac", "cmd-start-001")
                assert status is not None and status["command_id"] == "cmd-start-001"
                assert await transport.command_status("home-mac", "missing") is None

    asyncio.run(scenario())


def test_start_agent_unknown_kind_rejected() -> None:
    async def scenario() -> None:
        state, _ = _seed()
        access, _ = state.sessions.create(
            "acct-1", FULL_CAPS, frozenset({"home-mac"}), state.now_ms()
        )
        with LiveGateway(create_app(state)) as live:
            async with httpx.AsyncClient(base_url=live.base_url, timeout=10.0) as http:
                transport = _transport(live.base_url, http, access)
                body = agent_start_command(
                    command_id="cmd-start-002",
                    host_id="home-mac",
                    host_epoch="epoch-7",
                    workspace_id="w1",
                    kind="Evil Kind",
                    name="web",
                )
                with pytest.raises(TransportError) as exc:
                    await transport.submit_command(body)
                assert exc.value.code == "forbidden"

    asyncio.run(scenario())


def test_start_agent_missing_capability() -> None:
    async def scenario() -> None:
        state, _ = _seed()
        access, _ = state.sessions.create(
            "acct-1",
            frozenset({"sessions.read", "shells.create"}),
            frozenset({"home-mac"}),
            state.now_ms(),
        )
        with LiveGateway(create_app(state)) as live:
            async with httpx.AsyncClient(base_url=live.base_url, timeout=10.0) as http:
                transport = _transport(live.base_url, http, access)
                body = agent_start_command(
                    command_id="cmd-start-003",
                    host_id="home-mac",
                    host_epoch="epoch-7",
                    workspace_id="w1",
                    kind="agent-a",
                    name="web",
                )
                with pytest.raises(TransportError) as exc:
                    await transport.submit_command(body)
                assert exc.value.code == "unsupported_capability"

    asyncio.run(scenario())


def test_start_agent_stale_epoch_and_conflict() -> None:
    async def scenario() -> None:
        state, _ = _seed()
        access, _ = state.sessions.create(
            "acct-1", FULL_CAPS, frozenset({"home-mac"}), state.now_ms()
        )
        with LiveGateway(create_app(state)) as live:
            async with httpx.AsyncClient(base_url=live.base_url, timeout=10.0) as http:
                transport = _transport(live.base_url, http, access)
                stale = agent_start_command(
                    command_id="cmd-start-004",
                    host_id="home-mac",
                    host_epoch="epoch-1",
                    workspace_id="w1",
                    kind="agent-a",
                    name="web",
                )
                with pytest.raises(TransportError) as exc:
                    await transport.submit_command(stale)
                assert exc.value.code == "stale_epoch"
                first = agent_start_command(
                    command_id="cmd-start-005",
                    host_id="home-mac",
                    host_epoch="epoch-7",
                    workspace_id="w1",
                    kind="agent-a",
                    name="web",
                )
                assert (await transport.submit_command(first))["status"] == "accepted"
                assert (await transport.submit_command(first))["status"] == "accepted"
                clash = dict(first, payload={"kind": "agent-a", "name": "other"})
                with pytest.raises(TransportError) as exc:
                    await transport.submit_command(clash)
                assert exc.value.code == "command_conflict"

    asyncio.run(scenario())


def test_start_agent_disconnected_host() -> None:
    async def scenario() -> None:
        state, backend = _seed()
        backend.offline_hosts.add("home-mac")
        access, _ = state.sessions.create(
            "acct-1", FULL_CAPS, frozenset({"home-mac"}), state.now_ms()
        )
        with LiveGateway(create_app(state)) as live:
            async with httpx.AsyncClient(base_url=live.base_url, timeout=10.0) as http:
                transport = _transport(live.base_url, http, access)
                body = agent_start_command(
                    command_id="cmd-start-006",
                    host_id="home-mac",
                    host_epoch="epoch-7",
                    workspace_id="w1",
                    kind="agent-a",
                    name="web",
                )
                with pytest.raises(TransportError) as exc:
                    await transport.submit_command(body)
                assert exc.value.code == "host_offline"
                assert exc.value.retryable is True

    asyncio.run(scenario())
