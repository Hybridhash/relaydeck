"""Expanded input area.

Plain builders preserve control ancestry across layouts. State and hooks belong
to the parent component; builders receive only props and callbacks.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import flet as ft

from relaydeck.client.components.design_system.terminal_window import (
    DirectInputStrip,
    PillButton,
    SegmentedSwitch,
)
from relaydeck.client.features.composer.components import ComposerBox
from relaydeck.client.features.terminal.input_modes import (
    KEYBOARD_KEYS,
    KEYBOARD_TEXT,
    WORKFLOW_AGENT_COMPOSER,
    WORKFLOW_AGENT_DIRECT,
)
from relaydeck.client.features.terminal.layout_budget import (
    COMPOSER_HEIGHT,
    TYPING_SPLIT_FALLBACK_WIDTH,
)
from relaydeck.client.features.terminal.models import TerminalViewState
from relaydeck.client.features.terminal.paste_policy import PasteReview
from relaydeck.client.features.terminal.pictures import (
    attach_button,
    picture_chips,
)
from relaydeck.client.features.terminal.surfaces.tools_popover import tools_button

if TYPE_CHECKING:
    from relaydeck.client.features.composer.domain import StagedMarker

from relaydeck.client.features.terminal.surfaces.common import (
    AGENT_DIRECT_DRAFT_TEXT,
    AGENT_DIRECT_TEXT,
    AGENT_WORKFLOW_COMPOSER_KEY,
    AGENT_WORKFLOW_DIRECT_KEY,
    AGENT_WORKFLOW_GROUP_KEY,
    AGENT_WORKFLOW_KEY,
)

# Slot geometry eases between the normal and split layouts. The
# props are set in both layouts (never toggled), so no wrapper is added or removed.
SLOT_ANIMATION = ft.Animation(200, ft.AnimationCurve.EASE_OUT)
WIDE_DRAFT_LINES = 3


def _measure_composer(
    e: Any,
    *,
    split: bool,
    comp: tuple[float, float],
    split_comp_h: float,
    set_comp_size: Callable[[tuple[float, float]], Any],
    set_split_comp_h: Callable[[float], Any],
) -> None:
    """Record frame measurements separately for split and normal slot geometry."""
    if split:
        if abs(e.height - split_comp_h) >= 2:
            set_split_comp_h(e.height)
    elif abs(e.width - comp[0]) >= 2 or abs(e.height - comp[1]) >= 2:
        set_comp_size((e.width, e.height))


def build_expanded_input(
    *,
    agent_status: str,
    attach_enabled: bool,
    blocked_review: PasteReview | None,
    blockers: tuple[str, ...],
    bottom: list[Any],
    can_send: bool,
    collapse_keys: bool,
    comp_size: tuple[float, float],
    composer_focusable: bool,
    ctl_size: tuple[float, float],
    dark: bool,
    detail_width: float,
    draft: str,
    error: str | None,
    expanded_lines: int,
    input_mode: str,
    keep_typing: Callable[..., Any],
    keyboard: str,
    keyboard_up: bool,
    hide_requests: int = 0,
    markers: tuple[StagedMarker, ...],
    on_attach_picture: Any,
    on_cancel_blocked: Callable[[], Any],
    on_clear_error: Callable[[], Any],
    on_composer_focus: Callable[[bool], Any] | None,
    on_typing_done: Callable[[], Any] | None = None,
    on_confirm_blocked: Callable[[], Any],
    on_draft_change: Callable[[str], Any],
    on_keyboard_change: Callable[[str], Any],
    on_tools: Callable[[], Any],
    tools_open: bool,
    on_remove_picture: Any,
    on_workflow_change: Callable[[str], Any],
    pad: float,
    picture_action: Any,
    pictures: tuple[Any, ...],
    send_then_leave: Callable[..., Any],
    set_comp_size: Callable[[tuple[float, float]], Any],
    set_ctl_size: Callable[[tuple[float, float]], Any],
    set_split_comp_h: Callable[[float], Any],
    set_split_ctl_height: Callable[[float], Any],
    slot_children: list[Any],
    slot_mode: bool,
    split: bool,
    split_comp_h: float,
    split_ctl_height: float,
    split_side_width: float,
    split_top: float,
    submission: str,
    terminal: TerminalViewState,
    workflow: str,
) -> Any:
    """The expanded (non-phone) in-control input area: in-column, or the always-present slots."""
    if workflow == WORKFLOW_AGENT_COMPOSER:
        input_body: Any = ft.Container(
            expand=None if slot_mode else True,
            content=ComposerBox(
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
                dark=dark,
                blocked=agent_status.lower() == "blocked",
                working=agent_status.lower() == "working",
                attachment=attach_button(
                    on_attach_picture,
                    enabled=attach_enabled,
                    key="composer-attach-picture",
                    action=picture_action,
                ),
                chips=picture_chips(pictures, on_remove_picture) if pictures else None,
                on_focus_change=on_composer_focus,
                fill_lines=expanded_lines if split else WIDE_DRAFT_LINES,
                keyboard=("hide" if keyboard_up else "show") if split else "",
                hide_requests=hide_requests,
            ),
        )
    else:
        input_body = ft.Container(
            expand=None if slot_mode else True,
            content=DirectInputStrip(
                AGENT_DIRECT_DRAFT_TEXT if draft else AGENT_DIRECT_TEXT,
                key="agent-direct-note",
            ),
        )
    workflow_switch = ft.Container(
        key=AGENT_WORKFLOW_GROUP_KEY,
        content=SegmentedSwitch(
            (
                (WORKFLOW_AGENT_COMPOSER, "Compose", AGENT_WORKFLOW_COMPOSER_KEY),
                (WORKFLOW_AGENT_DIRECT, "Direct", AGENT_WORKFLOW_DIRECT_KEY),
            ),
            workflow,
            keep_typing(on_workflow_change),
            key=AGENT_WORKFLOW_KEY,
        ),
    )
    axes_controls: list[Any] = [workflow_switch, input_body]
    if split:
        axes_controls.insert(
            0, tools_button(keep_typing(on_tools), opened=tools_open, key="agent-tools-toggle")
        )
    elif collapse_keys:
        # The collapsed key row becomes one toggle that reopens the
        # dock (same keyboard flip as the More keys pill).
        axes_controls.insert(
            0,
            PillButton(
                "Keys",
                keep_typing(
                    lambda: on_keyboard_change(
                        KEYBOARD_TEXT if keyboard == KEYBOARD_KEYS else KEYBOARD_KEYS
                    )
                ),
                key="agent-keys-toggle",
                icon=ft.Icons.KEYBOARD,
                selected=keyboard == KEYBOARD_KEYS,
                tooltip="Ctrl, Alt and more keys",
            ),
        )
    if slot_mode:
        # Controls cluster, composer, spacer: the composer slot keeps its wrappers in both layouts.
        cluster_controls = axes_controls[:-1]
        comp_w, comp_h = comp_size
        ctl_w, ctl_h = ctl_size
        if split:
            cluster = ft.Container(
                key="agent-input-controls",
                right=pad,
                top=split_top,
                width=split_side_width,
                animate_position=SLOT_ANIMATION,
                animate_size=SLOT_ANIMATION,
                disabled=terminal.selection.open or not composer_focusable,
                # on_size_change must sit on an inner control: Flet wraps the
                # observer outside Positioned, which breaks the Stack parent data.
                content=ft.Container(
                    on_size_change=lambda e: (
                        set_split_ctl_height(e.height)
                        if abs(e.height - split_ctl_height) >= 2
                        else None
                    ),
                    content=ft.Row(
                        key="agent-input-axes",
                        wrap=True,
                        spacing=8,
                        run_spacing=6,
                        controls=[
                            *(
                                [
                                    ft.Row(
                                        tight=True,
                                        controls=[
                                            PillButton(
                                                "Done",
                                                lambda: on_typing_done(),
                                                key="agent-typing-done",
                                                tooltip="Leave typing mode",
                                            )
                                        ],
                                    )
                                ]
                                if on_typing_done is not None
                                else []
                            ),
                            *(ft.Row(tight=True, controls=[c]) for c in cluster_controls),
                        ],
                    ),
                ),
            )
        else:
            cluster = ft.Container(
                key="agent-input-controls",
                left=pad,
                bottom=pad + max(0.0, comp_h - ctl_h),
                animate_position=SLOT_ANIMATION,
                animate_size=SLOT_ANIMATION,
                disabled=terminal.selection.open or not composer_focusable,
                content=ft.Container(
                    on_size_change=lambda e: (
                        set_ctl_size((e.width, e.height))
                        if abs(e.width - ctl_w) >= 2 or abs(e.height - ctl_h) >= 2
                        else None
                    ),
                    content=ft.Row(
                        key="agent-input-axes",
                        spacing=10,
                        controls=cluster_controls,
                    ),
                ),
            )
        normal_width = max(
            160.0, (detail_width or TYPING_SPLIT_FALLBACK_WIDTH) - 2 * pad - ctl_w - 10
        )
        # In the typing split the frame is TOP-anchored under the
        # toggle row, so its position depends only on the header and toggle heights,
        # never on how many lines the draft column holds (empty vs typed).
        composer_slot = ft.Container(
            key="agent-composer-slot",
            animate_position=SLOT_ANIMATION,
            animate_size=SLOT_ANIMATION,
            right=pad,
            top=split_top + max(split_ctl_height, 48.0) + 8.0 if split else None,
            bottom=None if split else pad,
            width=split_side_width if split else normal_width,
            disabled=terminal.selection.open or not composer_focusable,
            content=ft.Container(
                on_size_change=lambda e: _measure_composer(
                    e,
                    split=split,
                    comp=(comp_w, comp_h),
                    split_comp_h=split_comp_h,
                    set_comp_size=set_comp_size,
                    set_split_comp_h=set_split_comp_h,
                ),
                content=input_body,
            ),
        )
        slot_children.extend([cluster, composer_slot])
        bottom.append(
            ft.Container(
                key="agent-input-spacer",
                height=0.0 if split else max(comp_h or COMPOSER_HEIGHT, ctl_h),
            )
        )
    else:
        axes = ft.Row(
            key="agent-input-axes",
            spacing=10,
            vertical_alignment=ft.CrossAxisAlignment.START,
            controls=axes_controls,
        )
        bottom.append(
            ft.Container(
                key="agent-input-dock",
                disabled=terminal.selection.open or not composer_focusable,
                content=ft.Column(tight=True, spacing=8, controls=[axes]),
            )
        )
