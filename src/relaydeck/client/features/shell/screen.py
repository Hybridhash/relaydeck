"""Declarative shell terminal screen.

`ShellScreen` is fully controlled: immutable props plus callbacks, no
sockets, no timers, no direct session access. It hosts the terminal
surface (with selection), the input row (Paste, Text/Keys mode,
Disconnect/Reconnect, Back/Close Terminal), the tools-keys dock, the
Review Paste sheet, and the connecting/conflict/disconnected/exited
overlays plus the "Close Terminal?" confirm.

Structure: the input row sits above the keyboard as ordinary content, Keys mode
mounts the app-side dock, the More menu carries Back + Close Terminal,
and the ended overlay offers Reattach (+ Take Over). There is
intentionally no Composer, agent switcher, staged state, notification,
or agent operation on this surface (asserted by tests).
"""

from __future__ import annotations

import functools
from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import Any

import flet as ft

from relaydeck.client.components.design_system import tokens as console_tokens
from relaydeck.client.components.design_system.surfaces import MutedText, SurfaceFrame
from relaydeck.client.components.design_system.terminal_window import (
    STATE_CONNECTING,
    STATE_DISCONNECTED,
    STATE_EXITED,
    STATE_IN_CONTROL,
    STATE_WATCHING,
    DirectInputStrip,
    MobileKeyBar,
    MoreMenu,
    NoticePanel,
    PhoneSelectionSheet,
    PillButton,
    SegmentedSwitch,
    StatePill,
    TakeoverPanel,
    TerminalHeader,
    WindowFrame,
)
from relaydeck.client.features.shell.session import (
    CLOSE_MESSAGE,
    CLOSE_TITLE,
    SHELL_CONFLICT,
    SHELL_CONNECTING,
    SHELL_DISCONNECTED,
    SHELL_EXITED,
    SHELL_LIVE,
    ShellSession,
    ShellViewState,
)
from relaydeck.client.features.terminal import haptics as haptic_feedback
from relaydeck.client.features.terminal.callbacks import view_actions
from relaydeck.client.features.terminal.events import click
from relaydeck.client.features.terminal.input_modes import KEYBOARD_KEYS, KEYBOARD_TEXT
from relaydeck.client.features.terminal.key_bar import KeyBarItem
from relaydeck.client.features.terminal.keys import ToolsKeysDock
from relaydeck.client.features.terminal.models import SelectionState, TerminalViewState
from relaydeck.client.features.terminal.paste_policy import PasteReview
from relaydeck.client.features.terminal.paste_review import PasteReviewSheet
from relaydeck.client.features.terminal.picture_paste_region import (
    take_paste_props,
    wrap_with_paste_region,
)
from relaydeck.client.features.terminal.pictures import attach_button, use_picture_attachment
from relaydeck.client.features.terminal.screen import TerminalScreen, deliver_output
from relaydeck.client.features.terminal.scrolling import use_open_at_end, use_terminal_scroll
from relaydeck.client.features.terminal.surfaces import (
    PASTE_FALLBACK_HINT,
    DirectInput,
    DirectTypingField,
    direct_typing_allowed,
    phone_selection_actions,
    request_terminal_keyboard,
    selection_actions,
    use_direct_keyboard,
    use_mobile_key_actions,
    use_paste_fallback,
)
from relaydeck.client.theme import palette

