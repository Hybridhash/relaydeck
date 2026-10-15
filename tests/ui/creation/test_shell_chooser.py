"""Shell rule through real Flet Root/session adapters; explicit HTTP/PTY doubles."""

from dataclasses import replace
from typing import Any, cast

import flet as ft
import pytest

from relaydeck.client.app import Root
from relaydeck.client.features.a11y.focus import focus_order
from relaydeck.client.features.inventory.routing import RouteBridge
from relaydeck.client.features.sync.controller import SyncController
from relaydeck.client.features.sync.transport import HttpxGatewayTransport
from relaydeck.domain.models import Host, Tab, Workspace, scope_key
from tests.support.host_harness import render_component, run
from tests.support.scripted_sessions import make_scripted_sessions
from tests.support.ui.navigation import _state, _StubController, _StubTransport


class CreationTransport(_StubTransport):
    def __init__(self, fault: str = "") -> None:
        super().__init__()
        self.fault = fault

    async def command_status(self, host_id: str, command_id: str) -> dict[str, Any]:
        doc = await super().command_status(host_id, command_id)
        assert doc is not None
        if self.fault:
            doc["status"] = self.fault
            doc.pop("result", None)
            if self.fault == "failed":
                doc["error_code"] = "forbidden"
        return doc


def _inventory(count: int) -> Any:
    state = _state()
    agent_tab = state.inventory.tabs["home-mac:tab-2"]
    tabs = {"home-mac:tab-2": agent_tab}
    for n in range(count):
        tab = Tab(id=f"shell-{n}", host_id="home-mac", workspace_id="w1", label=f"Shell {n}")
        tabs[scope_key(tab.host_id, tab.id)] = tab
    # Same bare host-local IDs and a separate project must never enter the chooser.
    tabs["other-mac:shell-0"] = Tab(id="shell-0", host_id="other-mac", workspace_id="w1")
    tabs["home-mac:other-project"] = Tab(id="other-project", host_id="home-mac", workspace_id="w2")
    inv = replace(
        state.inventory,
        tabs=tabs,
        hosts={**state.inventory.hosts, "other-mac": Host(id="other-mac", label="Other")},
        workspaces={
            **state.inventory.workspaces,
            "other-mac:w1": Workspace(id="w1", host_id="other-mac"),
        },
    )
    return replace(state, inventory=inv)


async def _render(count: int, fault: str = "") -> tuple[Any, ...]:
    sessions = make_scripted_sessions()
    controller = _StubController()
    transport = CreationTransport(fault)
    routes: list[str] = []

    @ft.component
    def Parent() -> Any:
        mounted, set_mounted = ft.use_state(True)
        return ft.Column(
            controls=[
                Root(
                    controller=cast(SyncController, controller),
                    transport=cast(HttpxGatewayTransport, transport),
                    host_id="home-mac",
                    bridge=RouteBridge(route="/", width=1280),
                    go=routes.append,
                    session_factory=sessions.factory,
                    account="synthetic",
                )
                if mounted
                else ft.Text("Closed"),
                ft.Button("Unmount", key="test-unmount", on_click=lambda e: set_mounted(False)),
            ]
        )

    app = await render_component(Parent)
    controller.push(_inventory(count))
    await app.settle()
    await app.set_props_and_fire("agent-home-mac:term-1", {}, "click")
    return app, controller, transport, sessions, routes


@pytest.mark.parametrize("count", [0, 1, 3])
def test_root_agent_to_shell_rule_scopes_candidates_and_never_prompts(count: int) -> None:
    async def scenario() -> None:
        app, controller, transport, sessions, routes = await _render(count)
        try:
            await app.set_props_and_fire("agent-open-shell", {}, "click")
            if count == 3:
                assert app.find("shell-choice-home-mac:shell-0").height == 48
                assert app.find("shell-choice-home-mac:shell-2")
                with pytest.raises(KeyError):
                    app.find("shell-choice-other-mac:shell-0")
                with pytest.raises(KeyError):
                    app.find("shell-choice-home-mac:other-project")
                assert transport.submitted == [] and sessions.ops.attaches == []
                assert focus_order(app.page)[:5] == (
                    "shell-choice-home-mac:shell-0",
                    "shell-choice-home-mac:shell-1",
                    "shell-choice-home-mac:shell-2",
                    "shell-choice-new",
                    "shell-choice-cancel",
                )
                await app.set_props_and_fire("shell-choice-cancel", {}, "click")
                assert sessions.ops.attaches == []
                await app.set_props_and_fire("agent-open-shell", {}, "click")
                await app.set_props_and_fire("shell-choice-home-mac:shell-2", {}, "click")
            await app.settle(50)
            assert len(transport.submitted) == int(count == 0)
            if transport.submitted:
                assert transport.submitted[0]["kind"] == "shell.create"
                assert transport.submitted[0]["target"] == {"workspace_id": "w1"}
            assert len(sessions.ops.attaches) == 1
            assert sessions.ops.attaches[0]["kind"] == "shell"
            assert sessions.ops.attaches[0]["takeover"] is False
            expected = "w1:t1" if count == 0 else f"shell-{2 if count == 3 else 0}"
            assert sessions.ops.attaches[0]["terminal_id"] == expected
            assert sessions.ops.attaches[0]["host_id"] == "home-mac"
            assert sessions.submitted == [] and sessions.prompted == []
            assert not sessions.ops.closes
            assert routes[-1].startswith("/terminals/")
        finally:
            await app.set_props_and_fire("test-unmount", {}, "click")

    run(scenario())


