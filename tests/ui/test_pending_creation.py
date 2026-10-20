"""Actual Root creation dispatch settles only verified successful operations."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import flet as ft
import httpx
import pytest

from relaydeck.client.app import Root
from relaydeck.client.features.inventory.routing import RouteBridge
from relaydeck.client.features.sync.transport import HttpxGatewayTransport
from relaydeck.client.pending_commands import REVIEW_GRACE_MS, MemoryCommandStore, PendingCommands
from tests.support.host_harness import render_component, run
from tests.support.scripted_sessions import make_scripted_sessions
from tests.support.ui.navigation import _state, _StubController


@pytest.mark.parametrize("kind", ["shell", "agent"])
@pytest.mark.parametrize("inventory", ["present", "unavailable", "changed_epoch"])
def test_root_creation_settles_verified_success(kind: str, inventory: str) -> None:
    async def scenario() -> None:
        journal = PendingCommands(MemoryCommandStore(), "https://gw", "acct", "dev")
        release_status = asyncio.Event()
        submitted: list[dict[str, Any]] = []
        requests: list[tuple[str, str]] = []
        snapshot = json.loads(Path("tests/fixtures/gateway/snapshot.json").read_text())

        async def handler(request: httpx.Request) -> httpx.Response:
            requests.append((request.method, request.url.path))
            if request.url.path == "/v1/hosts":
                return httpx.Response(503, json={"code": "host_offline"})
            if request.method == "POST":
                body = json.loads(request.content)
                submitted.append(body)
                return httpx.Response(
                    202,
                    json={
                        **{
                            k: body[k]
                            for k in ("protocol", "command_id", "host_id", "kind", "target")
                        },
                        "status": "accepted",
                    },
                )
            if "/commands/" in request.url.path:
                await release_status.wait()
                body = submitted[0]
                return httpx.Response(
                    200,
                    json={
                        **{
                            k: body[k]
                            for k in ("protocol", "command_id", "host_id", "kind", "target")
                        },
                        "status": "succeeded",
                        "result": {
                            "type": "agent_started" if kind == "agent" else "tab_created",
                            "agent_id" if kind == "agent" else "tab_id": "term-1"
                            if kind == "agent"
                            else "w1:t2",
                        },
                    },
                )
            assert request.url.path.endswith("/snapshot")
            if inventory == "unavailable":
                return httpx.Response(503, json={"code": "host_offline"})
            if inventory == "changed_epoch":
                snapshot["host"]["epoch"] = "replacement-epoch"
            return httpx.Response(200, json=snapshot)

        async def token() -> str:
            return "synthetic"

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            transport = HttpxGatewayTransport("https://gw", token, http, commands=journal)
            controller = _StubController()
            routes: list[str] = []
            app = await render_component(
                Root,
                controller=controller,
                transport=transport,
                host_id="home-mac",
                bridge=RouteBridge(route="/", width=1280),
                go=routes.append,
                session_factory=make_scripted_sessions().factory,
                account="acct",
                pending_commands=journal,
            )
            try:
                controller.push(_state())
                await app.settle()
                if kind == "shell":
                    await app.set_props_and_fire("console-rail-terminals", {}, "click")
                    await app.set_props_and_fire("new-terminal-open", {}, "click")
                    await app.set_props_and_fire(
                        "shell-label", {"value": "qa-attach"}, "change", "qa-attach"
                    )
                else:
                    await app.set_props_and_fire("console-new-agent", {}, "click")
                await app.set_props_and_fire(f"{kind}-workspace", {"value": "w1"}, "select", "w1")
                routes.clear()
                await app.set_props_and_fire("sheet-submit", {}, "click")
                assert len(submitted) == 1
                assert submitted[0]["kind"] == (
                    "shell.create" if kind == "shell" else "agent.start"
                )
                assert len(journal.records) == 1
                # An operation in flight is not something to review yet.
                assert app.find("command-recovery-empty") is not None
                release_status.set()
                await app.settle(40)
                if inventory == "changed_epoch":
                    assert not routes
                    assert len(journal.records) == 1
                    assert "do not retry" in app.find("sheet-error").value
                    if journal.needs_review(next(iter(journal.records.values())), journal.clock()):
                        assert app.find("command-recovery") is not None
                    else:  # still in flight: it is listed once the grace period passes
                        later = journal.clock() + REVIEW_GRACE_MS
                        journal.clock = lambda: later
                        journal.changed()
                        await app.settle()
                        assert app.find("command-recovery") is not None
                else:
                    assert not journal.records
                    assert not any(journal.unresolved(row) for row in journal.records.values())
                    assert app.find("command-recovery-empty") is not None
                    with pytest.raises(KeyError):
                        app.find("command-recovery")
                    assert routes == [
                        "/agents/home-mac:term-1"
                        if kind == "agent"
                        else "/terminals/home-mac:w1%3At2"
                    ]
                    replacement = PendingCommands(journal.store, "https://gw", "acct", "dev")
                    await replacement.load()
                    assert replacement.records == {}
                assert sum(method == "POST" for method, _ in requests) == 1
            finally:
                release_status.set()
                app.page.render(lambda: ft.Text("Closed"))
                await app.settle()

    run(scenario())
