"""Phone input dock.

Plain builders preserve control ancestry across layouts. State and hooks belong
to the parent component; builders receive only props and callbacks.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import flet as ft

from relaydeck.client.components.design_system.terminal_window import (
    MobileKeyBar,
)
from relaydeck.client.features.composer.domain import SUBMISSION_FAILED, SUBMISSION_UNKNOWN
from relaydeck.client.features.terminal.events import (
    click,
)
from relaydeck.client.features.terminal.input_modes import (
    WORKFLOW_AGENT_COMPOSER,
    WORKFLOW_AGENT_DIRECT,
)
from relaydeck.client.features.terminal.key_bar import KeyBarItem
from relaydeck.client.features.terminal.layout_budget import (
    PHONE_DOCK_HEIGHT,
    PHONE_FALLBACK_WIDTH,
)
from relaydeck.client.features.terminal.models import TerminalViewState
from relaydeck.client.features.terminal.paste_policy import PasteReview
from relaydeck.client.features.terminal.pictures import (
    picture_chips,
)

if TYPE_CHECKING:
    from relaydeck.client.features.composer.domain import StagedMarker

from relaydeck.client.features.terminal.surfaces.actions import (
    DirectInput,
)
from relaydeck.client.features.terminal.surfaces.common import (
    AGENT_PASTE_KEY,
)
from relaydeck.client.features.terminal.surfaces.direct_typing import (
    PASTE_FALLBACK_HINT,
    DirectTypingField,
)
from relaydeck.client.features.terminal.surfaces.phone_composer import (
    _phone_composer,
    with_working_label,
)

# Memoised on value-equal inputs and stable callbacks: a render caused elsewhere on the screen
# reuses the key bar's controls instead of rebuilding and diffing its hundred-odd controls.
_KeyBar = ft.memo(MobileKeyBar)  # type: ignore[no-untyped-call]


def _ignore_item(item: Any, hold: bool) -> None:
    return None


def dock_signature(**shape: Any) -> tuple[Any, ...]:
    """The dock's shape: the inputs that decide which dock rows exist and how tall they are."""
    return tuple(sorted(shape.items()))


def _keyboard_toggle(
    *,
    in_control: bool,
    keyboard_closing: bool,
    mobile_mode: str,
    tier: str,
    toggle_keyboard: Callable[[], Any],
    tokens: dict[str, str],
) -> Any:
    """The Direct-keyboard button of the typing row."""
    return ft.IconButton(
        ft.Icons.KEYBOARD_HIDE,
        key="agent-workflow-toggle",
        width=32,
        height=32,
        icon_size=16,
        padding=0,
        selected=mobile_mode == "direct" and not keyboard_closing,
        selected_icon_color=tokens["accent_text"],
        tooltip=None
        if tier == "compact"
        else ("Close keyboard" if mobile_mode == "direct" else "Open Direct typing"),
        disabled=not in_control,
        on_click=click(toggle_keyboard),
    )


def _show_keyboard_icon(tokens: dict[str, str]) -> Any:
    """An up arrow over a keyboard, as on the text box's show-keyboard button."""
    return ft.Column(
        tight=True,
        spacing=0,
        horizontal_alignment=ft.CrossAxisAlignment.CENTER,
        controls=[
            ft.Icon(ft.Icons.KEYBOARD_ARROW_UP, size=12, color=tokens["muted"]),
            ft.Icon(ft.Icons.KEYBOARD, size=16, color=tokens["muted"]),
        ],
    )


def _typing_slot(typing_field: Any) -> ft.Container:
    return ft.Container(
        key="agent-typing-slot",
        expand=True,
        height=36,
        content=ft.Column(
            tight=True,
            horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
            controls=[typing_field],
        ),
    )


