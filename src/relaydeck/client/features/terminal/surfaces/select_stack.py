"""Selection overlay stack.

Plain builders preserve control ancestry across layouts. State and hooks belong
to the parent component; builders receive only props and callbacks.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

import flet as ft

from relaydeck.client.components.design_system.terminal_window import (
    PhoneSelectionSheet,
)
from relaydeck.client.features.terminal.models import TerminalViewState

if TYPE_CHECKING:
    pass


def build_select_stack(
    *,
    can_phone_select: Callable[[], bool] | None,
    dark: bool,
    dock_overlay: Any,
    frame: Any,
    on_close_selection: Callable[[], Any],
    on_phone_copy: Callable[[str], Awaitable[str | None]] | None,
    on_selection_change: Callable[[int, int], Any] | None,
    phone: bool,
    scroll_state: Any,
    selection_binding: Any,
    selection_font_size: float,
    selection_text: str,
    slot_children: list[Any],
    terminal: TerminalViewState,
    overlays: list[Any] | None = None,
) -> Any:
    """The outer Stack: detail frame, overlay dock, input slots and the phone selection sheet."""
    return ft.Stack(
        key="phone-select-stack",
        expand=True,
        fit=ft.StackFit.EXPAND,
        controls=[
            frame,
            *([dock_overlay] if dock_overlay is not None else []),
            *slot_children,
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
            # Last, so opening or closing them never shifts the layers below.
            *(overlays or []),
        ],
    )
