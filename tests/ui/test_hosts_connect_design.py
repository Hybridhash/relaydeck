"""Actual Hosts branch, host tiles, ConnectPanel.

Real Flet 1.0.3 dispatch through AppShell (destination=hosts) with
synthetic AppState, plus direct ConnectPanel renders. Covers grouped
reachable/offline cards, selection/actions, narrow/wide layout,
long-label/empty/offline states, 48px focusable actions and Connect
callback/disabled behavior. No full suite, no devices, no servers.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import flet as ft

from relaydeck.client.app import Root
from relaydeck.client.components.appearance import AppearanceBridge
from relaydeck.client.components.command_recovery import CommandRecovery
from relaydeck.client.components.design_system.connect import (
    PHASE_CONNECTING,
    PHASE_FAILED,
    PHASE_READY,
    PHASE_SETUP,
    PHASE_WAITING,
    ConnectPanel,
    ConnectState,
    approve_command,
)
from relaydeck.client.components.design_system.hosts import HostCard
from relaydeck.client.components.design_system.tokens import MIN_TOUCH
from relaydeck.client.components.session_rows import host_tile
from relaydeck.client.features.a11y.focus import iter_controls
from relaydeck.client.features.inventory.components import AppShell
from relaydeck.client.features.inventory.routing import RouteBridge
from relaydeck.client.features.inventory.selectors import HostRow
from relaydeck.client.features.management.components import ManagementPane
from relaydeck.client.features.sync.backoff import Backoff
from relaydeck.client.features.sync.controller import SyncController
from relaydeck.client.features.sync.transport import TransportError
from relaydeck.client.features.terminal.keys import DOCK_KEYS, ToolsKeysDock, encode_key
from relaydeck.client.features.terminal.screen import SURFACE_KEY
from relaydeck.client.features.terminal.surfaces import (
    AGENT_KEYBOARD_KEYS_KEY,
    AGENT_TAKEOVER_CONFIRM_KEY,
)
from relaydeck.client.pending_commands import (
    REVIEW_GRACE_MS,
    MemoryCommandStore,
    PendingCommands,
)
from relaydeck.domain.models import (
    AppState,
    Connection,
    ConnectionState,
    Host,
    Inventory,
    Selection,
)
from tests.support.host_harness import HostApp, render_component, run
from tests.support.scripted_sessions import make_scripted_sessions
from tests.support.ui.navigation import _state as _nav_state
from tests.support.ui.open_terminal import AGENT_KEY, _TwoHostTransport


def _state(**kw: Any) -> AppState:
    hosts = {
        "mac-studio": Host(id="mac-studio", label="mac-studio", reachable=True),
        "hetzner-01": Host(id="hetzner-01", label="hetzner-01", reachable=True),
        "thinkpad-x1": Host(id="thinkpad-x1", label="thinkpad-x1", reachable=False),
    }
    state = AppState(
        connection=Connection(state=ConnectionState.LIVE, host_id="mac-studio"),
        inventory=Inventory(hosts=hosts),
        selection=Selection(host_id="mac-studio"),
    )
    if kw:
        state = replace(state, **kw)
    return state


def _noop(*args: Any) -> None:
    pass


async def _render_hosts(state: AppState, **kw: Any) -> HostApp:
    props: dict[str, Any] = {
        "state": state,
        "is_wide": True,
        "destination": "settings",
        "on_destination": _noop,
        "on_select_agent": _noop,
        "on_select_shell": _noop,
        "on_select_host": _noop,
        "on_banner_action": _noop,
        "on_open_terminal": _noop,
        "on_start_agent": _noop,
        "on_new_terminal": _noop,
        "on_back": _noop,
    }
    props.update(kw)
    return await render_component(AppShell, **props)


@ft.component
def _ConnectHarness(
    state: ConnectState,
    on_endpoint: Any,
    on_host_id: Any,
    on_label: Any,
    on_begin: Any,
    on_cancel: Any,
    on_retry: Any,
) -> Any:
    return ConnectPanel(
        state,
        on_endpoint=on_endpoint,
        on_host_id=on_host_id,
        on_label=on_label,
        on_begin=on_begin,
        on_cancel=on_cancel,
        on_retry=on_retry,
    )


def test_hosts_branch_groups_and_selects() -> None:
    async def scenario() -> None:
        calls: list[str] = []
        app = await _render_hosts(_state(), on_select_host=calls.append)
        await app.settle()
        assert app.find("hosts-header") is not None
        assert app.find("hosts-group-connected").value == "CONNECTED · 2"
        assert app.find("hosts-group-offline").value == "OFFLINE · 1"
        card = app.find("host-mac-studio")
        assert isinstance(card, ft.TextButton)
        assert card.on_click is not None
        assert card.content.shadow is not None  # selected ring
        assert app.find("host-hetzner-01").content.shadow is None
        await app.set_props_and_fire("host-thinkpad-x1", {}, "click")
        await app.settle()
        assert calls == ["thinkpad-x1"]

    run(scenario())


def test_hosts_narrow_wide_layout() -> None:
    async def scenario() -> None:
        narrow = await _render_hosts(_state(), is_wide=False, viewport_width=390)
        await narrow.settle()
        # AppShell rekeys nav to "destinations": bar is 76px tall, rail 96px wide.
        assert narrow.find("destinations").height == 76
        # The Hosts destination's list-beside-management pane is removed;
        # Hosts is a Settings section, and Settings' layout is covered by the settings tests.
        mgmt = ft.Text("MGMT", key="test-mgmt-stub")
        wide = await _render_hosts(_state(), is_wide=True, viewport_width=1440, management=mgmt)
        await wide.settle()
        assert wide.find("destinations").width == 96
        assert wide.find("test-mgmt-stub") is not None

    run(scenario())


def test_hosts_empty_offline_and_long_labels() -> None:
    async def scenario() -> None:
        empty = await _render_hosts(replace(_state(), inventory=Inventory(hosts={})))
        await empty.settle()
        assert empty.find("hosts-empty").value == "No hosts"
        offline_hosts = {
            "h1": Host(id="h1", label="h1", reachable=False),
            "h2": Host(id="h2", label="h2", reachable=False),
        }
        offline = await _render_hosts(replace(_state(), inventory=Inventory(hosts=offline_hosts)))
        await offline.settle()
        assert offline.find("hosts-group-offline").value == "OFFLINE · 2"
        try:
            offline.find("hosts-group-connected")
            raise AssertionError("connected group must not mount when empty")
        except KeyError:
            pass
        long_name = "y" * 120
        long_hosts = {"hl": Host(id="hl", label=long_name, reachable=True)}
        app = await _render_hosts(replace(_state(), inventory=Inventory(hosts=long_hosts)))
        await app.settle()
        assert app.find("host-hl").tooltip == long_name
        roots: list[Any] = [app.page.views[0].controls]
        titles = [
            c for c in iter_controls(roots) if isinstance(c, ft.Text) and c.value == long_name
        ]
        assert len(titles) == 1
        assert titles[0].no_wrap is True
        assert titles[0].overflow == ft.TextOverflow.ELLIPSIS

    run(scenario())


def test_host_tile_unit_dark_light() -> None:
    row = HostRow(key="h", title="H", subtitle="3 agents · 1 tab", reachable=True)
    calls: list[str] = []
    dark = host_tile(row, True, calls.append, True, False)
    assert isinstance(dark, ft.TextButton)
    assert dark.key == "host-h"
    assert dark.on_click is not None
    assert dark.content.shadow is not None
    offline = host_tile(
        HostRow(key="o", title="O", subtitle="0 agents", reachable=False),
        False,
        calls.append,
        True,
        False,
    )
    assert isinstance(offline, ft.TextButton)
    assert offline.content.shadow is None
    light = host_tile(row, True, calls.append, False, False)
    assert isinstance(light, ft.ListTile)
    assert light.title.value == "H"
    # Contract behind the shared card builder.
    assert HostCard("x", "X", "line", True, (), False, lambda i: None).key == "host-x"


def test_connect_panel_setup_fields_and_begin() -> None:
    async def scenario() -> None:
        calls: dict[str, list] = {}
        state = ConnectState()
        app = await render_component(
            _ConnectHarness,
            state,
            lambda v: calls.setdefault("endpoint", []).append(v),
            lambda v: calls.setdefault("host", []).append(v),
            lambda v: calls.setdefault("label", []).append(v),
            lambda: calls.setdefault("begin", []).append(True),
            lambda: calls.setdefault("cancel", []).append(True),
            lambda: calls.setdefault("retry", []).append(True),
        )
        await app.settle()
        assert app.find("connect-panel") is not None
        assert "pairing open" in app.find("connect-open-command-text").value
        await app.set_props_and_fire("connect-endpoint", {}, "change", "https://m:1")
        await app.set_props_and_fire("connect-host-id", {}, "change", "home-mac")
        await app.set_props_and_fire("connect-label", {}, "change", "pixel-8")
        await app.set_props_and_fire("connect-begin", {}, "click")
        await app.settle()
        assert calls["endpoint"] == ["https://m:1"]
        assert calls["host"] == ["home-mac"]
        assert calls["label"] == ["pixel-8"]
        assert calls["begin"] == [True]
        try:
            app.find("connect-code")
            raise AssertionError("code summary must not mount before a request")
        except KeyError:
            pass

    run(scenario())


def test_connect_panel_waiting_failed_and_busy() -> None:
    async def scenario() -> None:
        calls: dict[str, list] = {}
        waiting = ConnectState(
            phase=PHASE_WAITING,
            request_id="req-1",
            matching_code="ABC-123",
            fingerprint="SHA256:synthetic",
            scope="console + terminal (one host)",
            expiry="120s",
        )

        def render(state: ConnectState) -> Any:
            return render_component(
                _ConnectHarness,
                state,
                lambda v: None,
                lambda v: None,
                lambda v: None,
                lambda: calls.setdefault("begin", []).append(True),
                lambda: calls.setdefault("cancel", []).append(True),
                lambda: calls.setdefault("retry", []).append(True),
            )

        app = await render(waiting)
        await app.settle()
        assert app.find("connect-code").value == "ABC-123"
        assert "req-1" in app.find("connect-approve-command-text").value
        assert "ABC-123" in app.find("connect-approve-command-text").value
        assert app.find("connect-status") is not None
        await app.set_props_and_fire("connect-cancel", {}, "click")
        await app.settle()
        assert calls["cancel"] == [True]
        failed = await render(
            ConnectState(phase=PHASE_FAILED, message="Window expired.", busy=False)
        )
        await failed.settle()
        assert failed.find("connect-message").value == "Window expired."
        await failed.set_props_and_fire("connect-retry", {}, "click")
        await failed.settle()
        assert calls["retry"] == [True]
        busy = await render(ConnectState(phase=PHASE_CONNECTING, message="", busy=True))
        await busy.settle()
        assert busy.find("connect-progress") is not None
        assert busy.find("connect-cancel").disabled is True
        setup_busy = await render(ConnectState(phase=PHASE_SETUP, busy=True))
        await setup_busy.settle()
        assert setup_busy.find("connect-begin").disabled is True
        assert setup_busy.find("connect-endpoint").disabled is True

    run(scenario())


def test_connect_panel_actions_meet_48px_and_no_secrets() -> None:
    async def scenario() -> None:
        failed = ConnectState(phase=PHASE_FAILED, message="Denied.")
        app = await render_component(
            _ConnectHarness,
            failed,
            lambda v: None,
            lambda v: None,
            lambda v: None,
            lambda: None,
            lambda: None,
            lambda: None,
        )
        await app.settle()
        assert app.find("connect-retry").content.height == 52
        assert app.find("connect-cancel").content.height == 52
        assert 52 >= MIN_TOUCH
        roots: list[Any] = [app.page.views[0].controls]
        words = " ".join(
            c.value for c in iter_controls(roots) if isinstance(c, ft.Text) and c.value
        ).lower()
        for banned in ("verifier", "token", "cookie", "qr", "scan the", "plugin"):
            assert banned not in words, banned

    run(scenario())


def test_connect_ready_unknown_and_command_validation() -> None:
    async def scenario() -> None:
        def render(state: ConnectState) -> Any:
            return render_component(
                _ConnectHarness,
                state,
                lambda v: None,
                lambda v: None,
                lambda v: None,
                lambda: None,
                lambda: None,
                lambda: None,
            )

        def absent(app: HostApp, key: str) -> None:
            try:
                app.find(key)
                raise AssertionError(f"{key} must not mount")
            except KeyError:
                pass

        # Ready without a request keeps input/begin; no approval placeholders.
        bare = await render(ConnectState(phase=PHASE_READY))
        await bare.settle()
        assert bare.find("connect-endpoint") is not None
        assert bare.find("connect-begin").disabled is False
        absent(bare, "connect-approve-command")
        absent(bare, "connect-code")
        # Ready with validated ids shows the summary + approve command.
        full_state = ConnectState(phase=PHASE_READY, request_id="req-1", matching_code="ABC-123")
        full = await render(full_state)
        await full.settle()
        assert full.find("connect-code").value == "ABC-123"
        assert "req-1" in full.find("connect-approve-command-text").value
        for key, expected in (
            ("connect-open-command", full.find("connect-open-command-text").value),
            ("connect-approve-command", approve_command(full_state)),
            ("connect-code", full_state.matching_code),
            ("connect-request", full_state.request_id),
        ):
            button = full.find(f"{key}-copy")
            assert isinstance(button.action, ft.CopyToClipboard)
            assert button.action.data == expected
            assert button.on_click is None  # clipboard only; no pairing/PTY command
            assert button.height >= MIN_TOUCH
        absent(full, "connect-begin")
        # Unknown phases fail closed: visible form, disabled begin, no approval path.
        weird = await render(ConnectState(phase="launched", request_id="req-1"))
        await weird.settle()
        assert weird.find("connect-endpoint") is not None
        assert weird.find("connect-begin").disabled is True
        absent(weird, "connect-approve-command")
        absent(weird, "connect-code")
        absent(weird, "connect-retry")
        absent(weird, "connect-cancel")
        # Hostile ids are quoted, never spliced raw; the panel hides them.
        evil = ConnectState(
            phase=PHASE_WAITING, request_id='a"; rm -rf / #', matching_code="ABC-123"
        )
        cmd = approve_command(evil)
        assert "'a\"; rm -rf / #'" in cmd
        assert '--request a"' not in cmd
        evil_app = await render(evil)
        await evil_app.settle()
        absent(evil_app, "connect-approve-command")
        absent(evil_app, "connect-code")
        absent(evil_app, "connect-code-copy")
        absent(evil_app, "connect-approve-command-copy")

    run(scenario())


def test_hosts_single_heading_other_titles_intact() -> None:
    async def scenario() -> None:
        app = await _render_hosts(_state())
        await app.settle()
        assert app.find("hosts-header") is not None
        # The "outer title must not duplicate the Hosts heading" rule belonged
        # to the removed Hosts destination; the Hosts section now sits under Settings' own heading.
        other = await _render_hosts(_state(), destination="terminals")
        await other.settle()
        # Terminals owns its page heading, like Hosts.
        assert "Terminals" in {
            c.value
            for c in iter_controls([other.find("terminals-header")])
            if isinstance(c, ft.Text)
        }
        try:
            other.find("destination-title")
            raise AssertionError("outer title must not duplicate the Terminals heading")
        except KeyError:
            pass

    run(scenario())


class _EmptyTransport:
    """Available management surface whose reads fail cleanly (synthetic)."""

    async def me(self) -> dict[str, Any]:
        raise TransportError("unavailable", "stub", False)

    async def hosts(self) -> dict[str, Any]:
        raise TransportError("unavailable", "stub", False)

    async def devices(self) -> dict[str, Any]:
        raise TransportError("unavailable", "stub", False)

    async def revoke_host(self, *args: Any) -> dict[str, Any]:
        raise TransportError("unavailable", "stub", False)


def test_management_empty_state_and_disabled_reason() -> None:
    async def scenario() -> None:
        app = await render_component(
            ManagementPane,
            transport=_EmptyTransport(),
            account="synthetic",
            destination="settings",
            selected_host="",
            on_hosts=lambda rows: None,
            on_logout=lambda: None,
            dark=True,
        )
        await app.settle()
        assert (
            app.find("managed-hosts-empty").value
            == "No managed hosts loaded. Refresh to load host detail."
        )
        assert app.find("enroll-host-open").disabled is True
        assert (
            app.find("enroll-host-disabled-reason").value
            == "Enrollment needs the hosts.enroll capability."
        )

    run(scenario())


@ft.component
def _NavHarness() -> Any:
    dest, set_dest = ft.use_state("agents")
    return AppShell(
        state=_nav_state(),
        is_wide=True,
        destination=dest,
        on_destination=set_dest,
        on_select_agent=_noop,
        on_select_shell=_noop,
        on_select_host=_noop,
        on_banner_action=_noop,
        on_open_terminal=_noop,
        on_start_agent=_noop,
        on_new_terminal=_noop,
        on_back=_noop,
        viewport_width=1440,
    )


def test_collapse_is_scoped_per_destination() -> None:
    """Collapsing Console must not hide the Terminals list.

    Drives destination switches inside one mounted shell (state
    preserved, as in the app): collapse agents, visit terminals
    (list discoverable), collapse terminals, return (agents still
    collapsed), expand agents, return (terminals still collapsed).
    Also pins the shell-specific empty detail copy.
    """

    async def scenario() -> None:
        app = await render_component(_NavHarness)
        await app.settle()

        def visible(key: str) -> bool:
            return app.find(key).visible is not False

        def absent(key: str) -> None:
            try:
                app.find(key)
                raise AssertionError(f"{key} must not mount")
            except KeyError:
                pass

        assert visible("inventory-sidebar")
        absent("console-strip")
        await app.set_props_and_fire("console-collapse-toggle", {}, "click")
        await app.settle()
        assert app.find("console-strip") is not None
        # Terminals keeps its own expanded list.
        await app.set_props_and_fire("console-rail-terminals", {}, "click")
        await app.settle()
        assert visible("inventory-sidebar")
        absent("console-strip")
        assert app.find("shells-tiles") is not None
        assert app.find("detail-empty").value == "Select a shell to open its terminal."
        # Collapse terminals independently (to its strip), then return to agents.
        await app.set_props_and_fire("shells-collapse", {}, "click")
        await app.settle()
        assert app.find("shells-strip") is not None
        await app.set_props_and_fire("console-rail-console", {}, "click")
        await app.settle()
        assert app.find("console-strip") is not None
        assert app.find("detail-empty").value == "Select a session to see details."
        # Expand agents; terminals stays collapsed on return.
        await app.set_props_and_fire("sidebar-show", {}, "click")
        await app.settle()
        assert visible("inventory-sidebar")
        absent("console-strip")
        await app.set_props_and_fire("console-rail-terminals", {}, "click")
        await app.settle()
        assert app.find("shells-strip") is not None

    run(scenario())


def test_height_crossing_keeps_engine_writer_draft_and_dock() -> None:
    """Live Root across 1000/768/550/844 heights.

    Attached terminal + unsent draft + Keys dock: engine control
    identity, session/writer continuity (single attach, no
    dispose/release) and draft survive every crossing. The dock
    uses bounded flex + internal scroll below 600px and natural
    height above; keys stay mounted and reachable. Flex values
    only — no pixel-fit claim from headless structure.
    """

    async def scenario() -> None:
        sessions = make_scripted_sessions()
        transport = _TwoHostTransport()
        routes = RouteBridge(width=1440)
        app = await render_component(
            Root,
            controller=SyncController(transport=transport, backoff=Backoff.seeded(1)),
            transport=transport,
            host_id="h1",
            bridge=routes,
            go=lambda route: None,
            session_factory=sessions.factory,
            appearance=AppearanceBridge(True),
        )
        await app.settle()
        await app.set_props_and_fire(f"agent-{AGENT_KEY}", {}, "click")
        await app.settle()
        await app.set_props_and_fire(AGENT_TAKEOVER_CONFIRM_KEY, {}, "click")
        await app.settle()
        terminal = app.find(SURFACE_KEY)
        session = sessions.factory.created[0]
        await app.set_props_and_fire(
            "composer-draft",
            {"value": "unsent height draft"},
            "change",
            "unsent height draft",
        )
        await app.set_props_and_fire(AGENT_KEYBOARD_KEYS_KEY, {}, "click")
        await app.settle()
        for width, height in ((1440, 1000), (1366, 768), (1280, 550), (390, 844)):
            routes.set_size(width, height)
            await app.settle()
            assert app.find(SURFACE_KEY) is terminal
            assert session.composer_props()["draft"] == "unsent height draft"
            dock = app.find("agent-input-dock")
            assert dock.height is None
            if width < 600:
                assert dock.expand is None  # compact phone bar never shares viewport flex
                assert app.find("composer-footer") is not None
                assert app.find("composer-draft").max_lines == 2
            elif height < 600:
                # Composer stays intrinsic; the terminal takes the rest.
                assert dock.expand is None
                assert app.find("terminal-flex-host").expand is True
            else:
                assert dock.expand is None
            if width >= 600:
                assert app.find("agent-input-axes") is not None
            if width >= 1024 and height < 600:
                # Keyboard-open dock is an overlay; the overlay scrolls.
                assert app.find("agent-keys-overlay").content.scroll == ft.ScrollMode.AUTO
            elif width >= 1024:
                assert app.find("tools-grid-scroll").scroll == ft.ScrollMode.AUTO
            else:
                assert app.find("mobile-keybar").visible is False
        assert sessions.factory.created == [session]
        assert len(sessions.ops.attaches) == 1 and not session.disposed
        assert sessions.ops.releases == []

    run(scenario())


def _recovery_row(host: str, command_id: str, **kw: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "host": host,
        "id": command_id,
        "kind": "agent.prompt",
        "outcome": "accepted",
        "created": {},
    }
    row.update(kw)
    return row


def _journal_with(rows: list[dict[str, Any]]) -> PendingCommands:
    journal = PendingCommands(MemoryCommandStore(), "https://origin", "acct", "dev")
    for row in rows:
        journal.records[(row["host"], row["id"])] = row
    return journal


async def _open_recovery(journal: PendingCommands, **kw: Any) -> HostApp:
    props: dict[str, Any] = {
        "journal": journal,
        "transport": object(),
        "granted": frozenset(),
        "go": _noop,
    }
    props.update(kw)
    app = await render_component(CommandRecovery, **props)
    await app.settle()
    await app.set_props_and_fire("command-recovery-toggle", {}, "click")
    await app.settle()
    return app


def test_a_send_in_flight_is_listed_only_once_it_outlasts_the_grace_period() -> None:
    async def scenario() -> None:
        now = 1_000_000
        journal = _journal_with([_recovery_row("h1", "c1", outcome="pending", created_ms=now)])
        journal.clock = lambda: now + 1_000
        app = await render_component(
            CommandRecovery, journal=journal, transport=object(), granted=frozenset(), go=_noop
        )
        await app.settle()
        assert app.find("command-recovery-empty") is not None
        journal.clock = lambda: now + REVIEW_GRACE_MS
        journal.changed()
        await app.settle()
        assert app.find("command-recovery-toggle") is not None

    run(scenario())


def test_command_recovery_list_content_sized_then_bounded() -> None:
    """One accepted row takes no fixed 220px reservation.

    The opened review list sizes to content for 1-2 rows and only takes
    the bounded 220px scrollable box for longer queues. Outcome text,
    row keys, 48px actions and the open-resource callback are preserved.
    """

    async def scenario() -> None:
        one = await render_component(
            CommandRecovery,
            journal=_journal_with([_recovery_row("h1", "c1")]),
            transport=object(),
            granted=frozenset(),
            go=_noop,
        )
        await one.settle()
        assert one.find("command-recovery-toggle").height == 48
        assert one.find("command-recovery-check").height == 48
        try:
            one.find("command-recovery-list")
            raise AssertionError("list must not mount while closed")
        except KeyError:
            pass
        await one.set_props_and_fire("command-recovery-toggle", {}, "click")
        await one.settle()
        single = one.find("command-recovery-list")
        assert single.height is None
        assert single.scroll is None
        assert one.find("recovered-h1-c1") is not None
        assert any(
            isinstance(c, ft.Text) and c.value == "agent.prompt: accepted"
            for c in iter_controls([one.page.views[0].controls])
        )
        # Two rows still size to content; three rows bound + scroll.
        two = await _open_recovery(_journal_with([_recovery_row("h1", f"c{i}") for i in range(2)]))
        assert two.find("command-recovery-list").height is None
        assert two.find("recovered-h1-c1") is not None
        many = await _open_recovery(_journal_with([_recovery_row("h1", f"c{i}") for i in range(3)]))
        bounded = many.find("command-recovery-list")
        assert bounded.height == 220
        assert bounded.scroll == ft.ScrollMode.AUTO
        assert many.find("recovered-h1-c2") is not None
        # Succeeded row with a created resource keeps both 48px actions
        # and routes the open-resource callback at the created record.
        routes: list[str] = []
        acted = await _open_recovery(
            _journal_with(
                [_recovery_row("h1", "c9", outcome="succeeded", created={"agent_id": "a1"})]
            ),
            granted=frozenset({"h1"}),
            go=routes.append,
        )
        assert acted.find("command-recovery-list").height is None
        buttons = {
            c.content: c
            for c in iter_controls([acted.page.views[0].controls])
            if isinstance(c, ft.Button) and isinstance(c.content, str)
        }
        opener = buttons["Open created resources"]
        assert opener.height == 48
        assert opener.disabled is False
        assert buttons["Reviewed"].height == 48
        assert buttons["Reviewed"].on_click is not None
        opener.on_click(object())
        assert routes == ["/agents/h1:a1"]

    run(scenario())


def test_tools_key_labels_legible_and_bytes_stable() -> None:
    """Backspace/Shift labels avoid tofu glyphs.

    U+232B/U+21E7 are missing from the bundled browser font
    (rd-check-keyboard-panel.png); the dock uses the full readable
    words. Canonical names, tooltips, 48px targets and encoded bytes
    are unchanged.
    """
    labels = {name: label for name, label, _ in DOCK_KEYS}
    assert labels["backspace"] == "Backspace"
    assert labels["shift-tab"] == "Shift Tab"
    assert labels["shift-enter"] == "Shift Enter"
    for name, label in labels.items():
        assert "\u232b" not in label and "\u21e7" not in label, name
    assert encode_key("backspace") == b"\x7f"
    assert encode_key("shift-tab") == b"\x1b[Z"
    assert encode_key("shift-enter") == b"\n"

    async def scenario() -> None:
        app = await render_component(
            ToolsKeysDock,
            armed=frozenset(),
            enabled=True,
            on_key=_noop,
            on_toggle_modifier=_noop,
            on_dismiss=_noop,
            presentation="agent",
            dark=True,
        )
        await app.settle()
        back = app.find("tools-key-backspace")
        assert back.content == "Backspace"
        assert back.tooltip == "Backspace"
        assert back.height == MIN_TOUCH == 48
        assert app.find("tools-key-shift-tab").content == "Shift Tab"
        await app.set_props_and_fire("tools-page-terminal", {}, "click")
        await app.settle()
        shifting = app.find("tools-key-shift-enter")
        assert shifting.content == "Shift Enter"
        assert shifting.tooltip == "Shift Enter"

    run(scenario())