def build_phone_dock(
    *,
    agent_status: str,
    attached_output: bool,
    blocked_review: PasteReview | None,
    blockers: tuple[str, ...],
    bottom: list[Any],
    can_direct_type: Callable[[], bool] | None,
    can_send: bool,
    clear_paste_hint: Callable[[], Any],
    comp_size: tuple[float, float],
    composer_focusable: bool,
    composer_closing: bool,
    composer_focused: bool,
    dismiss_composer_keyboard: Callable[[], None],
    dark: bool,
    detail_width: float,
    direct_binding: Any,
    direct_field: Any,
    draft: str,
    error: str | None,
    focus_handled: Any,
    focus_requests: Any,
    in_control: bool,
    on_direct_focus: Callable[[], Any] | None = None,
    input_enabled: bool,
    input_mode: str,
    keyboard_closing: bool,
    keys_enabled: bool,
    markers: tuple[StagedMarker, ...],
    mobile_mode: str,
    modifier_states: dict[str, str] | None,
    on_cancel_blocked: Callable[[], Any],
    on_clear_error: Callable[[], Any],
    on_composer_focus: Callable[[bool], Any] | None,
    on_confirm_blocked: Callable[[], Any],
    on_direct_input: DirectInput | None,
    on_draft_change: Callable[[str], Any],
    on_input: Callable[[int, bytes], Any],
    on_mobile_item: Callable[[KeyBarItem, bool], Any] | None,
    on_open_selection: Callable[[], Any],
    on_paste: Callable[[], Any] | None,
    on_remove_picture: Any,
    paste_hint: bool,
    paste_review: PasteReview | None,
    phone_lines: int,
    pictures: tuple[Any, ...],
    send_then_leave: Callable[..., Any],
    set_comp_size: Callable[[tuple[float, float]], Any],
    show_key_row: bool,
    slot_children: list[Any],
    slot_mode: bool,
    split: bool,
    split_side_width: float,
    submission: str,
    terminal: TerminalViewState,
    tier: str,
    toggle_keyboard: Callable[[], Any],
    tokens: dict[str, str],
    workflow: str,
    dock_sizes: dict[tuple[Any, ...], float] | None = None,
    on_layout: Callable[[float, bool, bool], Any] | None = None,
) -> Any:
    """The phone input dock (composer, key bar, tab/typing row), normal or typing column."""
    compose = mobile_mode == "compose" and workflow == WORKFLOW_AGENT_COMPOSER
    # Idle in control, the dock follows the "Agents open in" setting.
    idle = mobile_mode == "reading" and in_control
    text_box = compose or (idle and workflow == WORKFLOW_AGENT_COMPOSER)
    direct_row = mobile_mode == "direct" or (idle and workflow != WORKFLOW_AGENT_COMPOSER)
    # In control, the key bar is for Direct input; otherwise it serves read-only output.
    # Direct input keeps its field and keys while the phone keyboard is down.
    direct = (
        in_control and workflow == WORKFLOW_AGENT_DIRECT and mobile_mode in ("direct", "reading")
    )
    key_bar_shown = (direct if in_control else attached_output) and not compose

    keyboard_button = _keyboard_toggle(
        in_control=in_control,
        keyboard_closing=keyboard_closing,
        mobile_mode=mobile_mode,
        tier=tier,
        toggle_keyboard=toggle_keyboard,
        tokens=tokens,
    )
    key_bar = _KeyBar(
        key="agent-mobile-keybar",
        on_item=on_mobile_item or _ignore_item,
        modifier_states=modifier_states or {},
        enabled=keys_enabled and in_control and not terminal.selection.open,
        on_paste=on_paste if in_control else None,
        on_select=on_open_selection if attached_output else None,
        visible=key_bar_shown,
        medium=tier == "medium",
        show_keys=show_key_row and in_control,
        paste_key=AGENT_PASTE_KEY,
    )
    # Direct input gets the same "Agent is working" label as the text box, above its keys.
    working = agent_status.lower() == "working"
    bottom.append(with_working_label(key_bar, working and direct, tokens, key="direct-working"))
    typing_field = DirectTypingField(
        on_input=on_direct_input or on_input,
        on_transaction=on_direct_input,
        visible=direct,
        on_focus=on_direct_focus,
        # iOS raises its keyboard only for a tap on the field itself, so while the keyboard is
        # down the keyboard icon sits inside the field; the separate button only closes it.
        suffix_icon=_show_keyboard_icon(tokens) if direct and mobile_mode != "direct" else None,
        enabled=input_enabled
        and keys_enabled
        and in_control
        and composer_focusable
        and not terminal.selection.open
        and paste_review is None
        and not keyboard_closing,
        binding=direct_binding,
        field_ref=direct_field,
        focus_requests=focus_requests,
        focus_handled=focus_handled,
        field_key="agent-workflow-direct-typing",
        dark=dark,
        can_input=can_direct_type,
        paste_fallback=paste_hint,
        on_input_handled=clear_paste_hint,
    )
    typing_row = ft.Row(
        key="agent-tab-row",
        height=40,
        spacing=4,
        visible=direct_row,
        controls=[
            _typing_slot(typing_field),
            ft.Semantics(
                key="agent-keyboard-semantics",
                visible=not (direct and mobile_mode != "direct"),
                button=True,
                selected=mobile_mode == "direct" and not keyboard_closing,
                label="Terminal keyboard",
                content=keyboard_button,
            ),
        ],
    )
    paste_notice = ft.Text(
        PASTE_FALLBACK_HINT,
        key="agent-direct-paste-hint",
        visible=paste_hint and mobile_mode == "direct",
        size=12,
        color=tokens["muted"],
    )
    # Keep the controlled field's identity, including a real focus gate
    # while hidden. This changes only input chrome, never terminal parents.
    composer = _phone_composer(
        draft=draft,
        markers=markers,
        can_send=can_send,
        blockers=blockers,
        submission=submission,
        error=error,
        review=blocked_review,
        input_mode=input_mode,
        on_draft_change=on_draft_change,
        on_send=send_then_leave,
        on_confirm_blocked=on_confirm_blocked,
        on_cancel_blocked=on_cancel_blocked,
        on_clear_error=on_clear_error,
        toggle=None,
        dark=dark,
        blocked=agent_status.lower() == "blocked",
        working=working and text_box,
        visible=text_box,
        focusable=text_box
        and composer_focusable
        and not terminal.selection.open
        and not composer_closing,
        compact=tier == "compact",
        chips=picture_chips(pictures, on_remove_picture) if pictures else None,
        lines=phone_lines if split else None,
        on_focus_change=on_composer_focus if slot_mode else None,
        keyboard=("hide" if composer_focused else "show") if slot_mode else "",
        on_hide_keyboard=dismiss_composer_keyboard,
    )
    phone_dock_content = ft.Column(
        tight=True, spacing=0, controls=[composer, paste_notice, typing_row]
    )
    if slot_mode:
        # Same wrappers in both layouts, so the composer field is never remounted.
        dock_w, dock_h = comp_size
        # Heights are remembered per dock shape so a mode change renders the right spacer in the
        # same frame: each late spacer step is an extra terminal resize.
        signature = dock_signature(
            compose=compose,
            mobile_mode=mobile_mode,
            workflow=workflow,
            parts=(text_box, direct_row, key_bar_shown),
            in_control=in_control,
            attached_output=attached_output,
            chips=bool(pictures),
            draft_lines=min(4, draft.count("\n") + 1),
            error_row=error is not None and submission in (SUBMISSION_FAILED, SUBMISSION_UNKNOWN),
            review=blocked_review is not None,
            paste_hint=paste_hint and mobile_mode == "direct",
            tier=tier,
            width=round(detail_width),
        )
        memo = dock_sizes if dock_sizes is not None else {}
        remembered = memo.get(signature)
        if split:
            spacer = 0.0
        elif remembered is not None:  # an empty dock measures 0, which is a real height
            spacer = remembered
        else:
            spacer = dock_h or PHONE_DOCK_HEIGHT
        if on_layout is not None:
            on_layout(spacer, key_bar_shown, signature in memo)
        bottom.append(ft.Container(key="agent-input-spacer", height=spacer))

        def measured(e: Any) -> None:
            if split:
                return
            # A hidden screen also measures 0; only a dock with nothing to show really is empty.
            if e.height > 0 or not (text_box or direct_row or key_bar_shown):
                memo[signature] = e.height
            if abs(e.width - dock_w) >= 2 or abs(e.height - spacer) >= 2:
                set_comp_size((e.width, e.height))

        slot_children.append(
            ft.Container(
                key="agent-input-dock",
                right=0,
                bottom=0,
                width=split_side_width if split else (detail_width or PHONE_FALLBACK_WIDTH),
                disabled=terminal.selection.open or not composer_focusable,
                content=ft.Container(
                    key="agent-input-dock-body",
                    on_size_change=measured,
                    content=phone_dock_content,
                ),
            )
        )
    else:
        bottom.append(
            ft.Container(
                key="agent-input-dock",
                disabled=terminal.selection.open or not composer_focusable,
                content=phone_dock_content,
            )
        )
