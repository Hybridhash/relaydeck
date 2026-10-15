"""Real HTTP/journals/link/creation observer over explicit fixture sources."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import flet as ft
import httpx
import pytest

from relaydeck.client.app import Root
from relaydeck.client.features.composer.observation import CommandOutcomeUnknown, CommandRefused
from relaydeck.client.features.create.commands import agent_start_command, shell_create_command
from relaydeck.client.features.create.components import NewTerminalSheet, StartAgentSheet
from relaydeck.client.features.create.observation import CreationObserver
from relaydeck.client.features.create.validation import parse_argv
from relaydeck.client.features.inventory.routing import RouteBridge
from relaydeck.client.features.shell.ops import HttpxShellGatewayOps
from relaydeck.client.features.sync.controller import SyncController
from relaydeck.client.features.sync.transport import HttpxGatewayTransport, TransportError
from relaydeck.client.sessions import SessionFactory
from relaydeck.connector.fixture import FixtureAdapter
from relaydeck.connector.herdr import HerdrError
from relaydeck.connector.link import ConnectorLink, LinkConfig
from relaydeck.contracts.launch import default_agent_name, public_command_result
from relaydeck.contracts.schemas import validate
from relaydeck.gateway.app import create_app
from relaydeck.gateway.backend import BackendError
from relaydeck.gateway.production import MAX_OUTBOX_PER_HOST, JournalBackend
from tests.security.helpers import mint_token
from tests.support.asgi_socket import AsgiSocket
from tests.support.gateway.asgi_terminal import AsgiTerminal
from tests.support.host_harness import render_component, run
from tests.support.sync.transport_fixtures import _state, _wait


class LaunchSource(FixtureAdapter):
    def __init__(self, fault: str = "") -> None:
        super().__init__()
        self.fault = fault
        self.initializing = 2 if fault == "busy" else 0

    async def agent_start(
        self,
        name: str,
        kind: str,
        pane_id: str,
        args: list[str] | None = None,
        timeout_ms: int | None = None,
    ) -> dict[str, Any]:
        if self.initializing:
            self.initializing -= 1
            raise HerdrError("agent_pane_busy", "synthetic shell initialization")
        if self.fault == "refused":
            raise HerdrError("agent_not_ready", "synthetic pre-agent refusal")
        result = await super().agent_start(name, kind, pane_id, args, timeout_ms)
        if self.fault == "unknown":
            raise TimeoutError("synthetic result lost after creation")
        return result


@pytest.mark.parametrize(
    "kind,new_workspace,fault",
    [
        ("agent", False, ""),
        ("agent", True, ""),
        ("agent", False, "busy"),
        ("agent", True, "busy"),
        ("shell", False, ""),
        ("shell", True, ""),
        ("agent", True, "refused"),
        ("agent", True, "unknown"),
    ],
)
def test_creation_observes_returned_resource_and_never_replays_partial_launch(
    tmp_path: Path,
    kind: str,
    new_workspace: bool,
    fault: str,
) -> None:
    async def scenario() -> None:
        state, backend, _ = _state(tmp_path)
        now = int(time.time() * 1000)
        state.now_ms = backend.now_ms = lambda: now
        source = LaunchSource(fault)
        app = create_app(state)
        connector_token = state.hosts.enroll("home-mac", account="acct-1")
        token = mint_token(state, now=now)

        async def get_token() -> str:
            return token

        async def opener() -> Any:
            return await AsgiSocket(app, "/v1/connectors", token=connector_token).open()

        link = ConnectorLink(
            LinkConfig(
                "http://127.0.0.1:8125",
                "home-mac",
                connector_token,
                journal_path=str(tmp_path / "connector.db"),
                clock=lambda: now,
            ),
            source,
            opener=opener,
        )
        stop = asyncio.Event()
        task = asyncio.create_task(link.run(stop))
        http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gw")
        transport = HttpxGatewayTransport("http://gw", get_token, http)
        try:
            await _wait(lambda: state.sockets.host_count("home-mac") == 1)
            await _wait(lambda: backend.snapshots["home-mac"]["host"]["version"] == "0.9.1-fixture")
            snap = await transport.snapshot("home-mac")
            args: dict[str, Any] = {
                "command_id": "cmd-synthetic-launch-001",
                "host_id": "home-mac",
                "host_epoch": snap["host"]["epoch"],
                "workspace_id": snap["workspaces"][0]["id"],
                "new_workspace": {"directory": "/synthetic/project", "label": "new-project"}
                if new_workspace
                else None,
            }
            body = (
                agent_start_command(
                    **args,
                    kind="agent-a",
                    name="",
                    existing_names=frozenset({"agent-a", "agent-a-2"}),
                    argv=[""],
                )
                if kind == "agent"
                else shell_create_command(**args, label="new-shell")
            )
            accepted = await transport.submit_command(body)
            assert accepted["status"] == "accepted"
            observer = CreationObserver(
                transport.command_status,
                transport.snapshot,
                command_budget_s=3,
                interval_s=0.01,
                visibility_s=1,
            )
            if fault == "refused":
                with pytest.raises(CommandRefused) as exc:
                    await observer.observe(body)
                assert exc.value.record is not None
                partial = exc.value.record["result"]
                assert partial["type"] == "partial_creation"
                assert partial["workspace_id"] in source._workspaces
                assert source._agents == {}
            elif fault == "unknown":
                with pytest.raises(CommandOutcomeUnknown):
                    await observer.observe(body)
                assert len(source._agents) == 1  # Effect happened; no result proof is fabricated.
            else:
                resource = await observer.observe(body)
                assert resource.kind == kind and resource.host_id == "home-mac"
                assert resource.snapshot is not None
                validate("relaydeck/gateway/v1/snapshot", resource.snapshot)
                controller = SyncController(transport)
                original_connection = controller.state.connection
                controller.accept_snapshot(resource.host_id, resource.snapshot)
                assert controller.state.connection is original_connection
                if kind == "agent":
                    assert next(iter(source._agents.values()))["name"] == "agent-a-3"
                    assert resource.resource_id in source._agents
                else:
                    assert resource.resource_id in source._tabs
                    assert source._tabs[resource.resource_id]["label"] == "new-shell"
                    assert not source._agents
            record = await transport.command_status("home-mac", body["command_id"])
            validate("relaydeck/gateway/v1/command-record", record)
            assert record is not None
            assert record["status"] == (
                "failed" if fault == "refused" else "unknown" if fault == "unknown" else "succeeded"
            )
            calls = source.calls
            await transport.submit_command(body)
            await asyncio.sleep(0.1)
            assert source.calls == calls
            assert sum(method == "workspace.create" for method, _ in calls) == int(new_workspace)
            assert sum(method == "tab.create" for method, _ in calls) == int(not new_workspace)
            assert sum(method == "agent.start" for method, _ in calls) == int(
                kind == "agent" and fault != "refused"
            )
            assert "/synthetic/project" not in json.dumps(record)
        finally:
            stop.set()
            await asyncio.wait_for(task, 3)
            await http.aclose()
            backend.journal.close()
            state.push.close()

    asyncio.run(scenario())


def test_pinned_default_name_and_native_argument_rules() -> None:
    assert default_agent_name("agent-a", {"agent-a", "agent-a-2", "agent-a-4"}) == "agent-a-3"
    assert parse_argv("—model “two words” '' \"x\\q\"") == (
        ["--model", "two words", "", "xq"],
        None,
    )
    assert parse_argv('"one\x00two"')[1] is not None
    assert parse_argv('"one\u0085two"')[1] is not None
    assert parse_argv('"one\u200btwo"')[1] is not None
    assert parse_argv("unfinished\\")[1] is not None
    assert public_command_result(
        {"type": "agent_started", "agent_id": "a", "terminal_text": "secret"}
    ) == {"type": "agent_started", "agent_id": "a"}
    assert public_command_result({"type": "agent_started", "agent_id": "a\nsecret"}) is None


def test_outbox_refusal_is_not_durable_or_recovered(tmp_path: Path) -> None:
    state, backend, _ = _state(tmp_path)
    backend._outbox["home-mac"] = [{} for _ in range(MAX_OUTBOX_PER_HOST)]
    body = shell_create_command(
        command_id="cmd-refused-capacity-001",
        host_id="home-mac",
        host_epoch="epoch-7",
        workspace_id="w1",
    )
    with pytest.raises(BackendError) as exc:
        backend.submit_command(body)
    assert exc.value.code == "rate_limited"
    assert backend.journal.record(body["command_id"]) is None
    recovered = JournalBackend(journal=backend.journal)
    assert recovered.drain_outbox("home-mac") == []
    backend.close()
    state.push.close()


@pytest.mark.parametrize(
    "field,value",
    [
        ("name", "Bad-Name"),
        ("name", "a" * 33),
        ("kind", "Unverified Provider"),
        ("argv", ["unsafe\x00argument"]),
        ("argv", ["unsafe\u200bargument"]),
        ("new_workspace", {"directory": "relative"}),
        ("new_workspace", {"directory": "/safe", "execute": "malicious"}),
        ("new_workspace", {"label": "bad\nsplit"}),
    ],
)
def test_launch_policy_refuses_invalid_intent_before_durable_acceptance(
    tmp_path: Path, field: str, value: Any
) -> None:
    async def scenario() -> None:
        state, backend, _ = _state(tmp_path)
        now = int(time.time() * 1000)
        state.now_ms = backend.now_ms = lambda: now
        token = mint_token(state, now=now)

        async def get_token() -> str:
            return token

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(state)), base_url="http://gw"
        ) as http:
            transport = HttpxGatewayTransport("http://gw", get_token, http)
            body = agent_start_command(
                command_id="cmd-invalid-launch-001",
                host_id="home-mac",
                host_epoch="epoch-7",
                workspace_id="",
                kind="agent-a",
                name="agent-a",
                argv=[],
                new_workspace={"directory": "/synthetic"},
            )
            body["payload"][field] = value
            with pytest.raises(TransportError) as exc:
                await transport.submit_command(body)
            assert exc.value.code == "forbidden"
            assert backend.journal.record(body["command_id"]) is None
            assert backend.drain_outbox("home-mac") == []
        backend.close()
        state.push.close()

    run(scenario())


@pytest.mark.parametrize("fault", ["", "refused", "unknown"])
def test_root_launch_observes_http_outcome_and_fences_duplicate_clicks(
    tmp_path: Path, fault: str
) -> None:
    async def scenario() -> None:
        state, backend, _ = _state(tmp_path)
        now = int(time.time() * 1000)
        state.now_ms = backend.now_ms = lambda: now
        source = LaunchSource(fault)
        gateway = create_app(state)
        connector_token = state.hosts.enroll("home-mac", account="acct-1")
        token = mint_token(state, now=now)

        async def get_token() -> str:
            return token

        async def opener() -> Any:
            return await AsgiSocket(gateway, "/v1/connectors", token=connector_token).open()

        link = ConnectorLink(
            LinkConfig(
                "http://127.0.0.1:8125",
                "home-mac",
                connector_token,
                journal_path=str(tmp_path / "root-connector.db"),
                clock=lambda: now,
            ),
            source,
            opener=opener,
        )
        stop = asyncio.Event()
        link_task = asyncio.create_task(link.run(stop))
        http = httpx.AsyncClient(transport=httpx.ASGITransport(app=gateway), base_url="http://gw")

        class RootTransport(HttpxGatewayTransport):
            submitted: list[dict[str, Any]] = []

            async def submit_command(self, command: dict[str, Any]) -> dict[str, Any]:
                self.submitted.append(command)
                return await super().submit_command(command)

            async def stream(self, host_id: str, cursor: str) -> Any:
                # Explicit metadata-stream double only; httpx ASGI buffers
                # indefinite SSE. All inventory/command operations use HTTP.
                await asyncio.Future()
                yield None

        transport = RootTransport("http://gw", get_token, http)
        from relaydeck.client.adapters.fake import FakeCredentialStore
        from relaydeck.client.pending_commands import PendingCommands

        transport.commands = PendingCommands(
            FakeCredentialStore(), "http://gw", "acct-1", "synthetic-device"
        )
        controller = SyncController(transport)
        bridge = RouteBridge(route="/", width=1280)
        routes: list[str] = []
        attachments: list[str] = []

        def open_stream(lease: str, ticket: str) -> AsgiTerminal:
            attachments.append(lease)
            return AsgiTerminal(gateway, lease, ticket)

        factory = SessionFactory(
            ops=HttpxShellGatewayOps("http://gw", get_token, http),
            open_stream=open_stream,
            submit_command=transport.submit_command,
            prompt_text=None,
            renew_lease=None,
            clipboard=None,
            device_id="synthetic-device",
        )

        @ft.component
        def Parent() -> Any:
            mounted, set_mounted = ft.use_state(True)
            return ft.Column(
                controls=[
                    Root(
                        controller=controller,
                        transport=transport,
                        host_id="home-mac",
                        bridge=bridge,
                        go=routes.append,
                        session_factory=factory,
                        account="acct-1",
                        pending_commands=transport.commands,
                    )
                    if mounted
                    else ft.Text("Closed"),
                    ft.Button("Unmount", key="test-unmount", on_click=lambda e: set_mounted(False)),
                ]
            )

        app = None
        try:
            await _wait(lambda: state.sockets.host_count("home-mac") == 1)
            await _wait(lambda: backend.snapshots["home-mac"]["host"]["version"] == "0.9.1-fixture")
            app = await render_component(Parent)
            await _wait(lambda: controller.state.inventory.hosts.get("home-mac") is not None)
            await app.settle()
            await app.set_props_and_fire("console-new-agent", {}, "click")
            await app.set_props_and_fire("sheet-cancel", {}, "click")
            assert source.calls == () and transport.submitted == []
            await app.set_props_and_fire("console-new-agent", {}, "click")
            await app.set_props_and_fire("agent-workspace-mode", {"value": "new"}, "select", "new")
            await app.set_props_and_fire(
                "agent-workspace-directory",
                {"value": "/synthetic/project"},
                "change",
                "/synthetic/project",
            )
            await app.set_props_and_fire("agent-argv", {"value": "''"}, "change", "''")
            click = app.find("sheet-submit").on_click
            click(None)
            click(None)
            await _wait(lambda: len(transport.submitted) == 1)
            await _wait(
                lambda: (backend.journal.record(transport.submitted[0]["command_id"]) or {})[
                    "status"
                ]
                in ("succeeded", "failed", "unknown")
            )
            await app.settle(50)
            if fault:
                assert app.find("agent-workspace-directory").value == "/synthetic/project"
                assert app.find("agent-argv").value == "''"
                assert not routes
                if fault == "refused":
                    assert "already created" in app.find("sheet-error").value
                else:
                    assert "do not retry" in app.find("sheet-error").value
            else:
                assert len(routes) == 1 and routes[0].startswith("/agents/home-mac")
                assert app.find("detail-title").value == "agent-a"
            assert len(transport.submitted) == 1
            if fault:
                recovered = transport.commands.records[
                    ("home-mac", transport.submitted[0]["command_id"])
                ]
                assert recovered["outcome"] == ("failed" if fault == "refused" else "unknown")
                if fault == "refused":
                    assert recovered["created"].get("workspace_id")
            else:
                assert transport.commands.records == {}
                assert app.find("command-recovery-empty") is not None
            assert "/synthetic/project" not in str(
                await transport.commands.store.get(transport.commands.key)
            )
            calls: tuple[tuple[str, dict[str, Any]], ...] = source.calls
            assert sum(call[0] == "workspace.create" for call in calls) == 1
            assert sum(call[0] == "tab.create" for call in calls) == 0
            assert attachments == []  # Creation never silently takes over a PTY.
        finally:
            if app is not None:
                await app.set_props_and_fire("test-unmount", {}, "click")
            stop.set()
            await asyncio.wait_for(link_task, 3)
            await http.aclose()
            backend.close()
            state.push.close()

    run(scenario())


@pytest.mark.parametrize(
    "component,prefix", [(StartAgentSheet, "agent"), (NewTerminalSheet, "shell")]
)
def test_declarative_new_workspace_form_preserves_optional_fields_without_existing_project(
    component: Any,
    prefix: str,
) -> None:
    async def scenario() -> None:
        submitted: list[Any] = []
        props: dict[str, Any] = {
            "hosts": (("home-mac", "Home"),),
            "workspaces_by_host": {},
            "submitting": False,
            "error": "",
            "command_status": "",
            "on_submit": lambda *args: submitted.append(args),
            "on_cancel": lambda: None,
        }
        if prefix == "agent":
            props.update(
                kinds=("agent-a",),
                kind_catalogs={"home-mac": ("agent-a",)},
                existing_names_by_host={"home-mac": frozenset({"agent-a"})},
            )
        app = await render_component(component, **props)
        await app.set_props_and_fire(prefix + "-workspace-mode", {"value": "new"}, "select", "new")
        await app.set_props_and_fire(
            prefix + "-workspace-directory",
            {"value": "/synthetic/project"},
            "change",
            "/synthetic/project",
        )
        await app.set_props_and_fire(
            prefix + "-workspace-label", {"value": "project"}, "change", "project"
        )
        if prefix == "agent":
            assert app.find("agent-name").hint_text == "agent-a-2"
        await app.set_props_and_fire("sheet-submit", {}, "click")
        assert len(submitted) == 1
        draft = submitted[0][0]
        assert draft.workspace_mode == "new" and draft.workspace_id == ""
        assert (
            draft.workspace_directory == "/synthetic/project" and draft.workspace_label == "project"
        )

    run(scenario())
