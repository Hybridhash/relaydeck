"""Real inventory menu and declarative rename dialog event dispatch."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from typing import Any

import flet as ft
import httpx
import pytest

from relaydeck.client.app import Root
from relaydeck.client.features.a11y.focus import iter_controls
from relaydeck.client.features.create.components import StartAgentSheet
from relaydeck.client.features.create.validation import AgentDraft, validate_agent
from relaydeck.client.features.inventory.components import AppShell
from relaydeck.client.features.inventory.rename_presentation import CLOSE_FALLBACK_S
from relaydeck.client.features.inventory.routing import RouteBridge
from relaydeck.client.features.sync.transport import HttpxGatewayTransport, TransportError
from relaydeck.client.pending_commands import MemoryCommandStore, PendingCommands
from relaydeck.domain.models import ConnectionState
from tests.support.host_harness import HostApp, button_label, render_component, run
from tests.support.scripted_sessions import make_scripted_sessions
from tests.support.ui.navigation import _state, _StubController, _StubTransport


class RenameTransport:
    def __init__(self) -> None:
        self.submitted: list[dict[str, Any]] = []
        self.reads = 0
        self.release = asyncio.Event()
        self.release.set()
        self.status = "succeeded"
        self.failure = False

    def record(self, command: dict[str, Any], status: str) -> dict[str, Any]:
        return {
            **{
                key: command[key] for key in ("protocol", "command_id", "host_id", "kind", "target")
            },
            "status": status,
            "updated_ms": 1,
        }

    async def submit_command(self, command: dict[str, Any]) -> dict[str, Any]:
        self.submitted.append(command)
        await self.release.wait()
        if self.failure:
            raise TransportError("unavailable", "response lost", retryable=True)
        return self.record(command, "accepted")

    async def command_status(self, host: str, command_id: str) -> dict[str, Any]:
        self.reads += 1
        assert host == "home-mac" and command_id == self.submitted[-1]["command_id"]
        return self.record(self.submitted[-1], self.status)


async def close_reported(app: HostApp) -> None:
    """The sheet closes natively first (open=False); it unmounts on the client's close report."""
    try:
        sheet = app.find("agent-rename-dialog")
    except KeyError:
        return  # already hidden, so it unmounted at once
    assert not sheet.open
    await app.session.dispatch_event(sheet._i, "dismiss", "")
    await app.settle()
    with pytest.raises(KeyError):
        app.find("agent-rename-dialog")


async def open_rename(app: HostApp) -> None:
    """Long-press the card, then choose Rename from its menu."""
    await app.set_props_and_fire("agent-gesture-home-mac:term-1", {}, "long_press_start")
    await app.wait_for("agent-rename-home-mac:term-1")
    await app.set_props_and_fire("agent-rename-home-mac:term-1", {}, "click")


async def render_rename(
    width: int,
    transport: RenameTransport,
    *,
    dark: bool = True,
    name: str = "web",
    typed: str | None = "release-review",
) -> tuple[HostApp, list[str]]:
    selected: list[str] = []

    @ft.component
    def Parent() -> Any:
        initial = _state()
        agent = initial.inventory.agents["home-mac:term-1"]
        initial = replace(
            initial,
            inventory=replace(
                initial.inventory, agents={"home-mac:term-1": replace(agent, name=name)}
            ),
        )
        state, set_state = ft.use_state(initial)
        foreground, set_foreground = ft.use_state(True)

        def change_epoch() -> None:
            host = state.inventory.hosts["home-mac"]
            set_state(
                replace(
                    state,
                    inventory=replace(
                        state.inventory, hosts={host.id: replace(host, epoch="replacement")}
                    ),
                )
            )

        return ft.Column(
            controls=[
                ft.TextButton(
                    "Revoke",
                    key="test-revoke",
                    on_click=lambda e: set_state(replace(state, revoked=True)),
                ),
                ft.TextButton("Replace", key="test-replace", on_click=lambda e: change_epoch()),
                ft.TextButton(
                    "Reconnect",
                    key="test-reconnect",
                    on_click=lambda e: set_state(
                        replace(
                            state,
                            connection=replace(
                                state.connection, state=ConnectionState.RECONNECTING
                            ),
                        )
                    ),
                ),
                ft.TextButton("Hide", key="test-hide", on_click=lambda e: set_foreground(False)),
                ft.TextButton("Show", key="test-show", on_click=lambda e: set_foreground(True)),
                AppShell(
                    state=state,
                    is_wide=width >= 1100,
                    viewport_width=width,
                    destination="agents",
                    command_transport=transport,
                    foreground=foreground,
                    dark=dark,
                    on_destination=lambda value: None,
                    on_select_agent=selected.append,
                    on_select_shell=lambda value: None,
                    on_select_host=lambda value: None,
                    on_banner_action=lambda: None,
                    on_open_terminal=lambda: selected.append("attach"),
                    on_start_agent=lambda: None,
                    on_new_terminal=lambda: None,
                ),
            ]
        )

    app = await render_component(Parent)
    await open_rename(app)
    if typed is not None:
        await app.set_props_and_fire("agent-rename-name", {"value": typed}, "change", typed)
    return app, selected


