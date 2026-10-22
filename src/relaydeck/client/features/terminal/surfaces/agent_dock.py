"""Tools-key dock.

Plain builders preserve control ancestry across layouts. State and hooks belong
to the parent component; builders receive only props and callbacks.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import flet as ft

from relaydeck.client.features.terminal.input_modes import (
    KEYBOARD_KEYS,
)
from relaydeck.client.features.terminal.keys import (
    ToolsKeysDock,
)
from relaydeck.client.features.terminal.layout_budget import PICTURE_HEIGHT, DetailBudget
from relaydeck.client.features.terminal.models import TerminalViewState

if TYPE_CHECKING:
    pass

from relaydeck.client.features.terminal.surfaces.common import (
    AGENT_KEYS_SCROLL_KEY,
)


def build_agent_dock(
    *,
    armed: frozenset[str],
    bottom: list[Any],
    budget: DetailBudget,
    collapse_keys: bool,
    composer_focusable: bool,
    dark: bool,
    in_control: bool,
    keyboard: str,
    keys_enabled: bool,
    on_dismiss_keys: Callable[[], Any],
    on_key: Callable[[str], Any],
    on_toggle_modifier: Callable[[str], Any],
    phone: bool,
    picture_progress: Any,
    show_key_row: bool,
    split: bool,
    terminal: TerminalViewState,
    tokens: dict[str, str],
) -> Any:
    """Append the inline keys and picture progress; return the floating key dock."""
    # The open dock sits under the key row, above the picture/status line and composer. This desktop
    # branch is always expanded, so the dock dedupes against the visible inline row; a hidden row
    # keeps the full grid so no key is unreachable.
    dock_overlay: Any = None
    if in_control and not phone and not split and keyboard == KEYBOARD_KEYS:
        picture_room = PICTURE_HEIGHT + 6 if collapse_keys and picture_progress is not None else 0
        dock_height = budget.dock_inline or max(0.0, budget.dock_overlay - picture_room)
        dock = ft.Container(
            key=AGENT_KEYS_SCROLL_KEY,
            disabled=terminal.selection.open or not composer_focusable,
            content=ToolsKeysDock(
                armed=armed,
                enabled=keys_enabled,
                on_key=on_key,
                on_toggle_modifier=on_toggle_modifier,
                on_dismiss=on_dismiss_keys,
                presentation="agent",
                dark=dark,
                max_height=dock_height,
                dedupe_inline=show_key_row and not collapse_keys,
                compact=collapse_keys,
            ),
        )
        if budget.dock_overlay:
            overlay_controls: list[Any] = [dock]
            if collapse_keys and picture_progress is not None:
                overlay_controls.insert(0, picture_progress)
            dock_overlay = ft.Container(
                key="agent-keys-overlay",
                left=0,
                right=0,
                bottom=budget.overlay_bottom,
                height=budget.dock_overlay,
                bgcolor=tokens["surface"],
                content=ft.Column(spacing=6, scroll=ft.ScrollMode.AUTO, controls=overlay_controls),
            )
        elif budget.dock_inline > 0:
            bottom.append(dock)
        # else: no room for the dock anywhere (zero inline and overlay
        # budget). It stays out of the main column; the "Keys"/"More keys"
        # toggle remains and the terminal keeps the remaining height.
    if picture_progress is not None and not collapse_keys:
        bottom.append(picture_progress)
    return dock_overlay