SHELL_SCREEN_KEY = "shell-screen"
SHELL_TITLE_KEY = "shell-title"
SHELL_INPUT_ROW_KEY = "shell-input-row"
SHELL_PASTE_KEY = "shell-paste"
SHELL_MODE_TEXT_KEY = "shell-mode-text"
SHELL_MODE_KEYS_KEY = "shell-mode-keys"
SHELL_DISCONNECT_KEY = "shell-disconnect"
SHELL_RECONNECT_KEY = "shell-reconnect"
SHELL_BACK_KEY = "shell-back"
SHELL_CLOSE_KEY = "shell-close"
SHELL_NEW_TERMINAL_KEY = "shell-new-terminal"
SHELL_TAKEOVER_KEY = "shell-takeover"
SHELL_CONFLICT_KEY = "shell-conflict"
SHELL_CONFLICT_OWNER_KEY = "shell-conflict-owner"
SHELL_CONNECTING_KEY = "shell-connecting"
SHELL_DISCONNECTED_KEY = "shell-disconnected"
SHELL_RECONNECTING_KEY = "shell-reconnecting"
SHELL_EXITED_KEY = "shell-exited"
SHELL_EXITED_DETAIL_KEY = "shell-exited-detail"
SHELL_NOTICE_KEY = "shell-notice"
SHELL_CLOSE_CONFIRM_KEY = "shell-close-confirm-box"
SHELL_CLOSE_CONFIRM_TITLE_KEY = "shell-close-confirm-title"
SHELL_CLOSE_CONFIRM_MESSAGE_KEY = "shell-close-confirm-message"
SHELL_CLOSE_CONFIRM_BUTTON_KEY = "shell-close-confirm"
SHELL_CLOSE_CANCEL_BUTTON_KEY = "shell-close-cancel"
SHELL_PASTE_ERROR_KEY = "shell-paste-error"
SHELL_HEADER_KEY = "shell-header"
SHELL_CONFLICT_FRAME_KEY = "shell-conflict-frame"
SHELL_DISCONNECTED_FRAME_KEY = "shell-disconnected-frame"
SHELL_EXITED_FRAME_KEY = "shell-exited-frame"
SHELL_INPUT_GROUP_KEY = "shell-input-group"
SHELL_INPUT_LABEL_KEY = "shell-input-label"
SHELL_KEYS_SCROLL_KEY = "shell-keys-scroll"
SHELL_PASTE_FRAME_KEY = "shell-paste-frame"
SHELL_CLOSE_FRAME_KEY = "shell-close-frame"


TEXT_DISABLED = console_tokens.SHELL_DISABLED
SHELL_MORE_KEY = "shell-more"
SHELL_MORE_NEW_KEY = "shell-more-new-terminal"
SHELL_DIRECT_TEXT = "Direct input is on. What you type goes straight to the shell."
SHELL_COMPOSE_KEY = "shell-workflow-compose"
SHELL_DIRECT_KEY = "shell-workflow-direct"
SHELL_TAKEOVER_TITLE = "Take over this terminal?"
SHELL_TAKEOVER_MESSAGE = (
    "You will be able to type here. The terminal may resize, and the client "
    "attached to it will be disconnected. "
    "While you control this pane it uses this device's size; it returns when you leave."
)


def shell_window_state(phase: str) -> str:
    return {
        SHELL_LIVE: STATE_IN_CONTROL,
        SHELL_CONFLICT: STATE_WATCHING,
        SHELL_DISCONNECTED: STATE_DISCONNECTED,
        SHELL_EXITED: STATE_EXITED,
    }.get(phase, STATE_CONNECTING)


def _ignore_tap(_event: Any) -> None:
    """Flet needs one GestureDetector handler; watching taps do nothing."""