@pytest.mark.parametrize("reconnect", ["test-reconnect", "test-replace"])
def test_rename_success_after_reconnect_settles_retained_dialog(reconnect: str) -> None:
    class Transport(RenameTransport):
        def __init__(self) -> None:
            super().__init__()
            self.status_release = asyncio.Event()

        async def command_status(self, host: str, command_id: str) -> dict[str, Any]:
            self.reads += 1
            await self.status_release.wait()
            return self.record(self.submitted[-1], "succeeded")

    async def scenario() -> None:
        transport = Transport()
        app, selected = await render_rename(390, transport)
        dialog = app.find("agent-rename-dialog")
        await app.set_props_and_fire("agent-rename-save", {}, "click")
        assert app.find("agent-rename-status").value == "Waiting for the host outcome…"
        await app.set_props_and_fire(reconnect, {}, "click")
        assert app.find("agent-rename-dialog")._i == dialog._i
        transport.status_release.set()
        await app.settle()
        await close_reported(app)
        assert len(transport.submitted) == 1 and transport.reads == 1 and not selected

    run(scenario())


def test_rename_status_retries_transient_disconnect_without_resubmitting() -> None:
    class Transport(RenameTransport):
        async def command_status(self, host: str, command_id: str) -> dict[str, Any]:
            self.reads += 1
            if self.reads == 1:
                raise TransportError("host_offline", "unavailable", retryable=True)
            return self.record(self.submitted[-1], "succeeded")

    async def scenario() -> None:
        transport = Transport()
        app, selected = await render_rename(390, transport)
        await app.set_props_and_fire("agent-rename-save", {}, "click")
        await asyncio.sleep(0.25)
        await app.settle()
        with pytest.raises(KeyError):
            app.find("agent-rename-dialog")
        assert transport.reads == 2 and len(transport.submitted) == 1 and not selected

    run(scenario())


def test_repeated_status_disconnect_stops_at_observation_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from relaydeck.client.features.composer.observation import CommandObserver
    from relaydeck.client.features.inventory import rename

    monkeypatch.setattr(
        rename, "CommandObserver", lambda status: CommandObserver(status, budget_s=0.05)
    )

    class Transport(RenameTransport):
        async def command_status(self, host: str, command_id: str) -> dict[str, Any]:
            self.reads += 1
            raise TransportError("host_offline", "unavailable", retryable=True)

    async def scenario() -> None:
        transport = Transport()
        app, _ = await render_rename(390, transport)
        await app.set_props_and_fire("agent-rename-save", {}, "click")
        assert app.find("agent-rename-status").value == "Outcome unknown."
        assert not app.find("agent-rename-save").disabled
        assert app.find("agent-rename-check").visible
        assert len(transport.submitted) == 1 and transport.reads == 1

    run(scenario())