@pytest.mark.parametrize("fault", ["failed", "unknown"])
def test_failed_or_uncertain_zero_shell_creation_does_not_replay_saved_action(fault: str) -> None:
    async def scenario() -> None:
        app, controller, transport, sessions, _ = await _render(0, fault)
        try:
            saved = app.find("agent-open-shell").on_click
            await saved(None)
            await saved(None)
            await app.settle(50)
            assert len(transport.submitted) == 1 and sessions.ops.attaches == []
            assert app.find("sheet-error").visible
            await app.set_props_and_fire("shell-launch-done", {}, "click")
            await saved(None)
            await app.settle()
            assert len(transport.submitted) == 1
            assert "reconcile" in app.find("terminal-open-error").value
        finally:
            await app.set_props_and_fire("test-unmount", {}, "click")

    run(scenario())


@pytest.mark.parametrize("change", ["epoch", "revision", "offline", "revoked", "closed"])
def test_saved_chooser_is_fenced_by_current_context(change: str) -> None:
    async def scenario() -> None:
        app, controller, transport, sessions, _ = await _render(3)
        try:
            await app.set_props_and_fire("agent-open-shell", {}, "click")
            saved = app.find("shell-choice-home-mac:shell-0").on_click
            current = controller.state
            inv = current.inventory
            if change in ("epoch", "offline"):
                host = inv.hosts["home-mac"]
                host = (
                    replace(host, epoch="different")
                    if change == "epoch"
                    else replace(host, reachable=False)
                )
                inv = replace(inv, hosts={**inv.hosts, "home-mac": host})
            elif change == "revision":
                inv = replace(
                    inv,
                    agents={
                        **inv.agents,
                        "home-mac:term-1": replace(inv.agents["home-mac:term-1"], revision=9),
                    },
                )
            elif change == "closed":
                inv = replace(
                    inv, tabs={k: v for k, v in inv.tabs.items() if k != "home-mac:shell-0"}
                )
            controller.push(replace(current, inventory=inv, revoked=change == "revoked"))
            await saved(None)
            await app.settle()
            assert sessions.ops.attaches == [] and transport.submitted == []
        finally:
            await app.set_props_and_fire("test-unmount", {}, "click")

    run(scenario())


def test_live_agent_shell_handoff_preserves_draft_and_invalidates_old_input() -> None:
    async def scenario() -> None:
        app, controller, transport, sessions, _ = await _render(1)
        try:
            await app.settle()
            await app.set_props_and_fire("agent-takeover-confirm", {}, "click")
            await app.settle()
            agent = sessions.factory.created[0]
            old_token = agent.input_token
            await app.set_props_and_fire(
                "composer-draft", {"value": "kept draft"}, "change", "kept draft"
            )
            await app.set_props_and_fire("agent-open-shell", {}, "click")
            await app.settle()
            assert [item["kind"] for item in sessions.ops.attaches] == ["agent", "shell"]
            assert sessions.ops.attaches[-1]["takeover"] is False
            assert not sessions.ops.closes
            assert await agent.handle_view_input(1, b"late", token=old_token) == "dropped"
            # Wide layout: no Back; the destination switch leaves the shell.
            await app.set_props_and_fire("console-rail-console", {}, "click")
            await app.set_props_and_fire("agent-home-mac:term-1", {}, "click")
            await app.settle()
            assert app.find("composer-draft").value == "kept draft"
            assert sessions.submitted == [] and sessions.prompted == []
            assert transport.submitted == []
        finally:
            await app.set_props_and_fire("test-unmount", {}, "click")

    run(scenario())


def test_many_shells_explicit_new_uses_plain_shell_command_once() -> None:
    async def scenario() -> None:
        app, _, transport, sessions, _ = await _render(3)
        try:
            await app.set_props_and_fire("agent-open-shell", {}, "click")
            await app.set_props_and_fire("shell-choice-new", {}, "click")
            await app.settle(50)
            assert len(transport.submitted) == 1
            assert transport.submitted[0]["kind"] == "shell.create"
            assert sessions.ops.attaches[0]["kind"] == "shell"
            assert sessions.prompted == [] and sessions.submitted == []
        finally:
            await app.set_props_and_fire("test-unmount", {}, "click")

    run(scenario())
