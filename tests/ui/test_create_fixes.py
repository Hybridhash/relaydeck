"""Scoped preselection, fresh status, and explicit partial-outcome review."""

from __future__ import annotations

import flet as ft
import pytest

from relaydeck.client.components.command_recovery import CommandRecovery
from relaydeck.client.features.a11y.focus import iter_controls
from relaydeck.client.features.inventory.routing import parse_route
from relaydeck.client.pending_commands import MemoryCommandStore, PendingCommands, RecoveryError
from relaydeck.domain.models import scope_key
from tests.support.creation.pending_creation import _creation, _record
from tests.support.host_harness import button_label, render_component, run
from tests.support.ui.navigation import _render_root, _state


@pytest.mark.parametrize("width", [390, 1280])
@pytest.mark.parametrize("kind", ["agent", "shell"])
def test_root_creation_preselects_the_visible_space_filter(width: int, kind: str) -> None:
    async def scenario() -> None:
        app, _, transport, _, _ = await _render_root(_state(), width=width)
        try:
            if kind == "shell":
                await app.set_props_and_fire(
                    "console-nav-terminals" if width == 390 else "console-rail-terminals",
                    {},
                    "click",
                )
            await app.set_props_and_fire(
                "shell-space-chip-home-mac:w2"
                if kind == "shell"
                else "host-chip-space-home-mac:w2",
                {},
                "click",
            )
            await app.set_props_and_fire(
                "new-terminal-open" if kind == "shell" else "console-new-agent", {}, "click"
            )
            assert app.find(f"{kind}-host").value == "home-mac"
            assert app.find(f"{kind}-workspace").value == "w2"
            assert app.find("sheet-status").value == ""
            assert transport.submitted == []  # Preselection is never a submission.
        finally:
            app.page.render(lambda: ft.Text("Closed"))
            await app.settle()

    run(scenario())


@pytest.mark.parametrize("width", [390, 1280])
@pytest.mark.parametrize("previous", ["agent", "shell"])
def test_success_status_does_not_leak_to_the_next_creation_sheet(width: int, previous: str) -> None:
    async def scenario() -> None:
        app, _, transport, bridge, _ = await _render_root(_state(), width=width)
        try:
            if previous == "shell":
                bridge.set_route("/terminals")
                await app.settle()
            await app.set_props_and_fire(
                "new-terminal-open" if previous == "shell" else "console-new-agent", {}, "click"
            )
            await app.set_props_and_fire(f"{previous}-workspace", {"value": "w1"}, "select", "w1")
            await app.set_props_and_fire("sheet-submit", {}, "click")
            assert len(transport.submitted) == 1
            with pytest.raises(KeyError):
                app.find("sheet-status")
            bridge.set_route("/agents" if previous == "shell" else "/terminals")
            await app.settle()
            await app.set_props_and_fire(
                "console-new-agent" if previous == "shell" else "new-terminal-open", {}, "click"
            )
            assert app.find("sheet-status").value == ""
            assert not app.find("sheet-status").visible
            assert app.find("sheet-error").value == ""
            assert len(transport.submitted) == 1
        finally:
            app.page.render(lambda: ft.Text("Closed"))
            await app.settle()

    run(scenario())


@pytest.mark.parametrize("agent", [False, True])
@pytest.mark.parametrize("outcome", ["failed", "unknown"])
def test_partial_creation_requires_explicit_review_and_never_resends(
    agent: bool, outcome: str
) -> None:
    async def scenario() -> None:
        store = MemoryCommandStore()
        journal = PendingCommands(store, "https://gw", "acct", "dev")
        body = _creation(agent, new_workspace=True)
        await journal.begin(body)
        doc = _record(body, outcome)
        doc["result"] = {
            "type": "partial_creation",
            "workspace_id": "w2",
            "tab_id": "w2:t7",
            "pane_id": "w2:p7",
        }
        await journal.observe("home-mac", body["command_id"], doc)
        routes: list[str] = []
        app = await render_component(
            CommandRecovery,
            journal=journal,
            transport=object(),
            granted=frozenset({"home-mac"}),
            go=routes.append,
        )
        try:
            await app.set_props_and_fire("command-recovery-toggle", {}, "click")
            buttons = {
                button_label(c): c
                for c in iter_controls([app.page.views[0].controls])
                if isinstance(c, ft.Button)
            }
            assert len(journal.records) == 1
            await app.session.dispatch_event(buttons["Open created resources"]._i, "click", "")
            assert len(routes) == 1
            route = parse_route(routes[0])
            assert route.destination == "terminals"
            assert route.selection == scope_key("home-mac", "w2:t7")
            assert len(journal.records) == 1  # Viewing alone does not discard outcome evidence.
            if outcome == "failed":
                await app.session.dispatch_event(buttons["Reviewed"]._i, "click", "")
                await app.settle()
                assert journal.records == {}
                restored = PendingCommands(store, "https://gw", "acct", "dev")
                await restored.load()
                assert restored.records == {}
            else:
                assert "Reviewed" not in buttons
                with pytest.raises(RecoveryError):
                    await journal.acknowledge("home-mac", body["command_id"])
                assert len(journal.records) == 1
        finally:
            app.page.render(lambda: ft.Text("Closed"))
            await app.settle()

    run(scenario())