def test_terminal_post_outcome_closes_without_another_read() -> None:
    class Transport(RenameTransport):
        async def submit_command(self, command: dict[str, Any]) -> dict[str, Any]:
            self.submitted.append(command)
            return self.record(command, "succeeded")

    async def scenario() -> None:
        transport = Transport()
        app, _ = await render_rename(390, transport)
        await app.set_props_and_fire("agent-rename-save", {}, "click")
        await close_reported(app)
        assert len(transport.submitted) == 1 and transport.reads == 0

    run(scenario())


def test_background_completion_requires_explicit_status_without_stuck_waiting() -> None:
    async def scenario() -> None:
        transport = RenameTransport()
        transport.release.clear()
        app, _ = await render_rename(390, transport)
        await app.set_props_and_fire("agent-rename-save", {}, "click")
        await app.set_props_and_fire("test-hide", {}, "click")
        transport.release.set()
        await app.settle()
        await app.set_props_and_fire("test-show", {}, "click")
        assert app.find("agent-rename-status").value == "Outcome unknown."
        assert app.find("agent-rename-check").visible and transport.reads == 0
        await app.set_props_and_fire("agent-rename-check", {}, "click")
        await close_reported(app)
        assert len(transport.submitted) == 1 and transport.reads == 1

    run(scenario())


@pytest.mark.parametrize("width", [390, 1280])
def test_root_status_observer_survives_sync_rebuild_with_real_pending_store(width: int) -> None:
    async def scenario() -> None:
        commands = PendingCommands(
            MemoryCommandStore(), "https://gateway.invalid", "synthetic", "device-a"
        )
        submitted: list[dict[str, Any]] = []
        records = RenameTransport()
        release = asyncio.Event()

        async def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "POST":
                submitted.append(json.loads(request.content))
                return httpx.Response(202, json=records.record(submitted[-1], "accepted"))
            await release.wait()
            return httpx.Response(200, json=records.record(submitted[-1], "succeeded"))

        async def token() -> str:
            return "synthetic-token"

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            http = HttpxGatewayTransport(
                "https://gateway.invalid", token, client, commands=commands
            )
            transport = _StubTransport()
            transport.submit_command = http.submit_command
            transport.command_status = http.command_status
            controller = _StubController()
            sessions = make_scripted_sessions()
            app = await render_component(
                Root,
                controller=controller,
                transport=transport,
                host_id="home-mac",
                bridge=RouteBridge(route="/agents", width=width),
                go=lambda route: None,
                session_factory=sessions.factory,
                account="synthetic",
                pending_commands=commands,
            )
            state = _state()
            controller.push(state)
            await app.settle()
            await open_rename(app)
            identifier = app.find("agent-rename-dialog")._i
            await app.set_props_and_fire(
                "agent-rename-name", {"value": "release-review"}, "change", "release-review"
            )
            await app.set_props_and_fire("agent-rename-save", {}, "click")
            controller.push(
                replace(
                    state, connection=replace(state.connection, state=ConnectionState.RECONNECTING)
                )
            )
            await app.settle()
            assert app.find("agent-rename-dialog")._i == identifier
            release.set()
            await app.settle()
            with pytest.raises(KeyError):
                app.find("agent-rename-dialog")
            assert len(submitted) == 1 and not commands.records
            assert not sessions.factory.created and not sessions.streams.opened

    run(scenario())


@pytest.mark.parametrize("name", ["8", "Build Helper"])
def test_unchanged_prefill_cannot_be_saved_and_invalid_names_show_why(name: str) -> None:
    async def scenario() -> None:
        transport = RenameTransport()
        app, _ = await render_rename(390, transport, name=name, typed=None)
        field = app.find("agent-rename-name")
        assert field.value == name and field.error is None
        assert app.find("agent-rename-save").disabled
        for text, why in (("", "empty"), ("a\nb", "control")):
            await app.set_props_and_fire("agent-rename-name", {"value": text}, "change", text)
            assert why in app.find("agent-rename-name").error.lower().replace("can't", "")
            assert app.find("agent-rename-save").disabled
        await app.set_props_and_fire("agent-rename-name", {}, "submit")
        assert not transport.submitted
        await app.set_props_and_fire("agent-rename-name", {"value": "a_1"}, "change", "a_1")
        assert app.find("agent-rename-name").error is None
        assert not app.find("agent-rename-save").disabled

    run(scenario())


