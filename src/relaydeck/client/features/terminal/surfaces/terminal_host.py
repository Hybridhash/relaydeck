"""Retained terminal host.

Plain builders preserve control ancestry across layouts. State and hooks belong
to the parent component; builders receive only props and callbacks.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from typing import TYPE_CHECKING, Any

import flet as ft

from relaydeck.client.features.terminal.events import click
from relaydeck.client.features.terminal.input_modes import (
    KEYBOARD_KEYS,
    KEYBOARD_TEXT,
)
from relaydeck.client.features.terminal.layout_budget import DetailBudget
from relaydeck.client.features.terminal.models import SelectionState, TerminalViewState
from relaydeck.client.features.terminal.screen import TerminalScreen

if TYPE_CHECKING:
    pass

from relaydeck.client.features.terminal.surfaces.common import (
    AGENT_KEYBOARD_KEYS_KEY,
    AGENT_KEYBOARD_TEXT_KEY,
    AGENT_PASTE_KEY,
    AGENT_WATCHING_HINT,
)
from relaydeck.client.features.terminal.surfaces.direct_typing import (
    _ignore_tap,
)


def build_terminal_host(
    *,
    attached_output: bool,
    budget: DetailBudget,
    cached_screen: str,
    connecting: bool = False,
    hidden: bool = False,
    can_scroll: Callable[[], bool] | None,
    collapse_keys: bool,
    dark: bool,
    font_size: float | None,
    host_height: float,
    in_control: bool,
    input_enabled: bool,
    keyboard: str,
    keys_enabled: bool,
    on_close_selection: Callable[[], Any],
    on_copy_selection: Callable[[int, int], Any],
    on_input: Callable[[int, bytes], Any],
    keyboard_shown: bool,
    on_keyboard: Callable[[], Any] | None = None,
    on_tools: Callable[[], Any] | None = None,
    tools_open: bool = False,
    on_jump_to_bottom: Callable[[], Any],
    on_key: Callable[[str], Any],
    on_keyboard_change: Callable[[str], Any],
    on_mode_report: Callable[[bool], Any] | None,
    on_open_selection: Callable[[], Any],
    on_paste: Callable[[], Any] | None,
    on_scroll: Callable[[int | None], Any] | None,
    on_selection_change: Callable[[int, int], Any] | None,
    on_terminal: Callable[[Any | None], Any] | None,
    on_tap: Callable[[], Any] | None = None,
    on_view_report: Callable[[Any, Any], Any] | None,
    phone: bool,
    report_size: Callable[[int, int], Any],
    scroll_binding: Any,
    scroll_input_enabled: bool,
    scroll_state: Any,
    selection_text: str,
    set_host_height: Callable[[float], Any],
    show_key_row: bool,
    split: bool,
    split_gap: float,
    split_side_width: float,
    terminal: TerminalViewState,
    tier: str,
    touch: bool,
) -> Any:
    """The terminal host: stable key, margin and measured size around `TerminalScreen`."""
    return ft.Container(
        key="terminal-flex-host",
        on_size_change=lambda e: (
            set_host_height(e.height) if abs(e.height - host_height) >= 2 else None
        ),
        expand=True if phone or budget.collapse else 3,
        margin=ft.Margin(
            left=0, top=0, right=split_side_width + split_gap if split else 0, bottom=0
        ),
        content=ft.GestureDetector(
            key="terminal-direct-tap",
            # Taps never open the keyboard; only the keyboard button does.
            on_tap=click(on_tap) if on_tap is not None else _ignore_tap,
            content=TerminalScreen(
                cached_screen=cached_screen,
                connecting=connecting,
                hidden=hidden,
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
                on_more_keys=lambda: on_keyboard_change(
                    KEYBOARD_TEXT if keyboard == KEYBOARD_KEYS else KEYBOARD_KEYS
                ),
                more_keys_open=keyboard == KEYBOARD_KEYS,
                more_keys_key=AGENT_KEYBOARD_TEXT_KEY
                if keyboard == KEYBOARD_KEYS
                else AGENT_KEYBOARD_KEYS_KEY,
                paste_key=AGENT_PASTE_KEY,
                font_size=font_size,
                placeholder=None if attached_output or in_control else AGENT_WATCHING_HINT,
                phone=phone,
                hide_shortcuts=phone or collapse_keys,
                hide_status=collapse_keys,
                compact=tier == "compact",
                touch=touch,
                scroll_state=scroll_state,
                scroll_binding=scroll_binding,
                on_view_report=on_view_report,
                on_scroll=on_scroll,
                can_scroll=can_scroll,
                on_keyboard=on_keyboard,
                keyboard_shown=keyboard_shown,
                on_tools=on_tools,
                tools_open=tools_open,
                scroll_input_enabled=scroll_input_enabled and not terminal.selection.open,
            ),
        ),
    )