@ft.component
def ShellScreen(
    view: ShellViewState,
    terminal: TerminalViewState,
    selection_text: str,
    review: PasteReview | None,
    paste_error: str | None,
    armed: frozenset[str],
    keys_enabled: bool,
    on_jump_to_bottom: Callable[[], Any],
    on_size_report: Callable[[int, int], Any],
    on_input: Callable[[int, bytes], Any],
    on_open_selection: Callable[[], Any],
    on_copy_selection: Callable[[int, int], Any],
    on_close_selection: Callable[[], Any],
    on_paste: Callable[[], Any],
    on_mode_change: Callable[[str], Any],
    on_key: Callable[[str], Any],
    on_toggle_modifier: Callable[[str], Any],
    on_dismiss_keys: Callable[[], Any],
    on_confirm_paste: Callable[[], Any],
    on_cancel_paste: Callable[[], Any],
    on_disconnect: Callable[[], Any],
    on_reconnect: Callable[[], Any],
    on_takeover: Callable[[], Any],
    on_back: Callable[[], Any],
    on_close_terminal: Callable[[], Any],
    on_confirm_close: Callable[[], Any],
    on_cancel_close: Callable[[], Any],
    on_new_terminal: Callable[[], Any],
    *,
    input_enabled: bool = True,
    on_terminal: Callable[[Any | None], Any] | None = None,
    on_selection_change: Callable[[int, int], Any] | None = None,
    on_mode_report: Callable[[bool], Any] | None = None,
    dark: bool = True,
    viewport_height: float = 800,
    title: str = "",
    host_line: str = "",
    show_back: bool = True,
    confirm_takeover: bool = True,
    show_key_row: bool = True,
    font_size: float | None = None,
    phone: bool = False,
    width_tier: str | None = None,
    keyboard_open: bool = False,
    touch: bool = False,
    connection_text: str = "",
    connection_live: bool = True,
    on_connection_action: Callable[[], Any] | None = None,
    on_operations: Callable[[], Any] | None = None,
    on_mobile_item: Callable[[KeyBarItem, bool], Any] | None = None,
    modifier_states: dict[str, str] | None = None,
    on_request_keyboard: Callable[[], Any] | None = None,
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
    picture_progress: Any = None,
    pictures: tuple[Any, ...] = (),
    on_remove_picture: Any = None,
    cached_screen: str = "",
) -> Any:
    """Shell detail in the terminal window.

    Header: name, mono host line, state pill, Disconnect, and a More
    menu (New terminal, Close terminal with confirmation). Back is a
    header arrow only when `show_back` (phone; wide layouts pick from
    the list). Bottom area by phase: in control shows the key row and
    the input row (shells are Direct only, so Compose is shown disabled
    and there is no composer); an ownership conflict shows the take-over
    panel; disconnected offers Reattach; exited offers New terminal.
    No composer, switcher, staged state, or agent operation on this
    surface."""
    tier = width_tier or ("compact" if phone else "expanded")
    phone = tier != "expanded"

    typing_open, set_typing_open = ft.use_state(False)

    def request_keyboard() -> None:
        set_typing_open(True)
        on_mode_change(KEYBOARD_TEXT)
        if on_request_keyboard:
            on_request_keyboard()

    window_size, set_window_size = ft.use_state((0, 0))

    def report_size(cols: int, rows: int) -> Any:
        set_window_size((cols, rows))
        return on_size_report(cols, rows)

    tokens = palette(dark)
    window = shell_window_state(view.phase)
    if terminal.lease_mode == "view" and window == STATE_IN_CONTROL:
        window = STATE_WATCHING
    in_control = window == STATE_IN_CONTROL
    (
        direct_field,
        focus_requests,
        focus_handled,
        open_keyboard,
        toggle_keyboard,
        _close_keyboard,
        keyboard_closing,
    ) = use_direct_keyboard(
        opened=typing_open,
        enabled=phone
        and in_control
        and keys_enabled
        and input_enabled
        and not terminal.selection.open,
        binding=direct_binding,
        on_open=request_keyboard,
        on_close=lambda: set_typing_open(False),
        can_open=can_open_keyboard,
    )
    actions: list[Any] = []
    if not phone:
        actions.append(
            attach_button(on_attach_picture, enabled=attach_enabled, action=picture_action)
        )
    on_paste, paste_hint, clear_paste_hint = use_paste_fallback(
        on_paste, open_keyboard, direct_binding, can_open_keyboard
    )
    menu: list[tuple[str, str, Callable[[], Any]]] = []
    if in_control:
        if phone:
            menu.append((SHELL_DISCONNECT_KEY, "Release", on_disconnect))
        else:
            actions.append(
                PillButton("Release", on_disconnect, key=SHELL_DISCONNECT_KEY, tone="outline")
            )
    if phone and on_operations is not None:
        menu.append(("terminal-operations", "Operations to review", on_operations))
    menu.append((SHELL_MORE_NEW_KEY, "New terminal", on_new_terminal))
    if view.phase not in (SHELL_EXITED,):
        menu.append((SHELL_CLOSE_KEY, "Close terminal…", on_close_terminal))
    label = title or f"Terminal {view.terminal_id}"
    actions.append(
        MoreMenu(
            menu,
            key=SHELL_MORE_KEY,
            compact=phone,
            information=(
                (f"{SHELL_TITLE_KEY}-host", host_line or "shell"),
                ("terminal-window-size", f"{window_size[0]} x {window_size[1]}"),
                (
                    "terminal-window-status",
                    f"{terminal.status} · {terminal.lease_mode} · "
                    f"{terminal.epoch}@{terminal.applied_offset}",
                ),
            )
            if phone
            else (),
        )
    )
    controls: list[Any] = [
        ft.Container(
            key=SHELL_HEADER_KEY,
            content=TerminalHeader(
                title=label,
                host_line=host_line or "shell",
                pills=[StatePill(window, key="shell-window-state", compact=phone)],
                actions=actions,
                on_back=on_back if show_back else None,
                title_key=SHELL_TITLE_KEY,
                back_key=SHELL_BACK_KEY,
                phone=phone,
                mobile_terminal=phone,
                connection_text=connection_text,
                connection_live=connection_live and in_control,
                on_connection_action=on_connection_action,
            ),
        ),
    ]
    if view.notice:
        controls.append(ft.Text(view.notice, key=SHELL_NOTICE_KEY, color=tokens["muted"], size=13))
    controls.append(
        ft.Container(
            key="shell-terminal-host",
            expand=True,
            content=ft.GestureDetector(
                key="terminal-direct-tap",
                on_tap=click(open_keyboard) if phone and in_control else _ignore_tap,
                content=TerminalScreen(
                    cached_screen=cached_screen,
                    state=replace(terminal, selection=SelectionState()) if phone else terminal,
                    on_jump_to_bottom=on_jump_to_bottom,
                    on_size_report=report_size,
                    on_input=on_input,
                    on_open_selection=on_open_selection,
                    selection_text=selection_text,
                    on_copy_selection=on_copy_selection,
                    on_close_selection=on_close_selection,
                    on_selection_change=on_selection_change,
                    input_enabled=input_enabled and not terminal.selection.open,
                    on_terminal=on_terminal,
                    on_mode_report=on_mode_report,
                    dark=dark,
                    on_key=on_key,
                    keys_enabled=keys_enabled and in_control,
                    key_row=in_control,
                    show_keys=show_key_row,
                    on_paste=on_paste,
                    paste_enabled=keys_enabled and in_control,
                    on_more_keys=lambda: on_mode_change(
                        KEYBOARD_TEXT if view.keyboard == KEYBOARD_KEYS else KEYBOARD_KEYS
                    ),
                    more_keys_open=view.keyboard == KEYBOARD_KEYS,
                    more_keys_key=SHELL_MODE_TEXT_KEY
                    if view.keyboard == KEYBOARD_KEYS
                    else SHELL_MODE_KEYS_KEY,
                    paste_key=SHELL_PASTE_KEY,
                    font_size=font_size,
                    phone=phone,
                    compact=tier == "compact",
                    touch=touch,
                    scroll_state=scroll_state,
                    scroll_binding=scroll_binding,
                    on_view_report=on_view_report,
                    on_scroll=on_scroll,
                    can_scroll=can_scroll,
                    scroll_input_enabled=scroll_input_enabled and not terminal.selection.open,
                    hide_shortcuts=phone,
                ),
            ),
        )
    )
    # Status sits above the key rows: below them it overlapped the 📎/⌨ tap
    # targets and swallowed taps on phones.
    if picture_progress is not None:
        controls.append(picture_progress)
    if phone:
        controls.append(
            MobileKeyBar(  # type: ignore[call-arg]
                key="shell-mobile-keybar",
                on_item=on_mobile_item or (lambda item, hold: None),
                modifier_states=modifier_states or {},
                enabled=keys_enabled and in_control and not terminal.selection.open,
                on_paste=on_paste if in_control else None,
                on_select=on_open_selection,
                visible=in_control or terminal.applied_offset > 0,
                medium=tier == "medium",
                show_keys=show_key_row and in_control,
                paste_key=SHELL_PASTE_KEY,
            )
        )
        typing_field = DirectTypingField(
            on_input=on_direct_input or on_input,
            on_transaction=on_direct_input,
            visible=typing_open,
            enabled=in_control
            and keys_enabled
            and input_enabled
            and not terminal.selection.open
            and review is None
            and not keyboard_closing,
            binding=direct_binding,
            field_ref=direct_field,
            focus_requests=focus_requests,
            focus_handled=focus_handled,
            field_key="shell-mode-text-typing",
            dark=dark,
            can_input=can_direct_type,
            paste_fallback=paste_hint,
            on_input_handled=clear_paste_hint,
        )
        controls.append(
            ft.Row(
                key="shell-keyboard-row",
                height=40,
                visible=in_control,
                alignment=ft.MainAxisAlignment.END,
                controls=[
                    ft.Container(
                        key="shell-typing-slot", expand=True, height=36, content=typing_field
                    ),
                    attach_button(
                        on_attach_picture,
                        enabled=attach_enabled,
                        action=picture_action,
                        compact=tier == "compact",
                    ),
                    ft.Semantics(
                        key="shell-keyboard-semantics",
                        button=True,
                        selected=typing_open and not keyboard_closing,
                        label="Terminal keyboard",
                        content=ft.IconButton(
                            ft.Icons.KEYBOARD,
                            key="shell-keyboard-toggle",
                            width=32,
                            height=32,
                            icon_size=16,
                            padding=0,
                            selected=typing_open and not keyboard_closing,
                            selected_icon_color=tokens["accent_text"],
                            tooltip=None
                            if tier == "compact"
                            else ("Close keyboard" if typing_open else "Open Direct typing"),
                            on_click=click(toggle_keyboard),
                        ),
                    ),
                ],
            )
        )
        controls.append(
            ft.Text(
                PASTE_FALLBACK_HINT,
                key="shell-direct-paste-hint",
                visible=paste_hint and typing_open,
                size=12,
                color=tokens["muted"],
            )
        )
    if view.phase == SHELL_CONNECTING:
        controls.append(MutedText("Connecting…", key=SHELL_CONNECTING_KEY, tokens=tokens))
    if view.phase == SHELL_CONFLICT or window == STATE_WATCHING:
        controls.append(
            ft.Container(
                key=SHELL_CONFLICT_FRAME_KEY,
                content=ft.Column(
                    key=SHELL_CONFLICT_KEY,
                    controls=[
                        *(
                            []
                            if phone
                            else [
                                ft.Text(
                                    f"Terminal owned by {view.owner_label}",
                                    key=SHELL_CONFLICT_OWNER_KEY,
                                    color=tokens["muted"],
                                )
                            ]
                        ),
                        TakeoverPanel(
                            phone=phone,
                            title=SHELL_TAKEOVER_TITLE,
                            message=(
                                f"{view.owner_label} owns this terminal. {SHELL_TAKEOVER_MESSAGE}"
                                if phone
                                else SHELL_TAKEOVER_MESSAGE
                            ),
                            on_take_over=on_takeover,
                            on_keep_watching=None,
                            confirm=confirm_takeover,
                            take_over_key=SHELL_TAKEOVER_KEY,
                        ),
                    ],
                ),
            )
        )
    if view.phase == SHELL_DISCONNECTED and view.reconnecting:
        controls.append(
            ft.Text("Reconnecting…", key=SHELL_RECONNECTING_KEY, color=tokens["muted"], size=13)
        )
    elif view.phase == SHELL_DISCONNECTED:
        controls.append(
            ft.Container(
                key=SHELL_DISCONNECTED_FRAME_KEY,
                content=ft.Column(
                    key=SHELL_DISCONNECTED_KEY,
                    controls=[
                        NoticePanel(
                            phone=phone,
                            key="shell-disconnected-panel",
                            title="Disconnected",
                            message="The shell keeps running on the host.",
                            actions=[
                                PillButton(
                                    "Reattach",
                                    on_reconnect,
                                    key=SHELL_RECONNECT_KEY,
                                    tone="primary",
                                )
                            ],
                        )
                    ],
                ),
            )
        )
    if view.phase == SHELL_EXITED:
        controls.append(
            ft.Container(
                key=SHELL_EXITED_FRAME_KEY,
                content=ft.Column(
                    key=SHELL_EXITED_KEY,
                    controls=[
                        ft.Text(
                            "Shell exited.", key=SHELL_EXITED_DETAIL_KEY, color=tokens["muted"]
                        ),
                        NoticePanel(
                            phone=phone,
                            key="shell-exited-panel",
                            title="Shell exited",
                            message="Open a new terminal to keep working.",
                            actions=[
                                PillButton(
                                    "New terminal",
                                    on_new_terminal,
                                    key=SHELL_NEW_TERMINAL_KEY,
                                    tone="primary",
                                )
                            ],
                        ),
                    ],
                ),
            )
        )
    if in_control:
        switch = SegmentedSwitch(
            (
                ("compose", "Compose", SHELL_COMPOSE_KEY),
                ("direct", "Direct", SHELL_DIRECT_KEY),
            ),
            "direct",
            lambda value: None,
            key=SHELL_INPUT_ROW_KEY,
        )
        compose = switch.content.controls[0]
        compose.disabled = True
        compose.tooltip = "Shells take direct input only"
        compose.content.content.style.color = TEXT_DISABLED
        strip = DirectInputStrip(SHELL_DIRECT_TEXT, key="shell-direct-note")
        axes: Any = ft.Row(
            key="shell-input-axes",
            spacing=10,
            controls=[switch, ft.Container(expand=True, content=strip)],
        )
        # Shells have no composer/workflow switch. On a phone the key row
        # already supplies every action; remove the redundant Direct strip.
        input_controls: list[Any] = [] if phone else [axes]
        if view.keyboard == KEYBOARD_KEYS and not phone:
            input_controls.append(
                ft.Column(
                    key=SHELL_KEYS_SCROLL_KEY,
                    scroll=ft.ScrollMode.AUTO,
                    controls=[
                        ToolsKeysDock(
                            armed=armed,
                            enabled=keys_enabled,
                            on_key=on_key,
                            on_toggle_modifier=on_toggle_modifier,
                            on_dismiss=on_dismiss_keys,
                            presentation="terminal",
                            dark=dark,
                            max_height=96 if phone else max(96, min(224, viewport_height - 400)),
                        )
                    ],
                )
            )
        if input_controls:
            controls.append(
                ft.Container(
                    key=SHELL_INPUT_GROUP_KEY,
                    disabled=terminal.selection.open,
                    content=ft.Column(tight=True, spacing=8, controls=input_controls),
                )
            )
    if review is not None:
        controls.append(
            SurfaceFrame(
                SHELL_PASTE_FRAME_KEY,
                PasteReviewSheet(
                    review=review,
                    on_confirm=on_confirm_paste,
                    on_cancel=on_cancel_paste,
                ),
                tokens=tokens,
            )
        )
    if paste_error is not None:
        controls.append(ft.Text(paste_error, key=SHELL_PASTE_ERROR_KEY))
    if view.confirm_close:
        controls.append(
            ft.Container(
                key=SHELL_CLOSE_FRAME_KEY,
                content=ft.Column(
                    key=SHELL_CLOSE_CONFIRM_KEY,
                    controls=[
                        ft.Text(CLOSE_TITLE, key=SHELL_CLOSE_CONFIRM_TITLE_KEY, visible=False),
                        NoticePanel(
                            phone=phone,
                            key="shell-close-panel",
                            title=CLOSE_TITLE,
                            message=CLOSE_MESSAGE,
                            actions=[
                                PillButton(
                                    "Cancel",
                                    on_cancel_close,
                                    key=SHELL_CLOSE_CANCEL_BUTTON_KEY,
                                    tone="outline",
                                ),
                                PillButton(
                                    "Close terminal",
                                    on_confirm_close,
                                    key=SHELL_CLOSE_CONFIRM_BUTTON_KEY,
                                    tone="primary",
                                ),
                            ],
                        ),
                        ft.Text(CLOSE_MESSAGE, key=SHELL_CLOSE_CONFIRM_MESSAGE_KEY, visible=False),
                    ],
                ),
            )
        )
    frame = WindowFrame(controls, key="shell-window", phone=phone)
    frame.content.key = SHELL_SCREEN_KEY
    return ft.Stack(
        key="phone-select-stack",
        expand=True,
        fit=ft.StackFit.EXPAND,
        controls=[
            frame,
            PhoneSelectionSheet(
                visible=phone and terminal.selection.open,
                text=selection_text,
                anchor=terminal.selection.anchor,
                focus=terminal.selection.focus,
                error=terminal.selection.error,
                binding=selection_binding,
                on_copy=on_phone_copy,
                on_close=on_close_selection,
                on_selection_change=on_selection_change,
                can_interact=can_phone_select,
                font_size=selection_font_size,
                dark=dark,
                alt=bool(scroll_state and scroll_state.alt),
            ),
        ],
    )


