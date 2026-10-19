"""Accessibility audit.

Headless audit against the REAL Flet 1.0.3 controls via the host
harness (no Flutter, no browser): semantic labels on every action,
declared action orders, keyboard-capable button contracts and direct
event dispatch, batched (never per-token) status
announcements, text-scale structure, contrast math over the real theme
tokens, and >=48dp touch targets from real control sizes.

Rendered-pixel checks (360/768/1280 browser matrix, back/forward, zoom,
screen-reader traversal, focus-ring visuals, soft-keyboard overlap,
device-measured dimensions, screenshots) need a browser harness and an
Android device: both are missing here, so those items are BLOCKED and
never claimed.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import flet as ft

from relaydeck.client.features.a11y import (
    DOCK_KEY_ORDER,
    FOCUS_ORDER,
    MIN_GRAPHIC_RATIO,
    MIN_TEXT_RATIO,
    StatusAnnouncer,
    audit_theme_tokens,
    check_focus_order,
    contrast_ratio,
    focus_order,
    interactive_controls,
    iter_controls,
)
from relaydeck.client.features.auth.components import AccountRow, LoginScreen
from relaydeck.client.features.auth.service import AuthSnapshot, AuthState
from relaydeck.client.features.composer.components import (
    REVIEW_CANCEL_KEY,
    REVIEW_CONFIRM_KEY,
    ComposerBox,
)
from relaydeck.client.features.composer.domain import (
    INPUT_MODE_COMPOSER,
    SUBMISSION_IDLE,
    SUBMISSION_PROGRESS,
)
from relaydeck.client.features.create.components import NewTerminalSheet, StartAgentSheet
from relaydeck.client.features.inventory.components import AppShell
from relaydeck.client.features.shell.screen import SHELL_CLOSE_KEY, ShellScreen
from relaydeck.client.features.shell.session import (
    SHELL_CONFLICT,
    SHELL_LIVE,
    ShellViewState,
)
from relaydeck.client.features.terminal.input_modes import (
    KEYBOARD_KEYS,
    KEYBOARD_TEXT,
    WORKFLOW_AGENT_COMPOSER,
)
from relaydeck.client.features.terminal.keys import (
    DOCK_KEYS,
    ToolsKeysDock,
)
from relaydeck.client.features.terminal.models import (
    ScrollModel,
    TerminalViewState,
    apply_live,
)
from relaydeck.client.features.terminal.paste_policy import PasteReview
from relaydeck.client.features.terminal.paste_review import PasteReviewSheet
from relaydeck.client.features.terminal.screen import TerminalScreen, status_announcement
from relaydeck.client.features.terminal.selection_sheet import SelectTextSheet
from relaydeck.client.features.terminal.surfaces import AgentTerminalScreen
from relaydeck.client.theme import DARK, LIGHT, MIN_TOUCH, STATUS_LABELS
from relaydeck.domain.models import (
    Agent,
    AgentStatus,
    AppState,
    Connection,
    ConnectionState,
    Host,
    Inventory,
    Selection,
    Tab,
    Workspace,
    scope_key,
)
from tests.support.host_harness import HostApp, render_component, run
from tests.support.ui.find_optional import find_or_none as _find_or_none


def _noop(*args: Any) -> None:
    pass


def _state(**kw: Any) -> AppState:
    hosts = {
        "home-mac": Host(
            id="home-mac",
            label="Home Mac",
            epoch="epoch-7",
            last_seen_ms=1_000,
            reachable=True,
            protocol=22,
            version="0.9.1",
        )
    }
    workspaces = {
        "w1": Workspace(id="w1", host_id="home-mac", label="web"),
        "w2": Workspace(id="w2", host_id="home-mac", label="ops"),
    }
    tabs = {
        "tab-1": Tab(
            id="tab-1", host_id="home-mac", workspace_id="w1", label="shell-a", pane_count=1
        ),
    }
    agents = {
        "term-1": Agent(
            terminal_id="term-1",
            host_id="home-mac",
            workspace_id="w1",
            tab_id="tab-2",
            pane_id="p1",
            name="web",
            agent="tool-a",
            status=AgentStatus.WORKING,
            focused=True,
        ),
        "term-2": Agent(
            terminal_id="term-2",
            host_id="home-mac",
            workspace_id="w2",
            tab_id="tab-2",
            pane_id="p2",
            name="ops",
            agent="tool-a",
            status=AgentStatus.BLOCKED,
        ),
    }
    state = AppState(
        connection=Connection(
            state=ConnectionState.LIVE,
            host_id="home-mac",
            cursor="c" * 32,
            last_sequence=3,
        ),
        inventory=Inventory(hosts=hosts, workspaces=workspaces, tabs=tabs, panes={}, agents=agents),
        selection=Selection(),
    )
    if kw:
        state = replace(state, **kw)
    return state


async def _render_shell(state: AppState, **kw: Any) -> HostApp:
    props: dict[str, Any] = {
        "state": state,
        "is_wide": True,
        "destination": "agents",
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


def _review() -> PasteReview:
    return PasteReview(line_count=2, character_count=11, preview="line one\nline two")


async def _render_agent_detail(**kw: Any) -> HostApp:
    props: dict[str, Any] = {
        "terminal": TerminalViewState(lease_mode="control"),
        "selection_text": "",
        "agent_title": "web",
        "agent_status": "Working",
        "workflow": WORKFLOW_AGENT_COMPOSER,
        "keyboard": KEYBOARD_TEXT,
        "draft": "hello",
        "markers": (),
        "can_send": True,
        "blockers": (),
        "submission": SUBMISSION_IDLE,
        "error": None,
        "blocked_review": None,
        "input_mode": INPUT_MODE_COMPOSER,
        "paste_review": None,
        "paste_error": None,
        "armed": frozenset(),
        "keys_enabled": True,
        "conflict_owner": None,
        "takeover_disclosure": None,
        "on_jump_to_bottom": _noop,
        "on_size_report": _noop,
        "on_input": _noop,
        "on_open_selection": _noop,
        "on_copy_selection": _noop,
        "on_close_selection": _noop,
        "on_draft_change": _noop,
        "on_send": _noop,
        "on_confirm_blocked": _noop,
        "on_cancel_blocked": _noop,
        "on_clear_error": _noop,
        "on_workflow_change": _noop,
        "on_keyboard_change": _noop,
        "on_key": _noop,
        "on_toggle_modifier": _noop,
        "on_dismiss_keys": _noop,
        "on_confirm_paste": _noop,
        "on_cancel_paste": _noop,
        "on_takeover": _noop,
        "on_confirm_takeover": _noop,
        "on_cancel_takeover": _noop,
    }
    props.update(kw)
    return await render_component(AgentTerminalScreen, **props)


async def _render_shell_screen(**kw: Any) -> HostApp:
    props: dict[str, Any] = {
        "view": ShellViewState(phase=SHELL_LIVE, terminal_id="tab-1"),
        "terminal": TerminalViewState(lease_mode="control"),
        "selection_text": "",
        "review": None,
        "paste_error": None,
        "armed": frozenset(),
        "keys_enabled": True,
        "on_jump_to_bottom": _noop,
        "on_size_report": _noop,
        "on_input": _noop,
        "on_open_selection": _noop,
        "on_copy_selection": _noop,
        "on_close_selection": _noop,
        "on_paste": _noop,
        "on_mode_change": _noop,
        "on_key": _noop,
        "on_toggle_modifier": _noop,
        "on_dismiss_keys": _noop,
        "on_confirm_paste": _noop,
        "on_cancel_paste": _noop,
        "on_disconnect": _noop,
        "on_reconnect": _noop,
        "on_takeover": _noop,
        "on_back": _noop,
        "on_close_terminal": _noop,
        "on_confirm_close": _noop,
        "on_cancel_close": _noop,
        "on_new_terminal": _noop,
    }
    props.update(kw)
    return await render_component(ShellScreen, **props)


def _roots(app: HostApp) -> list[Any]:
    roots: list[Any] = [app.page.views[0].controls]
    overlay = getattr(app.page, "_dialogs", None)
    if overlay is not None:
        roots.extend(overlay.controls or [])
    return roots


def _body(control: Any) -> Any:
    """Resolve a declarative Component wrapper to its rendered body."""
    body = getattr(control, "_b", None)
    if isinstance(body, list):
        return body[0] if body else control
    return body if body is not None else control


def _buttons(app: HostApp) -> list[Any]:
    return [c for c in iter_controls(_roots(app)) if isinstance(c, ft.Button | ft.TextButton)]


def _declared_height_floor(control: Any) -> float:
    """Infer the unscaled content floor from explicit sizes/line boxes.

    Flet1.0.3 has no Container.min_height or ButtonStyle.minimum_size.
    This is structural evidence; device measurement remains required.
    """
    if control is None:
        return 0
    height = getattr(control, "height", None)
    if isinstance(height, int | float):
        return float(height)
    if isinstance(control, ft.Text):
        style = control.style
        size = control.size or getattr(style, "size", None) or 0
        line = getattr(style, "height", None) or 1
        return float(size * line) if control.value else 0
    if isinstance(control, ft.Icon):
        return float(control.size or 0)
    if isinstance(control, ft.Row | ft.Column | ft.Stack):
        heights = [_declared_height_floor(child) for child in control.controls or []]
        if isinstance(control, ft.Column):
            return sum(heights) + float(control.spacing or 0) * max(0, len(heights) - 1)
        return max(heights, default=0)
    body = _body(control)
    if body is not control:
        return _declared_height_floor(body)
    content = getattr(control, "content", None)
    floor = _declared_height_floor(content)
    padding = getattr(control, "padding", None)
    if isinstance(padding, int | float):
        floor += 2 * padding
    elif isinstance(padding, ft.Padding):
        floor += padding.top + padding.bottom
    return floor


def test_height_floor_distinguishes_row_max_from_column_sum() -> None:
    first, second = ft.Container(height=20), ft.Container(height=25)
    assert _declared_height_floor(ft.Row(controls=[first, second])) == 25
    assert _declared_height_floor(ft.Column(controls=[first, second], spacing=3)) == 48
    assert _declared_height_floor(ft.TextButton(content=ft.Container(height=40))) < MIN_TOUCH


def test_announcer_batches_rapid_terminal_updates() -> None:
    announcer = StatusAnnouncer()
    # 100 rapid stream updates inside one window -> exactly one summary.
    for tick in range(100):
        announcer.note_output(now_ms=tick * 5)
    assert announcer.drain(1_000) == ("Terminal output updated (100 updates)",)
    assert announcer.drain(2_000) == ()

    # Discrete significant changes each announce (latest wins per window).
    announcer.announce("Working", now_ms=2_100)
    announcer.announce("Blocked", now_ms=2_200)
    assert announcer.drain(3_000) == ("Blocked",)

    # Consecutive duplicates collapse; churn never emits per-token.
    announcer.announce("Blocked", now_ms=3_100)
    for tick in range(50):
        announcer.note_output(now_ms=3_100 + tick)
    assert announcer.drain(4_000) == ()
    announcer.note_output(now_ms=4_100)
    assert announcer.drain(5_000) == ("Terminal output updated (1 updates)",)

    # Discrete beats churn within a window; windows stay <=1 each.
    announcer.note_output(now_ms=5_100)
    announcer.note_output(now_ms=5_150)
    announcer.announce("Done", now_ms=5_200)
    assert announcer.drain(6_000) == ("Done",)
    assert announcer.drain(7_000) == ()


def test_status_surfaces_expose_live_regions() -> None:
    async def scenario() -> None:
        state = TerminalViewState(lease_mode="control")
        terminal = await render_component(
            TerminalScreen,
            state=state,
            on_jump_to_bottom=_noop,
            on_size_report=_noop,
            on_input=_noop,
        )
        live = _body(terminal.find("terminal-status-live"))
        assert isinstance(live, ft.Semantics)
        assert live.live_region is True
        # The terminal announces the stable label (no byte offset) while
        # the visible line keeps the full detail.
        assert live.label == status_announcement(state)
        assert live.label != terminal.find("terminal-status").value
        # Pre-stream the footer is honest (connecting); live only after
        # the first established chunk (apply_live).
        assert "connecting" in str(live.label)
        assert "live" in status_announcement(apply_live(state, "e1", 0))

        offline = _state(
            connection=Connection(state=ConnectionState.HOST_OFFLINE, host_id="home-mac")
        )
        app = await _render_shell(offline)
        banner_live = _body(app.find("banner-live"))
        assert isinstance(banner_live, ft.Semantics)
        assert banner_live.live_region is True
        assert banner_live.label == app.find("banner-text").value

        agent = await _render_agent_detail()
        agent_live = _body(agent.find("agent-status-live"))
        assert isinstance(agent_live, ft.Semantics)
        assert agent_live.label == agent.find("agent-detail-status").value == "Working"

    run(scenario())


def test_terminal_announcement_stable_across_output_chunks() -> None:
    """States differing only in per-chunk values announce identically.

    The byte offset and unseen byte count change on every data chunk;
    neither may reach the live region (no per-token flood).
    Rendered through the real TerminalScreen, not the helper alone.
    """

    async def scenario() -> None:
        stuck = ScrollModel(stick_to_bottom=True)
        labels: list[str] = []
        visible: list[str] = []
        for offset in (100, 6291456):
            state = TerminalViewState(
                lease_mode="control", epoch="e1", applied_offset=offset, scroll=stuck
            )
            app = await render_component(
                TerminalScreen,
                state=state,
                on_jump_to_bottom=_noop,
                on_size_report=_noop,
                on_input=_noop,
            )
            labels.append(str(_body(app.find("terminal-status-live")).label))
            visible.append(str(app.find("terminal-status").tooltip))
        assert labels[0] == labels[1]
        assert "100" not in labels[0] and "6291456" not in labels[0]
        # ... and the status line's tooltip still carries the full detail
        # (The design shows only connection and mode on the line itself).
        assert "e1@6291456" in visible[1]
        detached = [
            TerminalViewState(
                lease_mode="control",
                scroll=ScrollModel(stick_to_bottom=False, unseen_bytes=unseen),
            )
            for unseen in (7, 1048576)
        ]
        back_labels: list[str] = []
        for state in detached:
            app = await render_component(
                TerminalScreen,
                state=state,
                on_jump_to_bottom=_noop,
                on_size_report=_noop,
                on_input=_noop,
            )
            back_labels.append(str(_body(app.find("terminal-status-live")).label))
        assert back_labels[0] == back_labels[1]
        assert "1048576" not in back_labels[0]  # byte counts never announced
        assert "scrolled back" in back_labels[0]

    run(scenario())


def _composer_props(**kw: Any) -> dict[str, Any]:
    props: dict[str, Any] = {
        "draft": "hello",
        "markers": (),
        "can_send": True,
        "blockers": (),
        "submission": SUBMISSION_IDLE,
        "error": None,
        "review": None,
        "input_mode": INPUT_MODE_COMPOSER,
        "on_draft_change": _noop,
        "on_send": _noop,
        "on_confirm_blocked": _noop,
        "on_cancel_blocked": _noop,
        "on_clear_error": _noop,
    }
    props.update(kw)
    return props


def _sheet_hosts() -> dict[str, Any]:
    return {
        "hosts": (("home-mac", "Home Mac"),),
        "workspaces_by_host": {"home-mac": (("w1", "web"),)},
        "submitting": False,
        "error": "",
        "command_status": "",
        "on_cancel": _noop,
    }


def test_every_action_has_an_accessible_name() -> None:
    """No icon-only/unlabeled actions: every button carries visible text,
    a tooltip or explicit action Semantics. Legacy dock labels stay source-faithful."""

    async def scenario() -> None:
        selected = _state(selection=Selection(host_id="home-mac", session_key="term-1"))
        offline = _state(
            connection=Connection(state=ConnectionState.HOST_OFFLINE, host_id="home-mac")
        )
        apps = [
            await _render_shell(selected),
            await _render_shell(selected, is_wide=False),
            await _render_shell(offline, destination="terminals"),
            await _render_shell(_state(), destination="settings"),
            await _render_shell(_state(), destination="settings"),
            await render_component(
                StartAgentSheet, kinds=("tool-a",), on_submit=_noop, **_sheet_hosts()
            ),
            await render_component(NewTerminalSheet, on_submit=_noop, **_sheet_hosts()),
            await render_component(ComposerBox, **_composer_props()),
            await render_component(ComposerBox, **_composer_props(review=_review())),
            await render_component(
                ToolsKeysDock,
                armed=frozenset(),
                enabled=True,
                on_key=_noop,
                on_toggle_modifier=_noop,
                on_dismiss=_noop,
            ),
            await render_component(
                PasteReviewSheet, review=_review(), on_confirm=_noop, on_cancel=_noop
            ),
            await render_component(
                SelectTextSheet, text="out", anchor=0, focus=3, on_copy=_noop, on_close=_noop
            ),
            await _render_agent_detail(),
            await _render_agent_detail(conflict_owner="desk", takeover_disclosure="take?"),
            await _render_agent_detail(keyboard=KEYBOARD_KEYS, paste_review=_review()),
            await _render_shell_screen(),
            await _render_shell_screen(
                view=ShellViewState(phase=SHELL_CONFLICT, terminal_id="t", owner_label="desk")
            ),
            await render_component(
                LoginScreen,
                snapshot=AuthSnapshot(state=AuthState.SIGNED_OUT),
                on_sign_in=_noop,
                on_cancel=_noop,
                on_retry=_noop,
                on_open_browser=_noop,
            ),
            await render_component(
                LoginScreen,
                snapshot=AuthSnapshot(state=AuthState.ERROR, error="boom", can_retry=True),
                on_sign_in=_noop,
                on_cancel=_noop,
                on_retry=_noop,
            ),
            await render_component(AccountRow, account="tester", on_logout=_noop),
        ]

        def semantic_action_name(app: HostApp, action: Any) -> str:
            for node in iter_controls(_roots(app)):
                if (
                    isinstance(node, ft.Semantics)
                    and node.button
                    and node.label
                    and any(child is action for child in iter_controls([node.content]))
                ):
                    return str(node.label)
            return ""

        for app in apps:
            for button in _buttons(app):
                content = button.content
                label = (
                    content
                    if isinstance(content, str)
                    else " ".join(
                        str(control.value)
                        for control in iter_controls([content])
                        if isinstance(control, ft.Text) and control.value
                    )
                )
                tooltip = button.tooltip
                label = label or (
                    tooltip if isinstance(tooltip, str) else getattr(tooltip, "message", "")
                )
                label = label or semantic_action_name(app, button)
                assert label.strip(), f"unlabeled {type(button).__name__} key={button.key!r}"
            for control in iter_controls(_roots(app)):
                if isinstance(control, ft.IconButton):
                    tooltip = control.tooltip
                    if isinstance(tooltip, str):
                        text = tooltip
                    else:
                        text = tooltip.message if tooltip is not None else ""
                    text = text or semantic_action_name(app, control)
                    assert text.strip(), f"unlabeled IconButton key={control.key!r}"
        # Tools dock: tooltips match the reference accessibility labels
        # verbatim. Expectations are literals, not a re-read of the table the
        # dock is built from.
        reference_labels = {
            "escape": "Escape",
            "tab": "Tab",
            "shift-tab": "Shift Tab",
            "shift-enter": "Shift Enter",
            "left": "Left Arrow",
            "up": "Up Arrow",
            "down": "Down Arrow",
            "right": "Right Arrow",
            "enter": "Enter",
            "backspace": "Backspace",
            "home": "Home",
            "end": "End",
            "insert": "Insert",
            "delete": "Forward Delete",
            "page-up": "Page Up",
            "page-down": "Page Down",
            **{f"f{n}": f"F{n}" for n in range(1, 13)},
        }
        assert {name: access for name, _, access in DOCK_KEYS} == reference_labels
        dock = apps[9]
        for name, access in reference_labels.items():
            assert dock.find(f"tools-key-{name}").tooltip == access
        assert dock.find("tools-mod-ctrl").tooltip == "Control modifier"
        assert dock.find("tools-mod-alt").tooltip == "Option modifier"
        assert dock.find("tools-mod-shift").tooltip == "Shift modifier"

    run(scenario())


def test_focus_orders_match_declared_sequences() -> None:
    async def scenario() -> None:
        from relaydeck.client.features.create.shell_chooser import ShellChooser

        new_agent = await render_component(
            StartAgentSheet, kinds=("tool-a",), on_submit=_noop, **_sheet_hosts()
        )
        new_shell = await render_component(NewTerminalSheet, on_submit=_noop, **_sheet_hosts())
        await new_agent.set_props_and_fire(
            "agent-workspace-mode", {"value": "new"}, "select", "new"
        )
        await new_shell.set_props_and_fire(
            "shell-workspace-mode", {"value": "new"}, "select", "new"
        )
        selected = _state(selection=Selection(host_id="home-mac", session_key="term-1"))
        offline = _state(
            connection=Connection(state=ConnectionState.HOST_OFFLINE, host_id="home-mac")
        )
        filtered = await _render_shell(selected)
        await filtered.set_props_and_fire("console-search", {}, "click")
        cases: list[tuple[str, HostApp]] = [
            (
                "shell-chooser",
                await render_component(
                    ShellChooser, tabs=(), on_choose=_noop, on_new=_noop, on_cancel=_noop
                ),
            ),
            ("start-agent-new-workspace", new_agent),
            ("new-terminal-new-workspace", new_shell),
            ("inventory-agents", await _render_shell(selected)),
            ("inventory-agents-filtered", filtered),
            ("inventory-terminals", await _render_shell(selected, destination="terminals")),
            ("inventory-hosts-layout", await _render_shell(_state(), destination="settings")),
            ("inventory-settings", await _render_shell(_state(), destination="settings")),
            ("inventory-agents-narrow", await _render_shell(_state(), is_wide=False)),
            ("inventory-agents-narrow-detail", await _render_shell(selected, is_wide=False)),
            (
                "start-agent-sheet",
                await render_component(
                    StartAgentSheet, kinds=("tool-a",), on_submit=_noop, **_sheet_hosts()
                ),
            ),
            (
                "new-terminal-sheet",
                await render_component(NewTerminalSheet, on_submit=_noop, **_sheet_hosts()),
            ),
            ("composer", await render_component(ComposerBox, **_composer_props())),
            (
                "composer-blocked-review",
                await render_component(ComposerBox, **_composer_props(review=_review())),
            ),
            (
                "paste-review",
                await render_component(
                    PasteReviewSheet, review=_review(), on_confirm=_noop, on_cancel=_noop
                ),
            ),
            (
                "select-text",
                await render_component(
                    SelectTextSheet,
                    text="out",
                    anchor=0,
                    focus=3,
                    on_copy=_noop,
                    on_close=_noop,
                ),
            ),
            (
                "tools-dock",
                await render_component(
                    ToolsKeysDock,
                    armed=frozenset(),
                    enabled=True,
                    on_key=_noop,
                    on_toggle_modifier=_noop,
                    on_dismiss=_noop,
                ),
            ),
            ("shell-live", await _render_shell_screen()),
            (
                "shell-conflict",
                await _render_shell_screen(
                    view=ShellViewState(phase=SHELL_CONFLICT, terminal_id="t", owner_label="d")
                ),
            ),
            (
                "shell-close-confirm",
                await _render_shell_screen(
                    view=ShellViewState(phase=SHELL_LIVE, terminal_id="t", confirm_close=True)
                ),
            ),
            ("agent-composer", await _render_agent_detail()),
            (
                "login",
                await render_component(
                    LoginScreen,
                    snapshot=AuthSnapshot(state=AuthState.SIGNED_OUT),
                    on_sign_in=_noop,
                    on_cancel=_noop,
                    on_retry=_noop,
                    on_open_browser=_noop,
                ),
            ),
            (
                "login-error",
                await render_component(
                    LoginScreen,
                    snapshot=AuthSnapshot(state=AuthState.ERROR, error="boom", can_retry=True),
                    on_sign_in=_noop,
                    on_cancel=_noop,
                    on_retry=_noop,
                ),
            ),
        ]
        from tests.support.ui.management import _render as render_management_root

        management = await render_management_root(width=1280)
        management_app = management[0]
        await management_app.set_props_and_fire("console-rail-settings", {}, "click")
        await management_app.settle(30)
        cases.append(("inventory-hosts", management_app))
        try:
            assert set(FOCUS_ORDER) == {name for name, _ in cases}, "registry/case drift"
            for name, app in cases:
                actual = focus_order(app.page)
                if app is management_app:
                    # The harness unmount button is outside the application's focus surface.
                    actual = tuple(key for key in actual if key != "test-unmount")
                assert not check_focus_order(actual, FOCUS_ORDER[name]), f"{name}: {actual!r}"
        finally:
            await management_app.set_props_and_fire("test-unmount", {}, "click")
            await management[1].aclose()
            management[2].push.close()
        # Alert banners traverse before content, never after it.
        offline_order = focus_order((await _render_shell(offline)).page)
        assert offline_order[4] == "banner-action", offline_order

    run(scenario())


def _recorder(calls: list[tuple[str, tuple[Any, ...]]], name: str) -> Any:
    def record(*args: Any) -> None:
        calls.append((name, args))

    return record


async def _drive_keyboard_order(
    app: HostApp,
    skip: frozenset[str] = frozenset(),
    text_values: dict[str, str] | None = None,
) -> None:
    """Dispatch actions on keyboard-capable control types in declared order.

    Headless click dispatch does not prove real Tab/Enter/Space behavior.
    The platform focus matrix remains an independent acceptance gate.
    """
    text_values = text_values or {}
    plan: list[tuple[str, str, str]] = []
    for key, control in interactive_controls(app.page):
        if key in skip:
            continue
        if isinstance(control, ft.Button | ft.TextButton | ft.IconButton | ft.ListTile):
            plan.append((key, "click", ""))
        elif isinstance(control, ft.TextField):
            plan.append((key, "change", text_values.get(key, "kb")))
        elif isinstance(control, ft.Dropdown):
            option = control.options[0]
            plan.append((key, "select", str(option.key if option.key is not None else option.text)))
        elif isinstance(control, ft.NavigationRail | ft.NavigationBar):
            plan.append((key, "nav", ""))
        elif isinstance(control, ft.PopupMenuButton):
            # Activation only opens the menu; its items are driven by key.
            continue
        else:
            raise AssertionError(f"no keyboard mapping for {type(control).__name__} {key!r}")
    for key, event, value in plan:
        if event == "click":
            await app.set_props_and_fire(key, {}, "click")
        elif event == "change":
            await app.set_props_and_fire(key, {"value": value}, "change", value)
        elif event == "select":
            await app.set_props_and_fire(key, {"value": value}, "select", value)
        else:
            await app.set_props_and_fire(key, {"selected_index": 1}, "change")


def test_keyboard_only_inventory_flows() -> None:
    async def scenario() -> None:
        calls: list[tuple[str, tuple[Any, ...]]] = []
        selected = _state(selection=Selection(host_id="home-mac", session_key="term-1"))
        app = await _render_shell(
            selected,
            on_destination=_recorder(calls, "destination"),
            on_select_agent=_recorder(calls, "select-agent"),
            on_select_shell=_recorder(calls, "select-shell"),
            on_select_host=_recorder(calls, "select-host"),
            on_banner_action=_recorder(calls, "banner"),
            on_open_terminal=_recorder(calls, "open-terminal"),
            on_start_agent=_recorder(calls, "start-agent"),
            on_new_terminal=_recorder(calls, "new-terminal"),
        )
        await app.set_props_and_fire("console-search", {}, "click")
        await _drive_keyboard_order(
            app,
            skip=frozenset({"inventory-query", "console-search", "console-collapse-toggle"}),
        )
        assert [name for name, _ in calls] == [
            "destination",
            "destination",
            "destination",
            "destination",
            "start-agent",
            "select-agent",
            "select-agent",
            "open-terminal",
        ]
        assert [args[0] for _, args in calls[:4]] == ["agents", "terminals", "usage", "settings"]
        assert {args[0] for _, args in calls[5:7]} == {
            scope_key("home-mac", "term-1"),
            scope_key("home-mac", "term-2"),
        }
        # Keyboard search filters the list without a pointer.
        await app.set_props_and_fire("inventory-query", {"value": "ops"}, "change", "ops")
        assert app.find("inventory-query").value == "ops"
        assert any(
            isinstance(control, ft.Text) and control.value == "ops"
            for control in iter_controls([app.find(f"agent-{scope_key('home-mac', 'term-2')}")])
        )
        assert app.find("agents-count").value == "1 agents"

    run(scenario())


def test_keyboard_only_creation_sheets() -> None:
    async def scenario() -> None:
        submitted: list[Any] = []
        cancelled: list[bool] = []
        start_props: dict[str, Any] = {
            **_sheet_hosts(),
            "on_submit": lambda draft, argv: submitted.append((draft, argv)),
            "on_cancel": lambda: cancelled.append(True),
        }
        start = await render_component(StartAgentSheet, kinds=("tool-a",), **start_props)
        await _drive_keyboard_order(start)
        assert cancelled == [True]
        assert len(submitted) == 1
        draft, argv = submitted[0]
        assert (draft.host_id, draft.workspace_id, draft.name, draft.kind) == (
            "home-mac",
            "w1",
            "kb",
            "tool-a",
        )
        assert argv == ["kb"]

        submitted.clear()
        cancelled.clear()
        new_props: dict[str, Any] = {
            **_sheet_hosts(),
            "on_submit": submitted.append,
            "on_cancel": lambda: cancelled.append(True),
        }
        new = await render_component(NewTerminalSheet, **new_props)
        await _drive_keyboard_order(new)
        assert cancelled == [True]
        assert len(submitted) == 1
        assert (submitted[0].host_id, submitted[0].workspace_id) == ("home-mac", "w1")

    run(scenario())


def test_keyboard_only_approval_analogues() -> None:
    """has no semantic approvals; the approval-shaped confirms are
    takeover disclosure, blocked insertion, and paste review. Each must be
    fully operable (confirm AND cancel) by keyboard alone."""

    async def scenario() -> None:
        calls: list[tuple[str, tuple[Any, ...]]] = []
        agent = await _render_agent_detail(
            conflict_owner="desk",
            takeover_disclosure="Attaching takes ownership.",
            blocked_review=_review(),
            can_send=False,
            paste_review=_review(),
            on_confirm_takeover=_recorder(calls, "takeover-confirm"),
            on_cancel_takeover=_recorder(calls, "takeover-cancel"),
            on_takeover=_recorder(calls, "takeover"),
            on_confirm_blocked=_recorder(calls, "blocked-confirm"),
            on_cancel_blocked=_recorder(calls, "blocked-cancel"),
            on_confirm_paste=_recorder(calls, "paste-confirm"),
            on_cancel_paste=_recorder(calls, "paste-cancel"),
        )
        for key in (
            "agent-takeover-confirm",
            "agent-takeover-cancel",
            REVIEW_CONFIRM_KEY,
            REVIEW_CANCEL_KEY,
            "paste-review-confirm",
            "paste-review-cancel",
        ):
            await agent.set_props_and_fire(key, {}, "click")
        assert [name for name, _ in calls] == [
            "takeover-confirm",
            "takeover-cancel",
            "blocked-confirm",
            "blocked-cancel",
            "paste-confirm",
            "paste-cancel",
        ]

        shell_calls: list[tuple[str, tuple[Any, ...]]] = []
        shell = await _render_shell_screen(
            view=ShellViewState(phase=SHELL_CONFLICT, terminal_id="t", owner_label="desk"),
            on_takeover=_recorder(shell_calls, "takeover"),
            on_back=_recorder(shell_calls, "back"),
        )
        await shell.set_props_and_fire("shell-takeover", {}, "click")
        await shell.set_props_and_fire("shell-back", {}, "click")
        assert [name for name, _ in shell_calls] == ["takeover", "back"]

    run(scenario())


def test_phone_composer_send_keeps_focus_and_announces_status() -> None:
    async def scenario() -> None:
        calls: list[tuple[str, tuple[Any, ...]]] = []
        app = await _render_agent_detail(
            phone=True,
            touch=True,
            submission=SUBMISSION_PROGRESS,
            on_send=_recorder(calls, "send"),
            on_composer_focus=_recorder(calls, "focus"),
            on_typing_exit=_recorder(calls, "exit"),
        )
        await app.set_props_and_fire("agent-compose-toggle", {}, "click")
        await app.set_props_and_fire("composer-draft", {}, "focus")
        field = app.find("composer-draft")
        await app.session.dispatch_event(field.suffix_icon.controls[0]._i, "click", "")
        await app.settle()
        after_send = app.find("composer-draft")
        assert after_send._i == field._i
        assert after_send.can_request_focus and not after_send.disabled
        assert after_send.on_submit is None
        assert calls == [("focus", (True,)), ("send", ())]
        assert any(
            isinstance(control, ft.Semantics)
            and control.live_region
            and getattr(control.content, "key", None) == "composer-notice"
            and control.content.value == "Agent is working on your message."
            for control in iter_controls([app.page.views[0].controls])
        )

    run(scenario())


def test_keyboard_only_composer_terminal_and_login() -> None:
    async def scenario() -> None:
        # Composer: draft, Send.
        composer_calls: list[tuple[str, tuple[Any, ...]]] = []
        composer = await render_component(
            ComposerBox,
            **_composer_props(
                on_draft_change=_recorder(composer_calls, "draft"),
                on_send=_recorder(composer_calls, "send"),
            ),
        )
        await _drive_keyboard_order(composer)
        assert [name for name, _ in composer_calls] == ["draft", "send"]

        # Tools dock: every key, every modifier, dismiss.
        dock_calls: list[tuple[str, tuple[Any, ...]]] = []
        dock = await render_component(
            ToolsKeysDock,
            armed=frozenset(),
            enabled=True,
            on_key=_recorder(dock_calls, "key"),
            on_toggle_modifier=_recorder(dock_calls, "mod"),
            on_dismiss=_recorder(dock_calls, "dismiss"),
        )
        await _drive_keyboard_order(dock)
        keys = [args[0] for name, args in dock_calls if name == "key"]
        rendered = [
            key.removeprefix("tools-key-") for key in DOCK_KEY_ORDER if key.startswith("tools-key-")
        ]
        assert keys == rendered
        assert [args[0] for name, args in dock_calls if name == "mod"] == [
            "ctrl",
            "alt",
            "shift",
        ]
        assert dock_calls[-1][0] == "dismiss"

        # Select Text: copy, close.
        sheet_calls: list[tuple[str, tuple[Any, ...]]] = []
        sheet = await render_component(
            SelectTextSheet,
            text="out",
            anchor=0,
            focus=3,
            on_copy=_recorder(sheet_calls, "copy"),
            on_close=_recorder(sheet_calls, "close"),
        )
        await _drive_keyboard_order(sheet, skip=frozenset({"select-text-field"}))
        assert [name for name, _ in sheet_calls] == ["copy", "close"]
        assert sheet_calls[0][1] == (0, 3)

        # Shell input row: paste, mode, disconnect, back, close.
        shell_calls: list[tuple[str, tuple[Any, ...]]] = []
        shell = await _render_shell_screen(
            on_paste=_recorder(shell_calls, "paste"),
            on_mode_change=_recorder(shell_calls, "mode"),
            on_disconnect=_recorder(shell_calls, "disconnect"),
            on_back=_recorder(shell_calls, "back"),
            on_close_terminal=_recorder(shell_calls, "close"),
            on_open_selection=_recorder(shell_calls, "select-text"),
        )
        await _drive_keyboard_order(shell)
        # Window order: header (Back, Disconnect, More), then the key row
        # (More keys, Select text, Paste). Close Terminal is a More-menu
        # item, reached from the menu button.
        assert [name for name, _ in shell_calls] == [
            "back",
            "disconnect",
            "mode",
            "select-text",
            "paste",
        ]
        assert shell_calls[2][1] == ("keys",)
        await shell.set_props_and_fire(SHELL_CLOSE_KEY, {}, "click")
        assert shell_calls[-1][0] == "close"

        # Agent detail switches: workflow, keyboard.
        agent_calls: list[tuple[str, tuple[Any, ...]]] = []
        agent = await _render_agent_detail(
            on_workflow_change=_recorder(agent_calls, "workflow"),
            on_keyboard_change=_recorder(agent_calls, "keyboard"),
            on_send=_recorder(agent_calls, "send"),
            on_draft_change=_recorder(agent_calls, "draft"),
            on_open_selection=_recorder(agent_calls, "select-text"),
        )
        await _drive_keyboard_order(agent)
        # Window order: key row (More keys, Select text), then the input
        # row (workflow switch, composer draft, Send).
        assert [name for name, _ in agent_calls] == [
            "keyboard",
            "select-text",
            "workflow",
            "draft",
            "send",
        ]

        # Login: sign in, re-open, retry, cancel.
        login_calls: list[tuple[str, tuple[Any, ...]]] = []
        login = await render_component(
            LoginScreen,
            snapshot=AuthSnapshot(state=AuthState.SIGNED_OUT),
            on_sign_in=_recorder(login_calls, "sign-in"),
            on_cancel=_recorder(login_calls, "cancel"),
            on_retry=_recorder(login_calls, "retry"),
            on_open_browser=_recorder(login_calls, "reopen"),
        )
        await _drive_keyboard_order(login)
        assert [name for name, _ in login_calls] == ["sign-in", "reopen"]
        login_calls.clear()
        error = await render_component(
            LoginScreen,
            snapshot=AuthSnapshot(state=AuthState.ERROR, error="boom", can_retry=True),
            on_sign_in=_recorder(login_calls, "sign-in"),
            on_cancel=_recorder(login_calls, "cancel"),
            on_retry=_recorder(login_calls, "retry"),
        )
        await _drive_keyboard_order(error)
        assert [name for name, _ in login_calls] == ["retry", "cancel"]

    run(scenario())


def test_text_scale_structure_and_long_content() -> None:
    """Headless structure: full hostile names remain available and actions
    wrap/scroll. Compact cards may ellipsize with a full-name tooltip.
    This does not establish rendered 200% scale acceptance.
    """

    async def scenario() -> None:
        base = _state()
        agents = dict(base.inventory.agents)
        agents["term-1"] = replace(agents["term-1"], name="x" * 200)
        tabs = dict(base.inventory.tabs)
        tabs["tab-1"] = replace(tabs["tab-1"], label="https://" + "y" * 180 + ".example/some/path")
        hosts = dict(base.inventory.hosts)
        hosts["home-mac"] = replace(hosts["home-mac"], label="עבודה 🏠 中文 " + "z" * 120)
        hostile = replace(
            base, inventory=replace(base.inventory, agents=agents, tabs=tabs, hosts=hosts)
        )
        apps = [
            await _render_shell(hostile),
            await _render_shell(hostile, is_wide=False),
            await _render_shell(
                replace(hostile, selection=Selection(host_id="home-mac", session_key="tab-1")),
                destination="terminals",
            ),
            await _render_agent_detail(agent_title="t" * 200, draft="d" * 5000),
            await _render_agent_detail(blocked_review=_review()),
            await _render_shell_screen(),
            await render_component(ComposerBox, **_composer_props(draft="e" * 5000)),
            await render_component(
                PasteReviewSheet, review=_review(), on_confirm=_noop, on_cancel=_noop
            ),
            await render_component(
                SelectTextSheet,
                text="w" * 10000,
                anchor=0,
                focus=10000,
                on_copy=_noop,
                on_close=_noop,
            ),
        ]
        for app in apps:
            # A fixed group inside a wrapping row wraps as a unit (the
            # terminal key row): Flet 1.0.3 cannot nest a Wrap in a plain
            # Row, so groups are plain rows inside one bounded Wrap.
            wrapped_groups = {
                id(kid)
                for row in iter_controls(_roots(app))
                if isinstance(row, ft.Row) and row.wrap
                for kid in row.controls or []
            }
            for control in iter_controls(_roots(app)):
                if isinstance(control, ft.Row) and id(control) not in wrapped_groups:
                    # Icon-only header actions keep fixed 48px hit regions;
                    # full-text actions must wrap/scroll or share flex space.
                    text_actions = [
                        kid
                        for kid in control.controls or []
                        if isinstance(kid, ft.Button | ft.TextButton)
                        and (
                            isinstance(kid.content, str)
                            or any(isinstance(c, ft.Text) for c in iter_controls([kid.content]))
                        )
                    ]
                    # A segmented switch (Compose | Direct) is one toggle
                    # with two short fixed options, not a row of actions.
                    segmented = bool(text_actions) and all(
                        isinstance(kid.data, dict) and "selected" in kid.data
                        for kid in text_actions
                    )
                    assert (
                        not text_actions
                        or segmented
                        or control.wrap
                        or control.scroll == ft.ScrollMode.AUTO
                        or all(kid.expand for kid in text_actions)
                        # Fixed actions beside an expanded, ellipsizing
                        # sibling (window header title) share flex space.
                        or any(
                            kid.expand and kid not in text_actions for kid in control.controls or []
                        )
                    ), f"action row must wrap or scroll deliberately: {control.key!r}"
        # Hostile content is fully present in the tree (nothing truncated).
        assert any(
            isinstance(control, ft.Text) and control.value == "x" * 200
            for control in iter_controls([apps[0].find(f"agent-{scope_key('home-mac', 'term-1')}")])
        )
        card = apps[0].find(f"agent-{scope_key('home-mac', 'term-1')}")
        assert "x" * 200 in card.tooltip, "compact card must retain the full name"
        for control in iter_controls([card]):
            if isinstance(control, ft.Text) and control.value == "x" * 200:
                assert not control.no_wrap or control.overflow == ft.TextOverflow.ELLIPSIS
        # one host means no host chip; the agent card keeps the full name.
        assert _find_or_none(apps[0], "host-chip-home-mac") is None
        # Shell rows ellipsize the name; the tooltip keeps all of it.
        assert (
            apps[2].find(f"shell-{scope_key('home-mac', 'tab-1')}").tooltip.startswith("https://")
        )

    run(scenario())


def test_theme_contrast_and_no_color_only_status() -> None:
    """Theme tokens meet the 3:1 non-text bar on light and dark reference
    surfaces (they always pair with a text label); any explicit text color
    must meet 4.5:1; every status signal carries words, not hue alone."""

    async def scenario() -> None:
        for name, color, light, dark in audit_theme_tokens():
            assert light >= MIN_GRAPHIC_RATIO, f"{name} {color} vs light: {light:.2f}"
            assert dark >= MIN_GRAPHIC_RATIO, f"{name} {color} vs dark: {dark:.2f}"

        selected = _state(selection=Selection(host_id="home-mac", session_key="term-1"))
        offline = _state(
            connection=Connection(state=ConnectionState.HOST_OFFLINE, host_id="home-mac")
        )
        apps = [
            await _render_shell(selected),
            await _render_shell(offline, destination="terminals"),
            await _render_shell(_state(), destination="settings"),
            await _render_agent_detail(),
            await _render_shell_screen(),
            # Audit each explicit foreground against its inherited surface.
            await _render_shell(selected, is_wide=False),
            await _render_shell(_state(), destination="settings"),
            await render_component(
                StartAgentSheet, kinds=("tool-a",), on_submit=_noop, **_sheet_hosts()
            ),
            await render_component(NewTerminalSheet, on_submit=_noop, **_sheet_hosts()),
            await render_component(ComposerBox, **_composer_props()),
            await render_component(ComposerBox, **_composer_props(review=_review())),
            await render_component(
                ToolsKeysDock,
                armed=frozenset(),
                enabled=True,
                on_key=_noop,
                on_toggle_modifier=_noop,
                on_dismiss=_noop,
            ),
            await render_component(
                PasteReviewSheet, review=_review(), on_confirm=_noop, on_cancel=_noop
            ),
            await render_component(
                SelectTextSheet, text="out", anchor=0, focus=3, on_copy=_noop, on_close=_noop
            ),
            await _render_agent_detail(conflict_owner="desk", takeover_disclosure="take?"),
            await _render_agent_detail(keyboard=KEYBOARD_KEYS, paste_review=_review()),
            await _render_shell_screen(
                view=ShellViewState(phase=SHELL_CONFLICT, terminal_id="t", owner_label="desk")
            ),
            await render_component(
                LoginScreen,
                snapshot=AuthSnapshot(state=AuthState.SIGNED_OUT),
                on_sign_in=_noop,
                on_cancel=_noop,
                on_retry=_noop,
                on_open_browser=_noop,
            ),
            await render_component(
                LoginScreen,
                snapshot=AuthSnapshot(state=AuthState.ERROR, error="boom", can_retry=True),
                on_sign_in=_noop,
                on_cancel=_noop,
                on_retry=_noop,
            ),
            await render_component(AccountRow, account="tester", on_logout=_noop),
        ]
        assert len(apps) == 20
        light_apps = [
            await _render_shell(selected, dark=False),
            await _render_agent_detail(dark=False),
            await _render_shell_screen(dark=False),
        ]
        # Check the nearest declared surface, including deliberately dark
        # terminal bars under a light application theme. A foreground need
        # not contrast with the opposite theme's background.
        for app, initial_bg in [(a, DARK["bg"]) for a in apps] + [
            (a, LIGHT["bg"]) for a in light_apps
        ]:
            seen: set[int] = set()

            def audit(control: Any, background: str, seen: set[int] = seen) -> None:
                if control is None or id(control) in seen:
                    return
                seen.add(id(control))
                bg = getattr(control, "bgcolor", None)
                if getattr(control, "selected", False):
                    bg = getattr(control, "selected_tile_color", None) or bg
                if isinstance(bg, str) and bg.startswith("#"):
                    background = bg
                painted = getattr(control, "color", None)
                if (
                    isinstance(control, ft.Text)
                    and isinstance(painted, str)
                    and painted.startswith("#")
                ):
                    ratio = contrast_ratio(painted, background)
                    assert ratio >= MIN_TEXT_RATIO, (
                        f"{control.key}: {painted}/{background}: {ratio:.2f}"
                    )
                for attr in (
                    "_b",
                    "controls",
                    "content",
                    "leading",
                    "title",
                    "subtitle",
                    "trailing",
                    "actions",
                ):
                    value = getattr(control, attr, None)
                    children = value if isinstance(value, list | tuple) else [value]
                    for child in children:
                        if isinstance(child, ft.Control) or hasattr(child, "_b"):
                            audit(child, background)

            for root in _roots(app):
                for control in root if isinstance(root, list) else [root]:
                    audit(control, initial_bg)

        # Status dots pair with the word: trailing label + tooltip.
        agents = apps[0]
        for terminal, status in (("term-1", "working"), ("term-2", "blocked")):
            card = agents.find(f"agent-{scope_key('home-mac', terminal)}")
            assert any(
                isinstance(control, ft.Text)
                and control.value.casefold() == STATUS_LABELS[status].casefold()
                for control in iter_controls([card])
            ), "every card must expose its status as a word"
        # Reachability icon pairs with a subtitle; banner tone pairs with text.
        hosts = apps[2]
        host_card = hosts.find("host-home-mac")
        assert any(
            isinstance(c, ft.Text) and c.value and "agent" in c.value
            for c in iter_controls([host_card])
        )
        assert "offline" in apps[1].find("banner-text").value.lower()
        # Terminal/agent status is a text line, announced via live regions.
        assert apps[3].find("agent-detail-status").value == "Working"
        assert "connecting" in str(apps[4].find("terminal-status").value)

    run(scenario())


def test_touch_targets_meet_48dp() -> None:
    """Every Button/TextButton declares >=48dp height on the real control;
    no interactive control declares a sub-48 explicit size."""

    async def scenario() -> None:
        selected = _state(selection=Selection(host_id="home-mac", session_key="term-1"))
        offline = _state(
            connection=Connection(state=ConnectionState.HOST_OFFLINE, host_id="home-mac")
        )
        apps = [
            await _render_shell(selected),
            await _render_shell(selected, is_wide=False),
            await _render_shell(offline, destination="terminals"),
            await _render_shell(_state(), destination="settings"),
            await _render_shell(_state(), destination="settings"),
            await render_component(
                StartAgentSheet, kinds=("tool-a",), on_submit=_noop, **_sheet_hosts()
            ),
            await render_component(NewTerminalSheet, on_submit=_noop, **_sheet_hosts()),
            await render_component(ComposerBox, **_composer_props()),
            await render_component(ComposerBox, **_composer_props(review=_review())),
            await render_component(
                ToolsKeysDock,
                armed=frozenset(),
                enabled=True,
                on_key=_noop,
                on_toggle_modifier=_noop,
                on_dismiss=_noop,
            ),
            await render_component(
                PasteReviewSheet, review=_review(), on_confirm=_noop, on_cancel=_noop
            ),
            await render_component(
                SelectTextSheet, text="out", anchor=0, focus=3, on_copy=_noop, on_close=_noop
            ),
            await _render_agent_detail(),
            await _render_agent_detail(conflict_owner="desk", takeover_disclosure="take?"),
            await _render_agent_detail(keyboard=KEYBOARD_KEYS, paste_review=_review()),
            await _render_shell_screen(),
            await _render_shell_screen(
                view=ShellViewState(phase=SHELL_CONFLICT, terminal_id="t", owner_label="desk")
            ),
            await render_component(
                LoginScreen,
                snapshot=AuthSnapshot(state=AuthState.SIGNED_OUT),
                on_sign_in=_noop,
                on_cancel=_noop,
                on_retry=_noop,
                on_open_browser=_noop,
            ),
            await render_component(AccountRow, account="tester", on_logout=_noop),
        ]
        checked = 0
        for app in apps:
            for button in _buttons(app):
                height = button.height
                if height is None:
                    height = _declared_height_floor(button.content)
                assert height >= MIN_TOUCH, (
                    f"{type(button).__name__} key={button.key!r} height floor={height!r}"
                )
                checked += 1
            for key, control in interactive_controls(app.page):
                for dim in ("height", "width"):
                    declared = getattr(control, dim, None)
                    if isinstance(declared, int | float):
                        assert declared >= MIN_TOUCH, f"{key} {dim}={declared}"
        assert checked > 60, f"expected broad button coverage, checked {checked}"

    run(scenario())
