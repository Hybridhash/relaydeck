"""Mount helper: derives `AgentTerminalScreen` props from one live `AgentSession`."""

from __future__ import annotations

import functools
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

import flet as ft

from relaydeck.client.features.composer.domain import (
    SEND_DELIVERED_VIA_ATTACH,
    SEND_DELIVERED_VIA_PROMPT,
    SUBMISSION_SENDING,
)
from relaydeck.client.features.terminal import haptics as haptic_feedback
from relaydeck.client.features.terminal.callbacks import view_actions
from relaydeck.client.features.terminal.diagnostics import diagnostics_logger
from relaydeck.client.features.terminal.input_modes import (
    KEYBOARD_TEXT,
    ROUTE_DROPPED,
    ROUTE_PTY,
    WORKFLOW_AGENT_COMPOSER,
    WORKFLOW_AGENT_DIRECT,
)
from relaydeck.client.features.terminal.picture_paste_region import (
    take_paste_props,
    wrap_with_paste_region,
)
from relaydeck.client.features.terminal.pictures import (
    use_picture_attachment,
)
from relaydeck.client.features.terminal.screen import deliver_output
from relaydeck.client.features.terminal.scrolling import (
    reset_alt_scroll,
    use_open_at_end,
    use_terminal_scroll,
)

if TYPE_CHECKING:
    from relaydeck.client.features.terminal.agent_session import AgentSession
    from relaydeck.client.features.terminal.controller import TerminalController

from relaydeck.client.features.terminal.surfaces.actions import (
    phone_selection_actions,
    request_terminal_keyboard,
    selection_actions,
    use_mobile_key_actions,
)
from relaydeck.client.features.terminal.surfaces.agent_screen import (
    AgentTerminalScreen,
)
from relaydeck.client.features.terminal.surfaces.direct_typing import (
    direct_typing_allowed,
)
from relaydeck.client.features.terminal.surfaces.sessions import (
    TAKEOVER_DISCLOSURE_AGENT,
)


def _send_ready(session: AgentSession) -> bool:
    """The composer would send now (live props, read at tap time)."""
    props = session.composer_props()
    return (
        bool(props["can_send"])
        and props["submission"] != SUBMISSION_SENDING
        and props["review"] is None
    )


async def _copy_selection(session: AgentSession, text: str, anchor: int, focus: int) -> str:
    return await session.copy_selection(text, anchor, focus)


def _writer_boundary_live(
    session: AgentSession,
    token: tuple[int, int] | None,
    boundary: tuple[TerminalController | None, int],
) -> bool:
    """The input token and the (controller, generation) boundary captured at
    render still name the live, foreground, unpaused writer."""
    return (
        token is not None
        and token == session.input_token
        and session.foreground
        and not session.paused
        and not session.disposed
        and session.controller is not None
        and boundary[0] is session.controller
        and boundary[1] == session.controller.generation
    )


def _set_native_keyboard_button(session: AgentSession, enabled: bool) -> None:
    """Show or hide the terminal's native keyboard button."""
    term = session.current_term
    diagnostics_logger().info("keyboard_button enabled=%s term=%s", enabled, term is not None)
    if term is not None:
        term.set_keyboard_button(enabled)


def use_keyboard_state_workflow(session: AgentSession) -> None:
    """The native keyboard button reports KEYBOARD_STATE. Shown
    from Compose, it switches the workflow to Direct (draft kept); the keyboard already
    opened inside the tap, so no request is sent from here."""
    keyboard_unsubscribe: Any = ft.use_ref(None)
    native_term = session.current_term

    def _attach_keyboard_state() -> None:
        if native_term is None:
            return

        def on_keyboard_state(shown: bool) -> None:
            diagnostics_logger().info(
                "keyboard_state shown=%s workflow=%s", shown, session.view().workflow
            )
            if shown and session.view().workflow == WORKFLOW_AGENT_COMPOSER:
                session.set_workflow(WORKFLOW_AGENT_DIRECT)

        keyboard_unsubscribe.current = native_term.add_keyboard_state_listener(on_keyboard_state)

    def _detach_keyboard_state() -> None:
        if keyboard_unsubscribe.current is not None:
            keyboard_unsubscribe.current()
            keyboard_unsubscribe.current = None

    ft.use_effect(
        _attach_keyboard_state,
        dependencies=[id(native_term)],
        cleanup=_detach_keyboard_state,
    )


def _use_foreground(session: AgentSession) -> None:
    """Log foreground transitions; a terminal brought to the front shows its newest output."""
    use_open_at_end(session)

    def _trace_foreground() -> None:
        term = session.current_term
        diagnostics_logger().info(
            "term_foreground term=%s session=%s gen=%s token=%s foreground=%s",
            id(term) % 100000 if term is not None else 0,
            id(session) % 100000,
            getattr(term, "viewer_generation", 0),
            (session._view_token or 0) % 100000,
            session.foreground,
        )

    ft.use_effect(_trace_foreground, dependencies=[session, session.foreground])


