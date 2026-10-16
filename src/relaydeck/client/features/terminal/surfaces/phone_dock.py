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
    attach_button,
    picture_chips,
)
from relaydeck.client.features.terminal.surfaces.agent_tab_row import AgentTabRow

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
)


def dock_signature(**shape: Any) -> tuple[Any, ...]:
    """The dock's shape: the inputs that decide which dock rows exist and how tall they are."""
    return tuple(sorted(shape.items()))


def _toggle_buttons(
    *,
    close_keyboard: Callable[..., Any],
    composing: Callable[[], Any],
    in_control: bool,
    keyboard_closing: bool,
    mobile_mode: str,
    tier: str,
    toggle_keyboard: Callable[[], Any],
    tokens: dict[str, str],
) -> tuple[Any, Any]:
    """The Direct-keyboard and Compose icon buttons of the typing row."""
    keyboard_button = ft.IconButton(
        ft.Icons.KEYBOARD,
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
    compose_button = ft.IconButton(
        ft.Icons.EDIT,
        key="agent-compose-toggle",
        width=32,
        height=32,
        icon_size=16,
        padding=0,
        tooltip=None if tier == "compact" else "Compose message",
        on_click=click(lambda: close_keyboard(composing)),
    )
    return keyboard_button, compose_button


def build_phone_dock(
    *,
    agent_status: str,
    agent_tabs: tuple[tuple[str, str, str], ...],
    agent_title: str,
    attach_enabled: bool,
    attached_output: bool,
    blocked_review: PasteReview | None,
    blockers: tuple[str, ...],
    bottom: list[Any],
    can_direct_type: Callable[[], bool] | None,
    can_send: bool,
    clear_paste_hint: Callable[[], Any],
    close_keyboard: Callable[..., Any],
    comp_size: tuple[float, float],
    composer_focusable: bool,
    composing: Callable[[], Any],
    dark: bool,
    dot_blink: bool = False,
    detail_width: float,
    direct_binding: Any,
    direct_field: Any,
    draft: str,
    error: str | None,
    focus_handled: Any,
    focus_requests: Any,
    in_control: bool,
    input_enabled: bool,
    input_mode: str,
    keyboard_closing: bool,
    keys_enabled: bool,
    markers: tuple[StagedMarker, ...],
    mobile_mode: str,
    modifier_states: dict[str, str] | None,
    on_agent_tab: Callable[[str], Any] | None,
    on_attach_picture: Any,
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
    picture_action: Any,
    pictures: tuple[Any, ...],
    selected_agent: str,
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

    keyboard_button, compose_button = _toggle_buttons(
        close_keyboard=close_keyboard,
        composing=composing,
        in_control=in_control,
        keyboard_closing=keyboard_closing,
        mobile_mode=mobile_mode,
        tier=tier,
        toggle_keyboard=toggle_keyboard,
        tokens=tokens,
    )
    bottom.append(
        MobileKeyBar(  # type: ignore[call-arg]
            key="agent-mobile-keybar",
            on_item=on_mobile_item or (lambda item, hold: None),
            modifier_states=modifier_states or {},
            enabled=keys_enabled and in_control and not terminal.selection.open,
            on_paste=on_paste if in_control else None,
            on_select=on_open_selection if attached_output else None,
            visible=not compose and (in_control or attached_output),
            medium=tier == "medium",
            show_keys=show_key_row and in_control,
            paste_key=AGENT_PASTE_KEY,
        )
    )
    typing_field = DirectTypingField(
        on_input=on_direct_input or on_input,
        on_transaction=on_direct_input,
        visible=mobile_mode == "direct" and workflow == WORKFLOW_AGENT_DIRECT and in_control,
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
        visible=True,
        controls=[
            ft.Container(
                key="agent-typing-slot",
                expand=True,
                height=36,
                content=ft.Column(
                    tight=True,
                    horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
                    controls=[
                        AgentTabRow(
                            visible=mobile_mode != "direct" or not in_control,
                            agent_tabs=agent_tabs,
                            agent_status=agent_status,
                            agent_title=agent_title,
                            on_agent_tab=on_agent_tab,
                            selected_agent=selected_agent,
                            tokens=tokens,
                            blink=dot_blink,
                        ),
                        typing_field,
                    ],
                ),
            ),
            ft.Semantics(label="Compose message", button=True, content=compose_button),
            attach_button(
                on_attach_picture,
                enabled=attach_enabled,
                action=picture_action,
                compact=tier == "compact",
            ),
            ft.Semantics(
                key="agent-keyboard-semantics",
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
        visible=compose,
        focusable=compose and composer_focusable and not terminal.selection.open,
        compact=tier == "compact",
        chips=picture_chips(pictures, on_remove_picture) if pictures else None,
        lines=phone_lines if split else None,
        on_focus_change=on_composer_focus if slot_mode else None,
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
        spacer = 0.0 if split else memo.get(signature) or dock_h or PHONE_DOCK_HEIGHT
        if on_layout is not None:
            on_layout(spacer, not compose and (in_control or attached_output), signature in memo)
        bottom.append(ft.Container(key="agent-input-spacer", height=spacer))

        def measured(e: Any) -> None:
            if split:
                return
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
