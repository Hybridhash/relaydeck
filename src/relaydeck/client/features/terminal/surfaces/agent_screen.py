"""State and layout owner for the agent terminal detail.

Sibling builders are plain functions so layout changes preserve the identity
and ancestry of the terminal, tap target and draft editor. Hooks stay here;
builders receive explicit props and callbacks."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

import flet as ft

from relaydeck.client.components.design_system.terminal_window import (
    STATE_IN_CONTROL,
    STATE_WATCHING,
    WindowFrame,
)
from relaydeck.client.features.terminal.input_modes import (
    KEYBOARD_KEYS,
    KEYBOARD_TEXT,
    WORKFLOW_AGENT_COMPOSER,
    WORKFLOW_AGENT_DIRECT,
)
from relaydeck.client.features.terminal.key_bar import KeyBarItem
from relaydeck.client.features.terminal.layout_budget import (
    FIELD_LINE_HEIGHT,
    TYPING_SPLIT_GAP,
    TYPING_SPLIT_HEADER_HEIGHT,
    WINDOW_PADDING,
    plan_detail,
    typing_split_lines,
    typing_split_widths,
)
from relaydeck.client.features.terminal.models import TerminalViewState
from relaydeck.client.features.terminal.paste_policy import PasteReview
from relaydeck.client.features.terminal.paste_review import (
    PasteReviewSheet,
)
from relaydeck.client.theme import palette

if TYPE_CHECKING:
    from relaydeck.client.features.composer.domain import StagedMarker

from relaydeck.client.features.terminal.surfaces.actions import (
    DirectInput,
)
from relaydeck.client.features.terminal.surfaces.agent_dock import (
    build_agent_dock,
)
from relaydeck.client.features.terminal.surfaces.agent_header import (
    build_agent_header,
)
from relaydeck.client.features.terminal.surfaces.agent_hooks import (
    use_open_hold,
    use_row_metric,
    use_staged_picture_reveal,
)
from relaydeck.client.features.terminal.surfaces.agent_panels import (
    build_state_panels,
    build_watching_composer,
)
from relaydeck.client.features.terminal.surfaces.common import (
    AGENT_DETAIL_KEY,
    AGENT_PASTE_ERROR_KEY,
    AGENT_PASTE_FRAME_KEY,
    _framed,
    agent_window_state,
)
from relaydeck.client.features.terminal.surfaces.direct_typing import (
    use_direct_keyboard,
    use_paste_fallback,
)
from relaydeck.client.features.terminal.surfaces.input_area import (
    build_expanded_input,
)
from relaydeck.client.features.terminal.surfaces.phone_dock import (
    build_phone_dock,
)
from relaydeck.client.features.terminal.surfaces.select_stack import (
    build_select_stack,
)
from relaydeck.client.features.terminal.surfaces.terminal_host import (
    build_terminal_host,
)


@ft.component
def AgentTerminalScreen(
    terminal: TerminalViewState,
    selection_text: str,
    agent_title: str,
    agent_status: str,
    workflow: str,
    keyboard: str,
    draft: str,
    markers: tuple[StagedMarker, ...],
    can_send: bool,
    blockers: tuple[str, ...],
    submission: str,
    error: str | None,
    blocked_review: PasteReview | None,
    input_mode: str,
    paste_review: PasteReview | None,
    paste_error: str | None,
    armed: frozenset[str],
    keys_enabled: bool,
    conflict_owner: str | None,
    takeover_disclosure: str | None,
    on_jump_to_bottom: Callable[[], Any],
    on_size_report: Callable[[int, int], Any],
    on_input: Callable[[int, bytes], Any],
    on_open_selection: Callable[[], Any],
    on_copy_selection: Callable[[int, int], Any],
    on_close_selection: Callable[[], Any],
    on_draft_change: Callable[[str], Any],
    on_send: Callable[[], Any],
    on_confirm_blocked: Callable[[], Any],
    on_cancel_blocked: Callable[[], Any],
    on_clear_error: Callable[[], Any],
    on_workflow_change: Callable[[str], Any],
    on_keyboard_change: Callable[[str], Any],
    on_key: Callable[[str], Any],
    on_toggle_modifier: Callable[[str], Any],
    on_dismiss_keys: Callable[[], Any],
    on_confirm_paste: Callable[[], Any],
    on_cancel_paste: Callable[[], Any],
    on_takeover: Callable[[], Any],
    on_confirm_takeover: Callable[[], Any],
    on_cancel_takeover: Callable[[], Any],
    *,
    input_enabled: bool = False,
    on_terminal: Callable[[Any | None], Any] | None = None,
    on_selection_change: Callable[[int, int], Any] | None = None,
    on_mode_report: Callable[[bool], Any] | None = None,
    dark: bool = True,
    on_back: Callable[[], Any] | None = None,
    viewport_height: float = 800,
    viewport_width: float = 0.0,
    on_open_shell: Callable[[], Any] | None = None,
    phase: str = "",
    host_line: str = "",
    notice: str = "",
    attached_output: bool = True,
    on_disconnect: Callable[[], Any] | None = None,
    on_reconnect: Callable[[], Any] | None = None,
    on_paste: Callable[[], Any] | None = None,
    confirm_takeover: bool = True,
    control_pending: bool = False,
    expected_size: tuple[int, int] | None = None,
    show_key_row: bool = True,
    font_size: float | None = None,
    phone: bool = False,
    width_tier: str | None = None,
    keyboard_open: bool = False,
    touch: bool = False,
    connection_text: str = "",
    connection_live: bool = True,
    on_connection_action: Callable[[], Any] | None = None,
    agent_tabs: tuple[tuple[str, str, str], ...] = (),
    dot_blink: bool = False,
    shown: bool = True,
    selected_agent: str = "",
    on_agent_tab: Callable[[str], Any] | None = None,
    on_operations: Callable[[], Any] | None = None,
    on_mobile_item: Callable[[KeyBarItem, bool], Any] | None = None,
    modifier_states: dict[str, str] | None = None,
    on_request_keyboard: Callable[[], Any] | None = None,
    on_keyboard_button: Callable[[bool], Any] | None = None,
    composer_focusable: bool = True,
    received_input: bool = False,
    direct_binding: Any = None,
    on_direct_input: DirectInput | None = None,
    can_direct_type: Callable[[], bool] | None = None,
    can_open_keyboard: Callable[[], bool] | None = None,
    selection_binding: Any = None,
    on_phone_copy: Callable[[str], Awaitable[str | None]] | None = None,
    can_phone_select: Callable[[], bool] | None = None,
    selection_font_size: float = 14,
    scroll_state: Any = None,
    scroll_binding: Any = None,
    on_view_report: Callable[[Any, Any], Any] | None = None,
    on_scroll: Callable[[int | None], Any] | None = None,
    can_scroll: Callable[[], bool] | None = None,
    scroll_input_enabled: bool = False,
    on_attach_picture: Any = None,
    picture_action: Any = None,
    attach_enabled: bool = False,
    pictures: tuple[Any, ...] = (),
    draft_restore_marker: tuple[int, int] = (0, 0),
    on_remove_picture: Any = None,
    picture_progress: Any = None,
    cached_screen: str = "",
    typing_split: bool = False,
    on_composer_focus: Callable[[bool], Any] | None = None,
    on_keep_typing: Callable[[], Any] | None = None,
    on_typing_exit: Callable[[str], Any] | None = None,
    keyboard_up: bool = False,
) -> Any:
    """Controlled agent detail with a stable terminal renderer.

    Watching exposes takeover disclosure; only confirmation authorizes attach.
    Composer and Direct retain independent keyboard choice. Status is read-only
    metadata, never inferred from output. Input callbacks use the current writer;
    the renderer binds on mount and unbinds on unmount.
    """
    tier = width_tier or ("compact" if phone else "expanded")
    phone = tier != "expanded"
    mobile_mode, set_mobile_mode = ft.use_state("reading")
    previous_tier = ft.use_ref(tier)

    def keep_workflow_on_resize() -> None:
        if previous_tier.current == "expanded" and tier != "expanded":
            set_mobile_mode("compose" if workflow == WORKFLOW_AGENT_COMPOSER else "direct")
        previous_tier.current = tier

    ft.use_effect(keep_workflow_on_resize, dependencies=[tier, workflow])

    tokens = palette(dark)
    (window_size, set_window_size, host_height, set_host_height, row_metric) = use_row_metric(
        font_size=font_size, dock_open=keyboard == KEYBOARD_KEYS
    )

    # No phase (direct renders): in control unless a prompt is pending.
    window = agent_window_state(
        phase
        or (
            "connecting"
            if takeover_disclosure is not None or conflict_owner is not None
            else "live"
        ),
        takeover_disclosure is not None,
        conflict_owner is not None,
    )
    if terminal.lease_mode == "view" and window == STATE_IN_CONTROL:
        window = STATE_WATCHING
    # An automatic takeover under way lays out as in control from the first frame, so the
    # terminal keeps one size instead of resizing as the take-over panel comes and goes. The
    # controls stay inactive until the session holds the writer; a conflict or failure ends it.
    connecting = control_pending and conflict_owner is None
    if connecting:
        window = STATE_IN_CONTROL
    in_control = window == STATE_IN_CONTROL
    held_report, held = use_open_hold(
        expected_size,
        on_size_report,
        active=shown,
        settled=not control_pending and (not in_control or keys_enabled),
        input_phase=(input_enabled, scroll_input_enabled),
        binding=direct_binding,
    )

    def report_size(cols: int, rows: int) -> Any:
        set_window_size((cols, rows))
        return held_report(cols, rows)

    detail_width, set_detail_width = ft.use_state(0.0)
    # Terminal | input column, only for an expanded in-control
    # agent. The terminal keeps its slot; only the host's right margin and
    # the siblings around it change.
    # On touch devices the input area lives in always-present
    # Stack slots whose only change between layouts is geometry (position,
    # width, field lines), so the draft TextField is never remounted and
    # keeps focus (iOS Safari ignores programmatic focus). Desktop keeps the
    # original in-column layout and never enters the split.
    slot_mode = touch and (phone or in_control)
    split = typing_split and slot_mode
    pad = 0.0 if phone else WINDOW_PADDING
    split_gap = 8.0 if phone else TYPING_SPLIT_GAP
    # Until a size event measures the detail (fresh mount or reload),
    # use the real viewport width, not the 800 pt constant.
    split_terminal_width, split_side_width = typing_split_widths(
        detail_width or viewport_width, padding=pad, gap=split_gap
    )
    split_top = pad + TYPING_SPLIT_HEADER_HEIGHT + (4 if phone else 12)
    # Measured sizes of the slots (kept from the normal layout while split).
    comp_size, set_comp_size = ft.use_state((0.0, 0.0))
    ctl_size, set_ctl_size = ft.use_state((0.0, 0.0))
    split_ctl_height, set_split_ctl_height = ft.use_state(0.0)
    # Measured composer frame height (the split dock sits under it), and
    # an open counter that changes the dock's scroll key each time it opens.
    split_comp_h, set_split_comp_h = ft.use_state(0.0)
    dock_epoch_ref: Any = ft.use_ref((False, 0))
    dock_was_open, dock_epoch = dock_epoch_ref.current
    dock_open = in_control and not phone and split and keyboard == KEYBOARD_KEYS
    if dock_open and not dock_was_open:
        dock_epoch += 1
    dock_epoch_ref.current = (dock_open, dock_epoch)
    split_dock_top = split_top + max(split_ctl_height, 48.0) + 8.0 + split_comp_h + 8.0
    slim = phone or split
    controls: list[Any] = [
        build_agent_header(
            agent_status=agent_status,
            agent_title=agent_title,
            attach_enabled=attach_enabled,
            connection_live=connection_live,
            connection_text=connection_text,
            host_line=host_line,
            on_attach_picture=on_attach_picture,
            on_back=on_back,
            on_connection_action=on_connection_action,
            on_disconnect=on_disconnect,
            on_open_shell=on_open_shell,
            on_operations=on_operations,
            phone=phone,
            picture_action=picture_action,
            slim=slim,
            terminal=terminal,
            window=window,
            window_size=window_size,
            workflow=workflow,
        )
    ]
    # Below 600px tall the input dock takes one flex share
    # (terminal three) and scrolls internally, so a short window can
    # never starve the terminal of rows.
    detail_height, set_detail_height = ft.use_state(0.0)
    budget = plan_detail(
        detail_height=detail_height or max(0.0, viewport_height - 96.0),
        viewport_height=viewport_height,
        font_size=font_size,
        measured_row=row_metric[1] if row_metric[0] == font_size else None,
        dock_open=keyboard == KEYBOARD_KEYS,
        picture_visible=picture_progress is not None,
    )
    # A short visible height (keyboard) or a panel that cannot fit
    # 12 rows with full chrome collapses the key row, status line, dock and
    # picture into the axes "Keys" toggle below, which opens the dock as an
    # overlay. The read-only watching row is never collapsed.
    collapse_keys = budget.collapse and not phone and in_control

    def switch_to_direct() -> None:
        set_mobile_mode("direct")
        on_keyboard_change(KEYBOARD_TEXT)
        on_workflow_change(WORKFLOW_AGENT_DIRECT)

    def begin_direct() -> None:
        switch_to_direct()
        if on_request_keyboard:
            on_request_keyboard()

    # On touch tablets the keyboard button is drawn by the native
    # terminal, so its tap focuses inside the gesture. The terminal is told whether to show it.
    native_keyboard_button = touch and in_control and not phone

    def sync_native_keyboard_button() -> None:
        if on_keyboard_button is not None:
            on_keyboard_button(native_keyboard_button)

    ft.use_effect(sync_native_keyboard_button, dependencies=[native_keyboard_button])

    (
        direct_field,
        focus_requests,
        focus_handled,
        open_keyboard,
        toggle_keyboard,
        close_keyboard,
        keyboard_closing,
    ) = use_direct_keyboard(
        opened=mobile_mode == "direct",
        enabled=phone
        and in_control
        and keys_enabled
        and composer_focusable
        and not terminal.selection.open,
        binding=direct_binding,
        on_open=begin_direct,
        on_close=lambda: set_mobile_mode("reading"),
        can_open=can_open_keyboard,
    )

    def composing() -> None:
        set_mobile_mode("compose")
        on_workflow_change(WORKFLOW_AGENT_COMPOSER)

    use_staged_picture_reveal(
        pictures=pictures,
        draft_restore_marker=draft_restore_marker,
        phone=phone,
        workflow=workflow,
        composer_focusable=composer_focusable,
        close_keyboard=close_keyboard,
        composing=composing,
    )
    on_paste, paste_hint, clear_paste_hint = use_paste_fallback(
        on_paste, open_keyboard, direct_binding, can_open_keyboard
    )
    controls.append(
        build_terminal_host(
            attached_output=attached_output,
            budget=budget,
            cached_screen=cached_screen,
            connecting=connecting or held,
            hidden=held and not cached_screen,
            can_scroll=can_scroll,
            collapse_keys=collapse_keys,
            dark=dark,
            font_size=font_size,
            host_height=host_height,
            in_control=in_control,
            input_enabled=input_enabled,
            keyboard=keyboard,
            keys_enabled=keys_enabled,
            keyboard_shown=mobile_mode == "direct",
            on_close_selection=on_close_selection,
            # Phone Direct input is the existing direct-typing field; the native
            # terminal request is for the expanded (tablet) tier.
            on_keyboard=toggle_keyboard if phone and touch and in_control else None,
            on_copy_selection=on_copy_selection,
            on_input=on_input,
            on_jump_to_bottom=on_jump_to_bottom,
            on_key=on_key,
            on_keyboard_change=on_keyboard_change,
            on_mode_report=on_mode_report,
            on_open_selection=on_open_selection,
            on_paste=on_paste,
            on_scroll=on_scroll,
            on_selection_change=on_selection_change,
            on_terminal=on_terminal,
            on_view_report=on_view_report,
            phone=phone,
            report_size=report_size,
            scroll_binding=scroll_binding,
            scroll_input_enabled=scroll_input_enabled,
            scroll_state=scroll_state,
            selection_text=selection_text,
            set_host_height=set_host_height,
            show_key_row=show_key_row and not split,  # Keys live in the column
            split=split,
            split_gap=split_gap,
            split_side_width=split_side_width,
            terminal=terminal,
            tier=tier,
            touch=touch,
        )
    )
    bottom: list[Any] = []
    dock_overlay, split_dock = build_agent_dock(
        armed=armed,
        bottom=bottom,
        budget=budget,
        collapse_keys=collapse_keys,
        composer_focusable=composer_focusable,
        dark=dark,
        detail_height=detail_height,
        dock_epoch=dock_epoch,
        in_control=in_control,
        keyboard=keyboard,
        keys_enabled=keys_enabled,
        on_dismiss_keys=on_dismiss_keys,
        on_key=on_key,
        on_toggle_modifier=on_toggle_modifier,
        pad=pad,
        phone=phone,
        picture_progress=picture_progress,
        show_key_row=show_key_row,
        split=split,
        split_ctl_height=split_ctl_height,
        split_dock_top=split_dock_top,
        split_side_width=split_side_width,
        split_top=split_top,
        terminal=terminal,
        tokens=tokens,
    )
    build_state_panels(
        bottom=bottom,
        confirm_takeover=confirm_takeover,
        conflict_owner=conflict_owner,
        notice=notice,
        on_cancel_takeover=on_cancel_takeover,
        on_confirm_takeover=on_confirm_takeover,
        on_reconnect=on_reconnect,
        on_takeover=on_takeover,
        phone=phone,
        takeover_disclosure=takeover_disclosure,
        tokens=tokens,
        window=window,
    )
    slot_children: list[Any] = []

    def keep_typing(action: Callable[..., Any]) -> Callable[..., Any]:
        """In typing mode, pressing the column's own controls blurs the box;
        that must not leave the mode (the box is still the target)."""
        # Keep typing mode without changing field focus (keyboard_up).
        if not split or (on_keep_typing is None and on_composer_focus is None):
            return action

        def wrapped(*args: Any) -> Any:
            result = action(*args)
            if on_keep_typing is not None:
                on_keep_typing()
            elif on_composer_focus is not None:
                on_composer_focus(True)
            return result

        return wrapped

    def send_then_leave() -> Any:
        # Send leaves typing mode. With a typing-exit owner the exit
        # is explicit and logged; otherwise the debounced focus owner handles it.
        result = on_send()
        if on_typing_exit is not None:
            on_typing_exit("send")
        elif split and on_composer_focus is not None:
            on_composer_focus(False)
        return result

    def leave_when_control_is_lost() -> None:
        # Taking over, releasing or losing control ends typing mode.
        if not in_control and on_typing_exit is not None:
            on_typing_exit("lost")

    ft.use_effect(leave_when_control_is_lost, dependencies=[in_control])

    phone_lines = typing_split_lines(detail_height - split_top, 56)
    if phone:
        build_phone_dock(
            agent_status=agent_status,
            agent_tabs=agent_tabs,
            dot_blink=dot_blink,
            agent_title=agent_title,
            attach_enabled=attach_enabled,
            attached_output=attached_output,
            blocked_review=blocked_review,
            blockers=blockers,
            bottom=bottom,
            can_direct_type=can_direct_type,
            can_send=can_send,
            clear_paste_hint=clear_paste_hint,
            close_keyboard=close_keyboard,
            comp_size=comp_size,
            composer_focusable=composer_focusable,
            composing=composing,
            dark=dark,
            detail_width=detail_width,
            direct_binding=direct_binding,
            direct_field=direct_field,
            draft=draft,
            error=error,
            focus_handled=focus_handled,
            focus_requests=focus_requests,
            in_control=in_control,
            input_enabled=input_enabled,
            input_mode=input_mode,
            keyboard_closing=keyboard_closing,
            keys_enabled=keys_enabled,
            markers=markers,
            mobile_mode=mobile_mode,
            modifier_states=modifier_states,
            on_agent_tab=on_agent_tab,
            on_attach_picture=on_attach_picture,
            on_cancel_blocked=on_cancel_blocked,
            on_clear_error=on_clear_error,
            on_composer_focus=on_composer_focus,
            on_confirm_blocked=on_confirm_blocked,
            on_direct_input=on_direct_input,
            on_draft_change=on_draft_change,
            on_input=on_input,
            on_mobile_item=on_mobile_item,
            on_open_selection=on_open_selection,
            on_paste=on_paste,
            on_remove_picture=on_remove_picture,
            paste_hint=paste_hint,
            paste_review=paste_review,
            phone_lines=phone_lines,
            picture_action=picture_action,
            pictures=pictures,
            selected_agent=selected_agent,
            send_then_leave=send_then_leave,
            set_comp_size=set_comp_size,
            show_key_row=show_key_row,
            slot_children=slot_children,
            slot_mode=slot_mode,
            split=split,
            split_side_width=split_side_width,
            submission=submission,
            terminal=terminal,
            tier=tier,
            toggle_keyboard=toggle_keyboard,
            tokens=tokens,
            workflow=workflow,
        )
    if not phone and window == STATE_WATCHING and workflow == WORKFLOW_AGENT_COMPOSER:
        bottom.append(
            build_watching_composer(
                agent_status=agent_status,
                attach_enabled=attach_enabled,
                blocked_review=blocked_review,
                blockers=blockers,
                can_send=can_send,
                composer_focusable=composer_focusable,
                dark=dark,
                draft=draft,
                error=error,
                input_mode=input_mode,
                markers=markers,
                on_attach_picture=on_attach_picture,
                on_cancel_blocked=on_cancel_blocked,
                on_clear_error=on_clear_error,
                on_confirm_blocked=on_confirm_blocked,
                on_draft_change=on_draft_change,
                on_remove_picture=on_remove_picture,
                on_send=on_send,
                picture_action=picture_action,
                pictures=pictures,
                submission=submission,
                terminal=terminal,
            )
        )
    # Draft lines that fill the typing column (text scrolls inside the field).
    expanded_lines = typing_split_lines(
        detail_height - split_top - pad,
        max(split_ctl_height, 48.0)
        + (split_dock.height + 8 if split_dock is not None else 0)
        + 8
        + 44,
    )
    if split and not keyboard_up:
        # With the keyboard down the draft is sized to its content, at
        # most 40% of the column (scrolls inside); the keyboard fills what is left.
        column_height = detail_height - split_top - pad
        cap = int(0.4 * column_height // FIELD_LINE_HEIGHT)
        expanded_lines = min(expanded_lines, max(3, cap))
    if in_control and not phone:
        build_expanded_input(
            agent_status=agent_status,
            attach_enabled=attach_enabled,
            blocked_review=blocked_review,
            blockers=blockers,
            bottom=bottom,
            can_send=can_send,
            collapse_keys=collapse_keys,
            comp_size=comp_size,
            composer_focusable=composer_focusable,
            ctl_size=ctl_size,
            dark=dark,
            detail_width=detail_width,
            draft=draft,
            error=error,
            expanded_lines=expanded_lines,
            input_mode=input_mode,
            keep_typing=keep_typing,
            keyboard=keyboard,
            markers=markers,
            on_attach_picture=on_attach_picture,
            on_cancel_blocked=on_cancel_blocked,
            on_clear_error=on_clear_error,
            on_composer_focus=on_composer_focus,
            on_typing_done=(lambda: on_typing_exit("done")) if on_typing_exit else None,
            on_confirm_blocked=on_confirm_blocked,
            on_draft_change=on_draft_change,
            on_keyboard_change=on_keyboard_change,
            on_remove_picture=on_remove_picture,
            on_workflow_change=on_workflow_change,
            pad=pad,
            picture_action=picture_action,
            pictures=pictures,
            send_then_leave=send_then_leave,
            set_comp_size=set_comp_size,
            set_ctl_size=set_ctl_size,
            set_split_comp_h=set_split_comp_h,
            set_split_ctl_height=set_split_ctl_height,
            slot_children=slot_children,
            slot_mode=slot_mode,
            split=split,
            split_comp_h=split_comp_h,
            split_ctl_height=split_ctl_height,
            split_dock=split_dock,
            split_side_width=split_side_width,
            split_top=split_top,
            submission=submission,
            terminal=terminal,
            workflow=workflow,
        )
    controls.extend(bottom)
    if paste_review is not None:
        controls.append(
            _framed(
                tokens,
                AGENT_PASTE_FRAME_KEY,
                PasteReviewSheet(
                    review=paste_review,
                    on_confirm=on_confirm_paste,
                    on_cancel=on_cancel_paste,
                ),
            )
        )
    if paste_error is not None:
        controls.append(ft.Text(paste_error, key=AGENT_PASTE_ERROR_KEY))
    frame = WindowFrame(controls, key="agent-detail-panel", phone=phone)
    frame.content.key = AGENT_DETAIL_KEY

    def frame_size(e: Any) -> None:
        if abs(e.height - detail_height) >= 2:
            set_detail_height(e.height)
        if abs(e.width - detail_width) >= 2:
            set_detail_width(e.width)

    frame.on_size_change = frame_size
    return build_select_stack(
        can_phone_select=can_phone_select,
        dark=dark,
        dock_overlay=dock_overlay,
        frame=frame,
        on_close_selection=on_close_selection,
        on_phone_copy=on_phone_copy,
        on_selection_change=on_selection_change,
        phone=phone,
        scroll_state=scroll_state,
        selection_binding=selection_binding,
        selection_font_size=selection_font_size,
        selection_text=selection_text,
        slot_children=slot_children,
        terminal=terminal,
    )