@ft.component
def mount_agent_screen(
    session: AgentSession,
    *,
    selection_text: str = "",
    dark: bool = True,
    on_back: Callable[[], Any] | None = None,
    viewport_height: float = 800,
    viewport_width: float = 0.0,
    on_open_shell: Callable[[], Any] | None = None,
    title: str = "",
    host_line: str = "",
    confirm_takeover: bool = True,
    show_key_row: bool = True,
    haptics: bool = True,
    font_size: float | None = None,
    phone: bool = False,
    width_tier: str | None = None,
    keyboard_open: bool = False,
    touch: bool = False,
    connection_text: str = "",
    connection_live: bool = True,
    on_connection_action: Callable[[], Any] | None = None,
    agent_tabs: tuple[tuple[str, str, str], ...] = (),
    selected_agent: str = "",
    on_agent_tab: Callable[[str], Any] | None = None,
    on_operations: Callable[[], Any] | None = None,
    typing_split: bool = False,
    on_composer_focus: Callable[[bool], Any] | None = None,
    on_keep_typing: Callable[[], Any] | None = None,
    on_typing_exit: Callable[[str], Any] | None = None,
    keyboard_up: bool = False,
) -> Any:
    """Declarative agent mount: `AgentTerminalScreen` props
    derived from one live `AgentSession`, every callback bound to a session
    method. The subscription bumps a tick on each mutation and is released on
    unmount; the terminal is bound to the output sink on mount and unbound on
    unmount. Input callbacks capture the live writer token at render.
    """
    tick, set_tick = ft.use_state(0)
    initial_typed_boundary: tuple[TerminalController | None, int] = (None, 0)
    typed_boundary, set_typed_boundary = ft.use_state(initial_typed_boundary)
    unsubscribe_box: Any = ft.use_ref(None)

    def _subscribe() -> None:
        if unsubscribe_box.current is not None:
            unsubscribe_box.current()
        unsubscribe_box.current = session.subscribe(
            lambda changed: set_tick(lambda current: current + 1)
        )

    def _release() -> None:
        if unsubscribe_box.current is not None:
            unsubscribe_box.current()
            unsubscribe_box.current = None
        session.unbind_view()

    ft.use_effect(_subscribe, dependencies=[session], cleanup=_release)

    _use_foreground(session)
    use_keyboard_state_workflow(session)

    # Read the tick to render each session mutation.
    _ = tick
    mobile_item, modifier_states, mobile_input = use_mobile_key_actions(
        session, active=(width_tier or ("compact" if phone else "expanded")) != "expanded"
    )
    picture_props = use_picture_attachment(session, mobile_input, expose_sources=True)
    paste_props = take_paste_props(picture_props)
    scroll = use_terminal_scroll(session, mobile_input, touch=touch)
    native_scroll: Callable[[int, bytes], Awaitable[str | None]] = scroll.pop("native_scroll")
    open_selection, move_selection, close_selection = selection_actions(session, phone=phone)
    props = session.composer_props()
    callbacks = session.composer_callbacks()

    async def send_composer() -> Any:
        result = await callbacks["on_send"]()
        if result in (SEND_DELIVERED_VIA_PROMPT, SEND_DELIVERED_VIA_ATTACH):
            reset_alt_scroll(session)
        return result

    async def confirm_composer() -> Any:
        result = await callbacks["on_confirm_blocked"]()
        if result:
            reset_alt_scroll(session)
        return result

    view = session.view()
    token = session.input_token
    input_boundary = (
        session.controller,
        session.controller.generation if session.controller is not None else 0,
    )
    interaction = session.interaction_generation
    sheet_text = selection_text or session.selection_sheet_text
    selection_binding, phone_copy, can_phone_select = phone_selection_actions(session, sheet_text)
    direct_input = view.workflow != WORKFLOW_AGENT_COMPOSER

    async def _on_input(origin: int, data: bytes) -> str:
        scrolled = await native_scroll(origin, data)
        if scrolled is not None:
            return scrolled
        if not direct_input and scroll["scroll_input_enabled"]:
            # Alternate-buffer scrolling may enable native gesture handling
            # in Compose, but never native typing or changes to its draft.
            return ROUTE_DROPPED
        result = await mobile_input(origin, data)
        if (
            result == ROUTE_PTY
            and data
            and _writer_boundary_live(session, token, input_boundary)
            and typed_boundary != input_boundary
        ):
            set_typed_boundary(input_boundary)
        return result

    async def _on_direct_input(
        origin: int,
        data: bytes | tuple[bytes, ...],
        *,
        can_input: Callable[[], bool] | None = None,
        paste_text: str | None = None,
    ) -> str:
        return await mobile_input(
            origin, data, direct_only=True, can_input=can_input, paste_text=paste_text
        )

    def _on_terminal(term: Any) -> None:
        if term is None:
            session.unbind_view()
        else:
            session.bind_view(term, write=deliver_output)

    async def _take_over() -> str:
        session.log_reopen("take_over")
        # Arm disclosure before confirmation, even when one press performs both.
        session.request_takeover()
        if confirm_takeover:
            return TAKEOVER_DISCLOSURE_AGENT
        return await session.confirm_takeover()

    screen_props = dict(
        **picture_props,
        draft_restore_marker=(id(session), id(session.last_resume_restore)),
        **scroll,
        terminal=session.term_state,
        selection_text=sheet_text,
        selection_binding=selection_binding,
        on_phone_copy=haptic_feedback.on_phone_copy(phone_copy, haptics),
        can_phone_select=can_phone_select,
        selection_font_size=getattr(session.current_term, "font_size", font_size or 14),
        agent_title=title or session.title,
        agent_status=session.agent_status_label,
        workflow=view.workflow,
        keyboard=view.keyboard,
        draft=props["draft"],
        markers=props["markers"],
        can_send=props["can_send"],
        blockers=props["blockers"],
        submission=props["submission"],
        error=props["error"],
        blocked_review=props["review"],
        input_mode=props["input_mode"],
        paste_review=session.pending_review,
        paste_error=session.paste_error,
        armed=session.armed_modifiers,
        keys_enabled=session.keys_enabled,
        conflict_owner=session.conflict_owner,
        takeover_disclosure=session.takeover_disclosure,
        on_jump_to_bottom=session.jump_to_bottom,
        on_size_report=session.resize,
        on_input=_on_input,
        on_direct_input=_on_direct_input,
        direct_binding=(session, token, session.interaction_generation),
        can_direct_type=lambda: direct_typing_allowed(session, token, interaction),
        can_open_keyboard=lambda: direct_typing_allowed(
            session, token, interaction, direct_only=False
        ),
        on_open_selection=open_selection,
        on_copy_selection=haptic_feedback.on_copy(
            functools.partial(_copy_selection, session, sheet_text), haptics
        ),
        on_close_selection=close_selection,
        on_selection_change=move_selection,
        on_draft_change=callbacks["on_draft_change"],
        on_send=haptic_feedback.on_send(send_composer, haptics, lambda: _send_ready(session)),
        on_confirm_blocked=confirm_composer,
        on_cancel_blocked=callbacks["on_cancel_blocked"],
        on_clear_error=callbacks["on_clear_error"],
        on_workflow_change=session.set_workflow,
        on_keyboard_change=session.set_keyboard,
        on_request_keyboard=lambda: request_terminal_keyboard(session, token, agent_direct=True),
        on_keyboard_button=lambda enabled: _set_native_keyboard_button(session, enabled),
        on_key=session.press_key,
        on_toggle_modifier=session.toggle_modifier,
        on_dismiss_keys=lambda: session.set_keyboard(KEYBOARD_TEXT),
        on_confirm_paste=session.confirm_paste,
        on_cancel_paste=session.cancel_paste,
        on_takeover=_take_over,
        on_confirm_takeover=session.confirm_takeover,
        on_cancel_takeover=session.cancel_takeover,
        input_enabled=direct_input and session.foreground and not session.paused,
        on_terminal=_on_terminal,
        on_mode_report=session.set_bracketed_paste,
        dark=dark,
        on_back=on_back,
        viewport_height=viewport_height,
        viewport_width=viewport_width,
        on_open_shell=on_open_shell,
        phase="reconnecting" if session.recovering else view.phase,
        host_line=host_line,
        notice=view.notice,
        attached_output=session.controller is not None or len(session.output_log) > 0,
        on_disconnect=session.disconnect,
        on_reconnect=session.reconnect,
        on_paste=session.paste_from_clipboard,
        confirm_takeover=confirm_takeover,
        control_pending=session.control_pending,
        expected_size=session.expected_geometry,
        show_key_row=show_key_row,
        font_size=font_size,
        phone=phone,
        width_tier=width_tier,
        keyboard_open=keyboard_open,
        touch=touch,
        connection_text=connection_text,
        connection_live=connection_live,
        on_connection_action=on_connection_action,
        agent_tabs=agent_tabs,
        selected_agent=selected_agent,
        on_agent_tab=on_agent_tab,
        dot_blink=session.foreground and not session.paused,
        shown=session.foreground and not session.paused,
        on_operations=on_operations,
        typing_split=typing_split,
        on_composer_focus=on_composer_focus,
        on_keep_typing=on_keep_typing,
        on_typing_exit=on_typing_exit,
        keyboard_up=keyboard_up,
        on_mobile_item=mobile_item,
        modifier_states=modifier_states,
        cached_screen=session.cached_screen,
        composer_focusable=session.foreground
        and not session.paused
        and not session.disposed
        and not session.draft_loading,
        received_input=session.controller is not None and typed_boundary == input_boundary,
    )
    screen = AgentTerminalScreen(**view_actions(session, screen_props))
    return wrap_with_paste_region(screen, paste_props, key="agent-picture-paste")