@pytest.mark.parametrize("width", [390, 1280])
def test_cards_have_no_more_button_and_the_menu_does_not_select(width: int) -> None:
    async def scenario() -> None:
        transport = RenameTransport()
        app, selected = await render_rename(width, transport, typed=None)
        await app.set_props_and_fire("agent-rename-cancel", {}, "click")
        await close_reported(app)
        tile = app.find("agent-home-mac:term-1")
        controls = list(iter_controls([tile]))
        assert not any(isinstance(c, ft.PopupMenuButton) for c in controls)
        assert not any(getattr(c, "icon", None) == ft.Icons.MORE_VERT for c in controls)
        with pytest.raises(KeyError):
            app.find("agent-actions-home-mac:term-1")
        await app.set_props_and_fire("agent-gesture-home-mac:term-1", {}, "long_press_start")
        assert [await app.wait_for(f"agent-{kind}-home-mac:term-1") for kind in ("rename", "open")]
        assert not selected
        await app.set_props_and_fire("agent-rename-home-mac:term-1", {}, "click")
        assert app.find("agent-rename-dialog").open and not selected

    run(scenario())


@pytest.mark.parametrize("width", [390, 1280])
def test_a_closing_menu_never_closes_a_newer_opening(width: int) -> None:
    async def scenario() -> None:
        app, _selected = await render_rename(width, RenameTransport(), typed=None)
        await app.set_props_and_fire("agent-rename-cancel", {}, "click")
        await close_reported(app)
        # The first menu is still waiting for its close report when the card is pressed again.
        await app.set_props_and_fire("agent-gesture-home-mac:term-1", {}, "long_press_start")
        await asyncio.sleep(CLOSE_FALLBACK_S * 2)
        assert app.find("agent-rename-home-mac:term-1")

    run(scenario())


def test_start_name_validates_inline_without_changing_optional_default() -> None:
    async def scenario() -> None:
        submitted: list[AgentDraft] = []
        app = await render_component(
            StartAgentSheet,
            hosts=(("home-mac", "Home"),),
            workspaces_by_host={"home-mac": (("w1", "Workspace"),)},
            kinds=("tool-a",),
            submitting=False,
            error="",
            command_status="",
            on_submit=lambda draft, argv: submitted.append(draft),
            on_cancel=lambda: None,
        )
        field = app.find("agent-name")
        assert field.max_length == 32 and field.helper
        assert field.value == "" and field.error is None
        for invalid in ("Build Helper", "8", "-x", "a" * 33, "é", " a"):
            await app.set_props_and_fire("agent-name", {"value": invalid}, "change", invalid)
            assert app.find("agent-name").error
            assert app.find("sheet-submit").disabled
            await app.set_props_and_fire("sheet-submit", {}, "click")
            assert not submitted
        await app.set_props_and_fire("agent-name", {"value": "a_1"}, "change", "a_1")
        assert app.find("agent-name").error is None and not app.find("sheet-submit").disabled
        await app.set_props_and_fire("sheet-submit", {}, "click")
        assert submitted[0].name == "a_1"
        assert not validate_agent(
            submitted[0],
            host_ids=frozenset({"home-mac"}),
            workspace_ids_by_host={"home-mac": frozenset({"w1"})},
            kinds=("tool-a",),
        )

    run(scenario())


@pytest.mark.parametrize(
    "code,message",
    [
        ("invalid_agent_name", "Herdr refused this name"),
        ("agent_name_taken", "Another agent already uses this name."),
        ("unknown_refusal", "The Mac refused the rename. Refresh before trying again."),
    ],
)
def test_rename_failure_is_plain_and_never_renders_raw_kind(code: str, message: str) -> None:
    class Transport(RenameTransport):
        async def command_status(self, host: str, command_id: str) -> dict[str, Any]:
            return {**self.record(self.submitted[-1], "failed"), "error_code": code}

    async def scenario() -> None:
        app, _ = await render_rename(390, Transport())
        await app.set_props_and_fire("agent-rename-save", {}, "click")
        assert app.find("agent-rename-name").error == message
        assert "agent.rename" not in app.find("agent-rename-status").value

    run(scenario())


