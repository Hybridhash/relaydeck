"""Terminal window, Terminals list and Settings design.

Regression guards for reported issues and the new screens:
- re-selecting the current destination no longer clears the selection or
  closes the open terminal ("clicking Console reloads something");
- hidden retained surfaces keep an identical widget shape (always
  positioned, never opacity/pointer toggles), which on the Dart side
  remounted the native engine and forced "Viewer restarted; reattach";
- selecting an agent opens its window straight away, watching, with the
  take-over prompt; nothing attaches until Take over;
- the confirm-before-take-over setting off still arms the disclosure;
- Settings shows only rows the app honours and persists them;
- Terminals rows carry host · panes and Live/Detached.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import flet as ft
import pytest

from relaydeck.client.app import RETAINED_PARK_OFFSET
from relaydeck.client.components.design_system.shells import ShellListRow
from relaydeck.client.features.a11y.focus import iter_controls
from relaydeck.client.features.inventory.components import AppShell, terminal_rows
from relaydeck.client.features.settings import prefs as prefs_store
from relaydeck.client.features.settings.components import SettingsPage
from relaydeck.client.features.settings.prefs import (
    FONT_DEFAULT_PHONE,
    FONT_DEFAULT_WEB,
    FONT_MAX,
    PREFS_KEY,
    TerminalPrefs,
)
from relaydeck.client.features.terminal.agent_session import AGENT_CONNECTING, AGENT_LIVE
from relaydeck.client.features.terminal.input_modes import (
    WORKFLOW_AGENT_COMPOSER,
    WORKFLOW_AGENT_DIRECT,
)
from relaydeck.client.features.terminal.surfaces import (
    AGENT_TAKEOVER_CANCEL_KEY,
    AGENT_TAKEOVER_CONFIRM_KEY,
    mount_agent_screen,
)
from relaydeck.domain.models import Selection
from tests.support.host_harness import button_label, render_component, run
from tests.support.terminal.sessions import _agent_session
from tests.support.ui.navigation import _state
from tests.support.ui.retained_root import _open, _render
from tests.support.ui.window_design import _missing, _wrap_violations


def _texts(control: Any) -> set[str]:
    return {c.value for c in iter_controls([control]) if isinstance(c, ft.Text)}


# -- destination clicks ---------------------------------------------------------


def test_reclicking_console_returns_to_main_and_retains_terminal() -> None:
    async def scenario() -> None:
        app, controller, sessions, pool, _now = await _render()
        try:
            await _open(app, "home-mac:term-1")
            session = sessions.factory.created[0]
            assert session.phase == AGENT_LIVE
            engine = session.current_term
            await app.set_props_and_fire("console-rail-console", {}, "click")
            await app.settle()
            assert controller.state.selection.session_key == ""
            assert pool.foreground is None
            assert session.phase == AGENT_LIVE and session.current_term is engine
        finally:
            await app.set_props_and_fire("test-unmount", {}, "click")

    run(scenario())


def test_switching_destination_returns_to_list_and_card_restores_retained_view() -> None:
    async def scenario() -> None:
        app, controller, sessions, pool, _now = await _render()
        try:
            await _open(app, "home-mac:term-1")
            session = sessions.factory.created[0]
            engine, stream = session.current_term, session.controller
            await app.set_props_and_fire("console-rail-settings", {}, "click")
            await app.settle()
            assert pool.foreground is None and not session.foreground
            await app.set_props_and_fire("console-rail-console", {}, "click")
            await app.settle()
            assert controller.state.selection.session_key == ""
            assert pool.foreground is None
            await app.set_props_and_fire("agent-home-mac:term-1", {}, "click")
            await app.settle()
            assert pool.foreground is not None and pool.foreground.session is session
            assert session.current_term is engine and session.controller is not stream
            assert session.lease.mode == "view" and session.controller.key_generation() is None
            assert session.phase == AGENT_LIVE
            assert len(sessions.ops.attaches) == 1
        finally:
            await app.set_props_and_fire("test-unmount", {}, "click")

    run(scenario())


def test_hidden_retained_slots_keep_widget_shape() -> None:
    """Flet wraps a control in Opacity / IgnorePointer only when those props
    are set; toggling them remounted the native terminal. Slots must always
    be positioned and park off screen at full width instead."""

    async def scenario() -> None:
        app, _controller, _sessions, pool, _now = await _render()
        try:
            await _open(app, "home-mac:term-1")
            first = pool.foreground
            await _open(app, "home-mac:term-2")
            second = pool.foreground
            assert first is not None and second is not None and first is not second
            parked = app.find(f"retained-slot-{first.render_key}")
            active = app.find(f"retained-slot-{second.render_key}")
            for slot in (parked, active):
                assert slot.opacity == 1.0
                assert slot.ignore_interactions is False
                assert slot.top == 0 and slot.bottom == 0
            assert (parked.left, parked.right) == (-RETAINED_PARK_OFFSET, RETAINED_PARK_OFFSET)
            assert parked.disabled is True
            assert (active.left, active.right) == (0, 0)
            assert active.disabled is False
            # Registry order is kept: no reordering when the active one changes.
            stack = app.find("retained-surfaces")
            keys = [control.key for control in stack.controls]
            assert keys.index(f"retained-slot-{first.render_key}") < keys.index(
                f"retained-slot-{second.render_key}"
            )
        finally:
            await app.set_props_and_fire("test-unmount", {}, "click")

    run(scenario())


# -- agent window: watching and take over ---------------------------------------


def test_selecting_agent_opens_watching_window_without_attaching() -> None:
    async def scenario() -> None:
        app, _controller, sessions, _pool, _now = await _render()
        try:
            await app.set_props_and_fire("agent-home-mac:term-1", {}, "click")
            await app.settle()
            session = sessions.factory.created[0]
            assert session.phase == AGENT_CONNECTING
            assert sessions.ops.attaches == []
            assert _missing(app, "detail-open")
            assert button_label(app.find(AGENT_TAKEOVER_CONFIRM_KEY)) == "Take over"
            assert button_label(app.find(AGENT_TAKEOVER_CANCEL_KEY)) == "Keep watching"
            # Watching: no keys or Paste; the composer still works because
            # its sends are agent.prompt commands, not PTY writes.
            assert _missing(app, "shortcut-key-escape")
            assert _missing(app, "agent-paste")
            assert app.find("composer-draft") is not None
            assert "WATCHING" in _texts(app.find("agent-window-state"))
        finally:
            await app.set_props_and_fire("test-unmount", {}, "click")

    run(scenario())


@pytest.mark.auto_takeover
def test_selecting_a_free_agent_takes_control_automatically() -> None:
    async def scenario() -> None:
        app, _controller, sessions, _pool, _now = await _render()
        try:
            await app.set_props_and_fire("agent-home-mac:term-1", {}, "click")
            await app.settle()
            session = sessions.factory.created[0]
            assert [attach["takeover"] for attach in sessions.ops.attaches] == [True]
            assert session.lease is not None and session.lease.mode == "control"
            assert _missing(app, AGENT_TAKEOVER_CONFIRM_KEY)
        finally:
            await app.set_props_and_fire("test-unmount", {}, "click")

    run(scenario())


def test_take_over_without_confirm_still_arms_disclosure_first() -> None:
    async def scenario() -> None:
        session, ops, _factory, _submitted = _agent_session()
        session.open()
        session.cancel_takeover()  # "Keep watching": single Take over remains
        app = await render_component(mount_agent_screen, session, confirm_takeover=False)
        assert _missing(app, AGENT_TAKEOVER_CANCEL_KEY)
        await app.set_props_and_fire("agent-takeover", {}, "click")
        await app.settle()
        assert session.phase == AGENT_LIVE
        assert [attach["takeover"] for attach in ops.attaches] == [True]

    run(scenario())


def test_in_control_window_matches_design_bottom_area() -> None:
    async def scenario() -> None:
        session, _ops, _factory, _submitted = _agent_session()
        session.open()
        assert await session.confirm_takeover() == AGENT_LIVE
        app = await render_component(mount_agent_screen, session, title="fx-rounding-fix")
        assert app.find("agent-detail-title").value == "fx-rounding-fix"
        assert "LIVE · IN CONTROL" in _texts(app.find("agent-window-state"))
        assert button_label(app.find("agent-disconnect")) == "Release"
        for name in ("escape", "tab", "shift-tab", "left", "up", "down", "right"):
            assert app.find(f"shortcut-key-{name}") is not None
        assert button_label(app.find("shortcut-key-enter")) == "Enter"
        assert button_label(app.find("terminal-select-text")) == "Select text"
        assert button_label(app.find("agent-paste")) == "Paste"
        assert app.find("agent-workflow-composer").disabled is True  # selected
        await app.set_props_and_fire("agent-workflow-direct", {}, "click")
        await app.settle()
        assert session.view().workflow == WORKFLOW_AGENT_DIRECT
        assert "Direct input is on" in next(iter(_texts(app.find("agent-direct-note"))))

    run(scenario())


# -- settings -------------------------------------------------------------------


def test_prefs_clean_load_save_round_trip() -> None:
    async def scenario() -> None:
        store: dict[str, Any] = {}

        async def get(key: str) -> Any:
            return store.get(key)

        async def put(key: str, value: str) -> bool:
            store[key] = value
            return True

        assert await prefs_store.load(get) == TerminalPrefs()
        chosen = TerminalPrefs(
            font_size=15.0,
            agents_open_in=WORKFLOW_AGENT_DIRECT,
            confirm_takeover=False,
            show_key_row=False,
        )
        assert await prefs_store.save(put, chosen) is True
        assert PREFS_KEY in store
        assert await prefs_store.load(get) == chosen
        store[PREFS_KEY] = '{"font_size": "huge", "agents_open_in": "shell", "show_key_row": 1}'
        assert await prefs_store.load(get) == TerminalPrefs()
        store[PREFS_KEY] = "not json"
        assert await prefs_store.load(get) == TerminalPrefs()

        async def broken(*_args: Any) -> Any:
            raise RuntimeError("storage unavailable")

        assert await prefs_store.load(broken) == TerminalPrefs()
        assert await prefs_store.save(broken, chosen) is False

    run(scenario())
    assert TerminalPrefs().effective_font_size(phone=False) == FONT_DEFAULT_WEB
    assert TerminalPrefs().effective_font_size(phone=True) == FONT_DEFAULT_PHONE
    assert TerminalPrefs(font_size=FONT_MAX).step_font(1, phone=False).font_size == FONT_MAX
    assert prefs_store.clean({"agents_open_in": WORKFLOW_AGENT_COMPOSER}) == TerminalPrefs()


def test_settings_page_shows_supported_rows_and_updates_prefs() -> None:
    async def scenario() -> None:
        changes: list[TerminalPrefs] = []
        app = await render_component(
            SettingsPage,
            prefs=TerminalPrefs(),
            on_prefs=changes.append,
            phone=False,
            account="synthetic",
            version="0.1.0",
            on_logout=lambda: None,
        )
        page_text = _texts(app.find("settings-pane"))
        for row in (
            "Terminal text size",
            "Agents open in",
            "Confirm before taking over",
            "Show the key row",
            "RelayDeck",
        ):
            assert row in page_text, row
        # Rows the app cannot honour yet are dropped, not shown dead.
        for dropped in ("Terminal theme", "SSH key", "Check for updates", "Agent is Done"):
            assert dropped not in page_text, dropped
        assert "13 px" in page_text
        await app.set_props_and_fire("settings-font-size-plus", {}, "click")
        await app.set_props_and_fire("settings-open-direct", {}, "click")
        await app.set_props_and_fire("settings-confirm-takeover", {}, "click")
        await app.set_props_and_fire("settings-show-key-row", {}, "click")
        assert changes == [
            TerminalPrefs(font_size=14.0),
            TerminalPrefs(agents_open_in=WORKFLOW_AGENT_DIRECT),
            TerminalPrefs(confirm_takeover=False),
            TerminalPrefs(show_key_row=False),
        ]
        phone = await render_component(
            SettingsPage,
            prefs=TerminalPrefs(),
            on_prefs=changes.append,
            phone=True,
            account="synthetic",
            version="0.1.0",
            on_logout=lambda: None,
        )
        assert "12 px" in _texts(phone.find("settings-pane"))

    run(scenario())


# -- terminals list -------------------------------------------------------------


def test_terminal_rows_show_host_panes_and_live_state() -> None:
    state = _state()
    (entry,) = terminal_rows(state, "", "", frozenset())
    assert entry.row == ShellListRow(
        key=entry.row.key, title="shell-a", meta="Home Mac · 1 pane", live=False
    )
    (live,) = terminal_rows(state, "", "", frozenset({entry.row.key}))
    assert live.row.live is True
    assert terminal_rows(state, "zzz", "", frozenset()) == ()
    assert terminal_rows(state, "", "other-host", frozenset()) == ()


def test_terminals_page_layout_matches_design() -> None:
    async def scenario() -> None:
        state = _state()
        key = terminal_rows(state, "", "", frozenset())[0].row.key
        selected = replace(state, selection=Selection(host_id="home-mac", session_key=key))
        app = await render_component(
            AppShell,
            state=selected,
            is_wide=True,
            destination="terminals",
            on_destination=lambda dest: None,
            on_select_agent=lambda key: None,
            on_select_shell=lambda key: None,
            on_select_host=lambda key: None,
            on_banner_action=lambda: None,
            on_open_terminal=lambda: None,
            on_start_agent=lambda: None,
            on_new_terminal=lambda: None,
            viewport_width=1440,
            live_shells=frozenset({key}),
        )
        header = _texts(app.find("terminals-header"))
        assert {"Terminals", "1 SHELL ON 1 HOST"} <= header
        assert app.find("new-terminal-open") is not None
        assert app.find("shells-collapse") is not None
        assert app.find("shells-search").hint_text == "Filter shells"
        assert app.find("shell-host-chip-all") is not None
        card = app.find(f"shell-{key}")
        assert {"shell-a", "Home Mac · 1 pane", "Live"} <= _texts(card)
        assert card.content.shadow is not None  # selected outline
        await app.set_props_and_fire("shells-collapse", {}, "click")
        assert app.find("shells-strip") is not None
        assert app.find(f"shell-tile-{key}") is not None

    run(scenario())


# -- Flet layout guard (grey error box) -------------------------------------------


def test_terminal_window_never_nests_wraps_unbounded() -> None:
    async def scenario() -> None:
        session, _ops, _factory, _submitted = _agent_session()
        session.open()
        watching = await render_component(mount_agent_screen, session)
        assert _wrap_violations(watching) == []
        assert await session.confirm_takeover() == AGENT_LIVE
        live = await render_component(mount_agent_screen, session)
        assert _wrap_violations(live) == []
        phone = await render_component(mount_agent_screen, session, phone=True)
        assert _wrap_violations(phone) == []

    run(scenario())


def test_host_switch_does_not_flash_connecting_banner() -> None:
    from relaydeck.client.features.inventory.selectors import connection_banner
    from relaydeck.domain.models import ConnectionState

    state = _state()
    switching = replace(
        state, connection=replace(state.connection, state=ConnectionState.SYNCHRONIZING)
    )
    assert connection_banner(switching) is None
    first = replace(switching, inventory=replace(switching.inventory, hosts={}))
    banner = connection_banner(first)
    assert banner is not None and banner.text == "Connecting…"
    reconnecting = replace(
        state, connection=replace(state.connection, state=ConnectionState.RECONNECTING)
    )
    assert connection_banner(reconnecting) is not None
