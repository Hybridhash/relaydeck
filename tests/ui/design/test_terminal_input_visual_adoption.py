"""Terminal/input visual adoption structural contract.

These tests lock the presentation-only contract headlessly through real Flet dispatch: every
documented key still mounts, every callback still fires, value
handlers still read the live payload, and the reference geometry
holds (bounded scroll regions, wrapped action rows, 48dp targets,
themed frames). Real pixels need a browser run.
"""

from __future__ import annotations

from typing import Any

import flet as ft

from relaydeck.client.features.composer.components import (
    COMPOSER_KEY,
    DRAFT_KEY,
    MARKERS_KEY,
    REVIEW_CANCEL_KEY,
    REVIEW_CONFIRM_KEY,
    REVIEW_COUNTS_KEY,
    REVIEW_KEY,
    SEND_KEY,
    VALIDATION_KEY,
    ComposerBox,
)
from relaydeck.client.features.composer.domain import (
    BLOCKER_EMPTY,
    INPUT_MODE_COMPOSER,
    SUBMISSION_SENDING,
    StagedMarker,
)
from relaydeck.client.features.create.components import (
    NEW_TERMINAL_SHEET_KEY,
    START_AGENT_SHEET_KEY,
    NewTerminalSheet,
    StartAgentSheet,
)
from relaydeck.client.features.shell.screen import (
    SHELL_BACK_KEY,
    SHELL_CLOSE_KEY,
    SHELL_CONFLICT_KEY,
    SHELL_CONNECTING_KEY,
    SHELL_DISCONNECT_KEY,
    SHELL_DISCONNECTED_KEY,
    SHELL_EXITED_KEY,
    SHELL_HEADER_KEY,
    SHELL_INPUT_ROW_KEY,
    SHELL_KEYS_SCROLL_KEY,
    SHELL_MODE_KEYS_KEY,
    SHELL_MODE_TEXT_KEY,
    SHELL_NOTICE_KEY,
    SHELL_PASTE_ERROR_KEY,
    SHELL_PASTE_KEY,
    SHELL_SCREEN_KEY,
    SHELL_TITLE_KEY,
    ShellScreen,
)
from relaydeck.client.features.shell.session import (
    SHELL_CONFLICT,
    SHELL_CONNECTING,
    SHELL_DISCONNECTED,
    SHELL_EXITED,
    SHELL_LIVE,
    ShellViewState,
)
from relaydeck.client.features.terminal.input_modes import KEYBOARD_KEYS, KEYBOARD_TEXT
from relaydeck.client.features.terminal.models import TerminalViewState
from relaydeck.client.features.terminal.paste_policy import PasteReview
from relaydeck.client.features.terminal.paste_review import (
    CANCEL_KEY as PASTE_CANCEL_KEY,
)
from relaydeck.client.features.terminal.paste_review import (
    CONFIRM_KEY as PASTE_CONFIRM_KEY,
)
from relaydeck.client.features.terminal.paste_review import (
    COUNTS_KEY as PASTE_COUNTS_KEY,
)
from relaydeck.client.features.terminal.paste_review import (
    PREVIEW_KEY as PASTE_PREVIEW_KEY,
)
from relaydeck.client.features.terminal.paste_review import (
    SHEET_KEY as PASTE_SHEET_KEY,
)
from relaydeck.client.features.terminal.paste_review import (
    TITLE_KEY as PASTE_TITLE_KEY,
)
from relaydeck.client.features.terminal.paste_review import PasteReviewSheet
from relaydeck.client.features.terminal.selection_sheet import (
    SELECT_CLOSE_KEY,
    SELECT_COPY_KEY,
    SELECT_ERROR_KEY,
    SELECT_FIELD_KEY,
    SELECT_RANGE_KEY,
    SELECT_SHEET_KEY,
    SELECT_TITLE_KEY,
    SelectTextSheet,
)
from relaydeck.client.theme import DARK, LIGHT, MIN_TOUCH
from tests.support.host_harness import render_component, run

_REVIEW = PasteReview(line_count=2, character_count=7, preview="ab\ncdef")


def _noop(*args: Any, **kwargs: Any) -> None:
    pass


