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
from relaydeck.client.features.terminal.layout_budget import (
    DOCK_CONTENT_HEIGHT,
    PICTURE_HEIGHT,
    TYPING_SPLIT_DOCK_FRACTION,
    DetailBudget,
)
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
    detail_height: float,
    dock_epoch: int,
    in_control: bool,
    keyboard: str,
    keys_enabled: bool,
    on_dismiss_keys: Callable[[], Any],
    on_key: Callable[[str], Any],
    on_toggle_modifier: Callable[[str], Any],
    pad: float,
    phone: bool,
    picture_progress: Any,
    show_key_row: bool,
    split: bool,
    split_ctl_height: float,
    split_dock_top: float,
    split_side_width: float,
    split_top: float,
    terminal: TerminalViewState,
    tokens: dict[str, str],
) -> Any:
    """The tools-key dock (inline, overlay or typing-column slot) and picture progress.

    Appends to `bottom`; returns `(dock_overlay, split_dock)`."""
    # The open dock sits directly under the key row,
    # above the picture/status line and the composer (never below the
    # composer). This desktop-only branch is always the expanded tier,
    # so the dock dedupes against the inline row whenever
    # that row is visible; a hidden/collapsed row keeps the full legacy
    # grid so no key becomes unreachable. Compact phones never reach
    # this branch and keep the legacy grid via the default.
    dock_overlay: Any = None
    split_dock: Any = None
    if in_control and not phone and keyboard == KEYBOARD_KEYS:
        picture_room = PICTURE_HEIGHT + 6 if collapse_keys and picture_progress is not None else 0
        dock_height = budget.dock_inline or max(0.0, budget.dock_overlay - picture_room)
        if split:
            # The dock starts under the composer and never runs past the
            # bottom padding; its own scroll is only needed when the keys exceed that.
            dock_height = max(
                0.0,
                min(
                    DOCK_CONTENT_HEIGHT,
                    TYPING_SPLIT_DOCK_FRACTION * detail_height,
                    detail_height - split_dock_top - pad,
                ),
            )
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
        if split:
            # Top-anchored directly under the composer frame. A new
            # scroll key per open remounts the column, so the keys open at the top.
            split_dock = ft.Container(
                key="agent-split-dock",
                right=pad,
                top=split_dock_top,
                width=split_side_width,
                height=dock_height,
                content=ft.Column(
                    key=f"agent-split-dock-scroll-{dock_epoch}",
                    spacing=0,
                    scroll=ft.ScrollMode.AUTO,
                    controls=[dock],
                ),
            )
        elif budget.dock_overlay:
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
    return (dock_overlay, split_dock)