@pytest.mark.parametrize("width", [390, 1280])
@pytest.mark.parametrize("dark", [True, False])
def test_rename_dialog_prefill_validation_and_single_validated_dispatch(
    width: int, dark: bool
) -> None:
    async def scenario() -> None:
        transport = RenameTransport()
        transport.release.clear()
        app, selected = await render_rename(width, transport, dark=dark, typed=None)
        assert app.find("agent-rename-dialog").open
        assert app.find("agent-rename-name").value == "web" and not selected
        for invalid in ("", "   ", "a\nb"):
            await app.set_props_and_fire("agent-rename-name", {"value": invalid}, "change", invalid)
            await app.set_props_and_fire("agent-rename-save", {}, "click")
            assert app.find("agent-rename-name").error and not transport.submitted
        await app.set_props_and_fire("agent-rename-name", {"value": "x" * 40}, "change", "x" * 40)
        assert app.find("agent-rename-name").value == "x" * 32
        await app.set_props_and_fire(
            "agent-rename-name", {"value": "release-review"}, "change", "release-review"
        )
        await app.set_props_and_fire("agent-rename-save", {}, "click")
        assert app.find("agent-rename-save").disabled
        assert app.find("agent-rename-name").disabled
        await app.set_props_and_fire("agent-rename-save", {}, "click")
        assert len(transport.submitted) == 1
        command = transport.submitted[0]
        assert command["kind"] == "agent.rename" and command["host_epoch"] == "epoch-7"
        assert command["target"] == {"agent_id": "term-1"}
        assert command["payload"] == {"name": "release-review"}
        transport.release.set()
        await app.settle()
        with pytest.raises(KeyError):
            app.find("agent-rename-dialog")
        assert transport.reads == 1 and not selected

    run(scenario())


@pytest.mark.parametrize("width", [390, 1280])
def test_root_menu_uses_http_command_and_real_pending_store_without_terminal_open(
    width: int,
) -> None:
    async def scenario() -> None:
        commands = PendingCommands(
            MemoryCommandStore(), "https://gateway.invalid", "synthetic", "device-a"
        )
        submitted: list[dict[str, Any]] = []
        requests: list[tuple[str, str]] = []
        records = RenameTransport()

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append((request.method, request.url.path))
            assert request.headers["authorization"] == "Bearer synthetic-token"
            if request.method == "POST":
                submitted.append(json.loads(request.content))
                assert (
                    commands.records[("home-mac", submitted[-1]["command_id"])]["outcome"]
                    == "pending"
                )
                return httpx.Response(202, json=records.record(submitted[-1], "accepted"))
            return httpx.Response(200, json=records.record(submitted[-1], "succeeded"))

        async def token() -> str:
            return "synthetic-token"

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            http = HttpxGatewayTransport(
                "https://gateway.invalid", token, client, commands=commands
            )
            transport = _StubTransport()
            transport.submit_command = http.submit_command
            transport.command_status = http.command_status
            controller = _StubController()
            sessions = make_scripted_sessions()
            app = await render_component(
                Root,
                controller=controller,
                transport=transport,
                host_id="home-mac",
                bridge=RouteBridge(route="/agents", width=width),
                go=lambda route: None,
                session_factory=sessions.factory,
                account="synthetic",
                pending_commands=commands,
            )
            controller.push(_state())
            await app.settle()
            await open_rename(app)
            assert app.find("agent-rename-name").value == "web"
            await app.set_props_and_fire(
                "agent-rename-name", {"value": "release-review"}, "change", "release-review"
            )
            await app.set_props_and_fire("agent-rename-save", {}, "click")
            assert len(submitted) == 1 and submitted[0]["kind"] == "agent.rename"
            assert submitted[0]["payload"] == {"name": "release-review"}
            assert submitted[0]["target"] == {"agent_id": "term-1"}
            assert requests == [
                ("POST", "/v1/hosts/home-mac/commands"),
                ("GET", f"/v1/hosts/home-mac/commands/{submitted[0]['command_id']}"),
            ]
            assert (
                not commands.records
                and not sessions.factory.created
                and not sessions.streams.opened
            )
            await close_reported(app)

    run(scenario())