def _shell_props(**overrides: Any) -> dict[str, Any]:
    props: dict[str, Any] = {
        "view": ShellViewState(phase=SHELL_LIVE, terminal_id="t1", keyboard=KEYBOARD_KEYS),
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
        "on_dismiss_notice": _noop,
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
    props.update(overrides)
    return props


def test_shell_screen_live_keys_callbacks_and_geometry() -> None:
    async def scenario() -> None:
        calls: list[tuple[str, tuple[Any, ...]]] = []

        def rec(name: str) -> Any:
            def _call(*args: Any) -> None:
                calls.append((name, args))

            return _call

        app = await render_component(
            ShellScreen,
            **_shell_props(
                on_paste=rec("paste"),
                on_mode_change=rec("mode"),
                on_disconnect=rec("disconnect"),
                on_back=rec("back"),
                on_close_terminal=rec("close"),
            ),
        )
        # Terminal window: header (Back, Disconnect, More with
        # Close terminal), key row (keys, More keys, Select text, Paste),
        # input row (Compose | Direct switch), and the open tools dock.
        for key in (
            SHELL_SCREEN_KEY,
            SHELL_HEADER_KEY,
            SHELL_TITLE_KEY,
            SHELL_INPUT_ROW_KEY,
            SHELL_PASTE_KEY,
            SHELL_MODE_TEXT_KEY,
            SHELL_DISCONNECT_KEY,
            SHELL_BACK_KEY,
            SHELL_CLOSE_KEY,
            SHELL_KEYS_SCROLL_KEY,
        ):
            assert app.find(key) is not None, key
        # Geometry: scrollable keys dock and key row, 48dp live targets.
        assert app.find(SHELL_KEYS_SCROLL_KEY).scroll == ft.ScrollMode.AUTO
        assert app.find("terminal-key-row").wrap is True
        for key in (
            SHELL_PASTE_KEY,
            SHELL_MODE_TEXT_KEY,
            SHELL_DISCONNECT_KEY,
            SHELL_BACK_KEY,
            SHELL_CLOSE_KEY,
        ):
            assert app.find(key).height == MIN_TOUCH, key
        # Keys mode is active: the single More keys toggle offers Text.
        try:
            app.find(SHELL_MODE_KEYS_KEY)
            raise AssertionError("Keys toggle must read Text while the dock is open")
        except KeyError:
            pass
        assert app.find(SHELL_MODE_TEXT_KEY).disabled is False
        await app.set_props_and_fire(SHELL_PASTE_KEY, {}, "click")
        await app.set_props_and_fire(SHELL_MODE_TEXT_KEY, {}, "click")
        await app.set_props_and_fire(SHELL_DISCONNECT_KEY, {}, "click")
        await app.set_props_and_fire(SHELL_CLOSE_KEY, {}, "click")
        await app.settle()
        assert ("paste", ()) in calls
        assert ("mode", (KEYBOARD_TEXT,)) in calls
        assert ("disconnect", ()) in calls
        assert ("close", ()) in calls

    run(scenario())


def test_shell_screen_phase_branches_mount() -> None:
    async def scenario() -> None:
        connecting = await render_component(
            ShellScreen, **_shell_props(view=ShellViewState(phase=SHELL_CONNECTING))
        )
        assert connecting.find(SHELL_CONNECTING_KEY) is not None
        conflict = await render_component(
            ShellScreen,
            **_shell_props(view=ShellViewState(phase=SHELL_CONFLICT, owner_label="rival")),
        )
        assert conflict.find(SHELL_CONFLICT_KEY) is not None
        assert "rival" in str(conflict.find("shell-conflict-owner").value)
        await conflict.set_props_and_fire(SHELL_BACK_KEY, {}, "click")
        disconnected = await render_component(
            ShellScreen, **_shell_props(view=ShellViewState(phase=SHELL_DISCONNECTED))
        )
        assert disconnected.find(SHELL_DISCONNECTED_KEY) is not None
        exited = await render_component(
            ShellScreen, **_shell_props(view=ShellViewState(phase=SHELL_EXITED))
        )
        assert exited.find(SHELL_EXITED_KEY) is not None
        notice = await render_component(
            ShellScreen,
            **_shell_props(view=ShellViewState(phase=SHELL_LIVE, notice="held", keyboard="text")),
        )
        assert notice.find(SHELL_NOTICE_KEY).value == "held"
        review = await render_component(ShellScreen, **_shell_props(review=_REVIEW))
        assert review.find(PASTE_SHEET_KEY) is not None
        err = await render_component(ShellScreen, **_shell_props(paste_error="blocked"))
        assert err.find(SHELL_PASTE_ERROR_KEY).value == "blocked"
        confirm = await render_component(
            ShellScreen,
            **_shell_props(view=ShellViewState(phase=SHELL_LIVE, confirm_close=True)),
        )
        assert confirm.find("shell-close-confirm-box") is not None

    run(scenario())


def test_shell_screen_light_theme_renders() -> None:
    async def scenario() -> None:
        # The terminal window is the dark design in both themes; the
        # terminal area itself still follows the selected palette.
        app = await render_component(ShellScreen, **_shell_props(dark=False))
        assert app.find(SHELL_SCREEN_KEY) is not None
        assert app.find("shell-window").bgcolor == DARK["surface"]
        assert app.find("terminal-area").bgcolor == LIGHT["terminal_bg"]
        dark = await render_component(ShellScreen, **_shell_props(dark=True))
        assert dark.find("shell-window").bgcolor == DARK["surface"]
        assert dark.find("terminal-area").bgcolor == DARK["terminal_bg"]

    run(scenario())


def test_composer_box_controlled_contract() -> None:
    async def scenario() -> None:
        drafts: list[str] = []
        sends: list[bool] = []
        app = await render_component(
            ComposerBox,
            draft="",
            markers=(StagedMarker(token="@file", resolved=False),),
            can_send=False,
            blockers=(BLOCKER_EMPTY,),
            submission="",
            error=None,
            review=None,
            input_mode=INPUT_MODE_COMPOSER,
            on_draft_change=drafts.append,
            on_send=lambda: sends.append(True),
            on_confirm_blocked=_noop,
            on_cancel_blocked=_noop,
            on_clear_error=_noop,
        )
        assert app.find(COMPOSER_KEY) is not None
        assert app.find(DRAFT_KEY) is not None
        assert "resolving" in str(app.find(MARKERS_KEY).value)
        assert app.find(VALIDATION_KEY).value == "Write a message to send."
        assert app.find(SEND_KEY).disabled is True
        assert app.find(SEND_KEY).height == MIN_TOUCH
        # Live payload path: the dispatched value wins (browser fix).
        await app.set_props_and_fire(DRAFT_KEY, {"value": "hi"}, "change", "hi")
        await app.settle()
        assert drafts == ["hi"]
        ready = await render_component(
            ComposerBox,
            draft="go",
            markers=(),
            can_send=True,
            blockers=(),
            submission=SUBMISSION_SENDING,
            error=None,
            review=None,
            input_mode=INPUT_MODE_COMPOSER,
            on_draft_change=_noop,
            on_send=lambda: sends.append(True),
            on_confirm_blocked=_noop,
            on_cancel_blocked=_noop,
            on_clear_error=_noop,
        )
        # Busy submission disables Send even when the draft can send.
        assert ready.find(SEND_KEY).disabled is True
        blocked = await render_component(
            ComposerBox,
            draft="held",
            markers=(),
            can_send=True,
            blockers=(),
            submission="",
            error=None,
            review=_REVIEW,
            input_mode=INPUT_MODE_COMPOSER,
            on_draft_change=_noop,
            on_send=_noop,
            on_confirm_blocked=lambda: sends.append(True),
            on_cancel_blocked=_noop,
            on_clear_error=_noop,
        )
        assert blocked.find(REVIEW_KEY) is not None
        assert "2 lines" in str(blocked.find(REVIEW_COUNTS_KEY).value)
        assert blocked.find(SEND_KEY).disabled is True
        await blocked.set_props_and_fire(REVIEW_CONFIRM_KEY, {}, "click")
        await blocked.set_props_and_fire(REVIEW_CANCEL_KEY, {}, "click")
        await blocked.settle()
        assert sends == [True]

    run(scenario())


def test_create_sheets_submit_validated_drafts() -> None:
    hosts = (("home-mac", "Home Mac"),)
    workspaces = {"home-mac": (("w1", "web"),)}

    async def scenario() -> None:
        submitted: list[tuple[Any, list[str]]] = []
        cancelled: list[bool] = []
        app = await render_component(
            StartAgentSheet,
            hosts=hosts,
            workspaces_by_host=workspaces,
            kinds=("tool-a",),
            submitting=False,
            error="",
            command_status="",
            on_submit=lambda draft, argv: submitted.append((draft, argv)),
            on_cancel=lambda: cancelled.append(True),
        )
        assert app.find(START_AGENT_SHEET_KEY) is not None
        for key in ("agent-host", "agent-workspace", "agent-name", "agent-kind", "agent-argv"):
            field = app.find(key)
            assert field.height is None, key  # Labels/errors grow without clipping.
            assert field.content_padding.top == field.content_padding.bottom == 16, key
        await app.set_props_and_fire("agent-workspace", {"value": "w1"}, "select", "w1")
        await app.set_props_and_fire("agent-name", {"value": "kb"}, "change", "kb")
        await app.set_props_and_fire("sheet-submit", {}, "click")
        await app.settle()
        assert len(submitted) == 1
        draft, argv = submitted[0]
        assert (draft.host_id, draft.workspace_id, draft.name, draft.kind) == (
            "home-mac",
            "w1",
            "kb",
            "tool-a",
        )
        assert argv == []
        # Host change clears the workspace: submit is blocked, not stale.
        await app.set_props_and_fire("agent-host", {"value": "home-mac"}, "select", "home-mac")
        await app.set_props_and_fire("sheet-submit", {}, "click")
        await app.settle()
        assert len(submitted) == 1
        await app.set_props_and_fire("sheet-cancel", {}, "click")
        await app.settle()
        assert cancelled == [True]

        shells: list[Any] = []
        term = await render_component(
            NewTerminalSheet,
            hosts=hosts,
            workspaces_by_host=workspaces,
            submitting=False,
            error="",
            command_status="",
            on_submit=shells.append,
            on_cancel=_noop,
        )
        assert term.find(NEW_TERMINAL_SHEET_KEY) is not None
        await term.set_props_and_fire("shell-workspace", {"value": "w1"}, "select", "w1")
        await term.set_props_and_fire("sheet-submit", {}, "click")
        await term.settle()
        assert len(shells) == 1
        assert (shells[0].host_id, shells[0].workspace_id) == ("home-mac", "w1")

    run(scenario())


def test_selection_sheet_binds_current_range_and_error() -> None:
    async def scenario() -> None:
        copied: list[tuple[int, int]] = []
        closed: list[bool] = []
        app = await render_component(
            SelectTextSheet,
            text="hello world",
            anchor=2,
            focus=5,
            on_copy=lambda a, f: copied.append((a, f)),
            on_close=lambda: closed.append(True),
        )
        for key in (
            SELECT_SHEET_KEY,
            SELECT_TITLE_KEY,
            SELECT_RANGE_KEY,
            SELECT_FIELD_KEY,
            SELECT_COPY_KEY,
            SELECT_CLOSE_KEY,
        ):
            assert app.find(key) is not None, key
        assert app.find(SELECT_RANGE_KEY).value == "Chars 2-5 of 11 (1 lines)"
        await app.set_props_and_fire(SELECT_COPY_KEY, {}, "click")
        await app.set_props_and_fire(SELECT_CLOSE_KEY, {}, "click")
        await app.settle()
        # Copy binds the CURRENT anchor/focus, never a stale open range.
        assert copied == [(2, 5)]
        assert closed == [True]
        failed = await render_component(
            SelectTextSheet,
            text="",
            anchor=None,
            focus=None,
            on_copy=_noop,
            on_close=_noop,
            error="Live snapshot unavailable: TimeoutError",
        )
        assert failed.find(SELECT_RANGE_KEY).value == "No output to select"
        assert "TimeoutError" in str(failed.find(SELECT_ERROR_KEY).value)

    run(scenario())


def test_paste_review_sheet_counts_and_confirm() -> None:
    async def scenario() -> None:
        confirmed: list[bool] = []
        cancelled: list[bool] = []
        app = await render_component(
            PasteReviewSheet,
            review=_REVIEW,
            on_confirm=lambda: confirmed.append(True),
            on_cancel=lambda: cancelled.append(True),
        )
        for key in (
            PASTE_SHEET_KEY,
            PASTE_TITLE_KEY,
            PASTE_COUNTS_KEY,
            PASTE_PREVIEW_KEY,
            PASTE_CONFIRM_KEY,
            PASTE_CANCEL_KEY,
        ):
            assert app.find(key) is not None, key
        assert app.find(PASTE_COUNTS_KEY).value == "2 lines \u00b7 7 characters"
        assert app.find(PASTE_PREVIEW_KEY).value == "ab\ncdef"
        assert app.find(PASTE_CONFIRM_KEY).height == MIN_TOUCH
        await app.set_props_and_fire(PASTE_CONFIRM_KEY, {}, "click")
        await app.set_props_and_fire(PASTE_CANCEL_KEY, {}, "click")
        await app.settle()
        assert confirmed == [True]
        assert cancelled == [True]

    run(scenario())