async def close_and_leave(session: ShellSession, on_back: Callable[[], Any]) -> str:
    """An accepted "Close terminal" returns to the terminal list instead of
    leaving the exited screen up. A failed close keeps the screen (the terminal stays listed)."""
    phase = await session.close_terminal()
    if phase == SHELL_EXITED:
        on_back()
    return phase


@ft.component
def mount_shell_screen(
    session: ShellSession,
    on_back: Callable[[], Any],
    on_new_terminal: Callable[[], Any],
    *,
    selection_text: str = "",
    dark: bool = True,
    viewport_height: float = 800,
    title: str = "",
    host_line: str = "",
    show_back: bool = True,
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
    on_operations: Callable[[], Any] | None = None,
) -> Any:
    """Bind a controlled shell screen to one live session.

    Subscribe for the mount lifetime, bind/unbind the retained renderer, and
    capture the writer token for input callbacks. Shell input never routes to
    a composer. Back and creation remain explicit navigation callbacks.
    """
    tick, set_tick = ft.use_state(0)
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
    use_open_at_end(session)

    # The tick is read so the render depends on it; every session
    # mutation (sync click or background stream) rerenders through it.
    _ = tick
    mobile_item, modifier_states, mobile_input = use_mobile_key_actions(
        session, active=(width_tier or ("compact" if phone else "expanded")) != "expanded"
    )
    picture_props = use_picture_attachment(session, mobile_input, expose_sources=True)
    paste_props = take_paste_props(picture_props)
    scroll = use_terminal_scroll(session, mobile_input, touch=touch)
    native_scroll: Callable[[int, bytes], Awaitable[str | None]] = scroll.pop("native_scroll")
    open_selection, move_selection, close_selection = selection_actions(session, phone=phone)
    token = session.input_token
    interaction = session.interaction_generation
    sheet_text = selection_text or session.selection_sheet_text
    selection_binding, phone_copy, can_phone_select = phone_selection_actions(session, sheet_text)

    async def _on_input(origin: int, data: bytes) -> str:
        scrolled = await native_scroll(origin, data)
        if scrolled is not None:
            return scrolled
        return await mobile_input(origin, data)

    async def _on_copy(anchor: int, focus: int) -> str:
        return await session.copy_selection(sheet_text, anchor, focus)

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

    def _on_dismiss_keys() -> None:
        session.set_keyboard(KEYBOARD_TEXT)

    def _on_terminal(term: Any) -> None:
        if term is None:
            session.unbind_view()
        else:
            session.bind_view(term, write=deliver_output)

    screen_props = dict(
        **picture_props,
        **scroll,
        view=session.view(),
        terminal=session.term_state,
        selection_text=sheet_text,
        selection_binding=selection_binding,
        on_phone_copy=haptic_feedback.on_phone_copy(phone_copy, haptics),
        can_phone_select=can_phone_select,
        selection_font_size=getattr(session.current_term, "font_size", font_size or 14),
        review=session.pending_review,
        paste_error=session.paste_error,
        armed=session.armed_modifiers,
        keys_enabled=session.keys_enabled,
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
        on_copy_selection=haptic_feedback.on_copy(_on_copy, haptics),
        on_close_selection=close_selection,
        on_selection_change=move_selection,
        on_paste=session.paste_from_clipboard,
        on_mode_change=session.set_keyboard,
        on_request_keyboard=lambda: request_terminal_keyboard(session, token),
        on_key=session.press_key,
        on_toggle_modifier=session.toggle_modifier,
        on_dismiss_keys=_on_dismiss_keys,
        on_confirm_paste=session.confirm_paste,
        on_cancel_paste=session.cancel_paste,
        on_disconnect=session.disconnect,
        on_reconnect=session.reconnect,
        on_takeover=session.take_over,
        on_back=on_back,
        on_close_terminal=session.request_close,
        # The confirm button calls its action without an event: no click() wrapper here.
        on_confirm_close=functools.partial(close_and_leave, session, on_back),
        on_cancel_close=session.cancel_close,
        on_new_terminal=on_new_terminal,
        input_enabled=session.foreground and not session.paused,
        on_terminal=_on_terminal,
        on_mode_report=session.set_bracketed_paste,
        dark=dark,
        viewport_height=viewport_height,
        title=title,
        host_line=host_line,
        show_back=show_back,
        confirm_takeover=confirm_takeover,
        show_key_row=show_key_row,
        font_size=font_size,
        phone=phone,
        width_tier=width_tier,
        keyboard_open=keyboard_open,
        touch=touch,
        connection_text=connection_text,
        connection_live=connection_live,
        on_connection_action=on_connection_action,
        on_operations=on_operations,
        on_mobile_item=mobile_item,
        modifier_states=modifier_states,
        cached_screen=session.cached_screen,
    )
    screen = ShellScreen(**view_actions(session, screen_props))
    return wrap_with_paste_region(screen, paste_props, key="shell-picture-paste")