@pytest.mark.parametrize("action", ["test-replace", "test-hide", "test-revoke"])
def test_rename_inflight_completion_cannot_cross_identity_or_visibility(action: str) -> None:
    async def scenario() -> None:
        transport = RenameTransport()
        transport.release.clear()
        app, selected = await render_rename(390, transport)
        await app.set_props_and_fire("agent-rename-save", {}, "click")
        assert len(transport.submitted) == 1
        await app.set_props_and_fire(action, {}, "click")
        transport.release.set()
        await app.settle()
        assert transport.reads == 0 and not selected
        assert app.find("agent-rename-save").disabled
        await app.set_props_and_fire("agent-rename-save", {}, "click")
        assert len(transport.submitted) == 1

    run(scenario())


def test_rename_rejects_mismatched_status_without_replay() -> None:
    class Transport(RenameTransport):
        async def command_status(self, host: str, command_id: str) -> dict[str, Any]:
            return {
                **await super().command_status(host, command_id),
                "target": {"agent_id": "other"},
            }

    async def scenario() -> None:
        transport = Transport()
        app, selected = await render_rename(390, transport)
        await app.set_props_and_fire("agent-rename-save", {}, "click")
        assert app.find("agent-rename-status").value == "Outcome unknown."
        assert app.find("agent-rename-check").visible and not selected
        assert len(transport.submitted) == 1

    run(scenario())


def test_unknown_rename_only_sends_a_new_intent_on_explicit_click() -> None:
    async def scenario() -> None:
        transport = RenameTransport()
        transport.status = "unknown"
        app, selected = await render_rename(390, transport)
        await app.set_props_and_fire("agent-rename-save", {}, "click")
        await app.settle()
        assert len(transport.submitted) == 1
        assert button_label(app.find("agent-rename-save")) == "Rename again"
        transport.status = "succeeded"
        await app.set_props_and_fire("agent-rename-save", {}, "click")
        assert len(transport.submitted) == 2 and not selected
        first, second = transport.submitted
        assert first["command_id"] != second["command_id"]
        assert first["target"] == second["target"] and first["payload"] == second["payload"]

    run(scenario())


@pytest.mark.parametrize("status", ["failed", "unknown"])
def test_rename_outcome_and_check_status_never_replay(status: str) -> None:
    async def scenario() -> None:
        transport = RenameTransport()
        transport.status = status
        app, selected = await render_rename(390, transport)
        await app.set_props_and_fire("agent-rename-save", {}, "click")
        assert app.find("agent-rename-name").error
        if status == "unknown":
            assert app.find("agent-rename-check").visible
            assert not app.find("agent-rename-save").disabled
            transport.status = "succeeded"
            await app.set_props_and_fire("agent-rename-check", {}, "click")
            assert transport.reads == 2
        else:
            assert app.find("agent-rename-status").value == "Host refused the rename."
            await app.set_props_and_fire("agent-rename-cancel", {}, "click")
        assert len(transport.submitted) == 1 and not selected
        await close_reported(app)

    run(scenario())


@pytest.mark.parametrize("action", ["cancel", "revoke", "transport_failure"])
def test_rename_cancel_revocation_and_uncertain_delivery(action: str) -> None:
    async def scenario() -> None:
        transport = RenameTransport()
        app, selected = await render_rename(390, transport)
        if action == "cancel":
            await app.set_props_and_fire("agent-rename-cancel", {}, "click")
        elif action == "revoke":
            await app.set_props_and_fire("test-revoke", {}, "click")
            await app.set_props_and_fire("agent-rename-save", {}, "click")
        else:
            transport.failure = True
            await app.set_props_and_fire("agent-rename-save", {}, "click")
            assert app.find("agent-rename-check").visible
            await app.settle()
        assert len(transport.submitted) == int(action == "transport_failure")
        assert not selected

    run(scenario())