__all__ = [
    "SHELL_BACK_KEY",
    "SHELL_CLOSE_CANCEL_BUTTON_KEY",
    "SHELL_CLOSE_CONFIRM_BUTTON_KEY",
    "SHELL_CLOSE_CONFIRM_KEY",
    "SHELL_CLOSE_CONFIRM_MESSAGE_KEY",
    "SHELL_CLOSE_CONFIRM_TITLE_KEY",
    "SHELL_CLOSE_FRAME_KEY",
    "SHELL_CLOSE_KEY",
    "SHELL_CONFLICT_FRAME_KEY",
    "SHELL_CONFLICT_KEY",
    "SHELL_CONFLICT_OWNER_KEY",
    "SHELL_CONNECTING_KEY",
    "SHELL_DISCONNECTED_FRAME_KEY",
    "SHELL_DISCONNECTED_KEY",
    "SHELL_EXITED_DETAIL_KEY",
    "SHELL_EXITED_FRAME_KEY",
    "SHELL_EXITED_KEY",
    "SHELL_HEADER_KEY",
    "SHELL_INPUT_GROUP_KEY",
    "SHELL_INPUT_LABEL_KEY",
    "SHELL_INPUT_ROW_KEY",
    "SHELL_KEYS_SCROLL_KEY",
    "SHELL_MODE_KEYS_KEY",
    "SHELL_MODE_TEXT_KEY",
    "SHELL_NEW_TERMINAL_KEY",
    "SHELL_NOTICE_KEY",
    "SHELL_PASTE_ERROR_KEY",
    "SHELL_PASTE_FRAME_KEY",
    "SHELL_PASTE_KEY",
    "SHELL_RECONNECT_KEY",
    "SHELL_DISCONNECT_KEY",
    "SHELL_SCREEN_KEY",
    "SHELL_TAKEOVER_KEY",
    "SHELL_TITLE_KEY",
    "ShellScreen",
    "mount_shell_screen",
]
