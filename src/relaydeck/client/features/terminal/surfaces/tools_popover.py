"""Phone tools popover: terminal keys and input actions over the terminal."""

from __future__ import annotations

import inspect
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import flet as ft

from relaydeck.client.components.design_system import tokens as T
from relaydeck.client.components.design_system.fonts import FAMILY_INTERFACE
from relaydeck.client.components.design_system.primitives import text_style
from relaydeck.client.components.design_system.terminal_window import (
    SegmentedSwitch,
    key_chip,
)
from relaydeck.client.features.terminal.key_bar import KEY_BAR_GROUPS, KeyBarItem

logger = logging.getLogger("relaydeck.terminal")

LAYER_KEY = "tools-popover"
SCRIM_KEY = "tools-popover-scrim"
PANEL_KEY = "tools-popover-panel"
CLOSE_KEY = "tools-popover-close"
MODE_KEY = "tools-mode"
CHIP_SIZE = 44
GROUP_GAP = 8
_TITLE_STYLE = {"size": 12, "weight": 700, "height": 1.2}
_ACTION_STYLE = {"size": 13, "weight": 600, "height": 1.2}


@dataclass(frozen=True)
class ToolAction:
    key: str
    label: str
    icon: Any
    run: Callable[[], Any]
    enabled: bool = True
    native: Any = None  # iOS only opens a picker from the tap itself


def _action_button(action: ToolAction, done: Callable[[], Any]) -> Any:
    async def press(_event: Any) -> None:
        done()
        try:
            result = action.run()
            if inspect.isawaitable(result):
                await result
        except Exception:  # noqa: BLE001 - a failed action never breaks the panel
            logger.error("Terminal tools action failed")

    return ft.Semantics(
        label=action.label,
        button=True,
        exclude_semantics=True,
        content=ft.TextButton(
            key=action.key,
            height=CHIP_SIZE,
            disabled=not action.enabled,
            action=action.native,
            style=ft.ButtonStyle(
                padding=ft.Padding.symmetric(horizontal=10),
                bgcolor=T.RAISED,
                shape=ft.RoundedRectangleBorder(radius=8),
            ),
            on_click=press,
            content=ft.Row(
                tight=True,
                spacing=6,
                controls=[
                    ft.Icon(action.icon, size=16, color=T.PRIMARY_TEXT),
                    ft.Text(
                        action.label,
                        style=text_style(FAMILY_INTERFACE, _ACTION_STYLE, T.TEXT),
                        no_wrap=True,
                    ),
                ],
            ),
        ),
    )


def _keys(
    on_item: Callable[[KeyBarItem, bool], Any],
    modifier_states: dict[str, str],
    enabled: bool,
    show_keys: bool,
) -> list[Any]:
    async def perform(item: KeyBarItem, hold: bool) -> None:
        try:
            result = on_item(item, hold)
            if inspect.isawaitable(result):
                await result
        except Exception:  # noqa: BLE001 - a failed key never breaks the panel
            logger.error("Terminal tools key failed")

    chips: list[Any] = []
    for group in KEY_BAR_GROUPS:
        keys = [item for item in group if item.kind != "action"]
        if not show_keys:
            keys = [item for item in keys if item.kind == "modifier"]
        for item in keys:
            state = (
                modifier_states.get(next(iter(item.mods), ""), "off")
                if item.kind == "modifier"
                else "off"
            )

            async def press(_event: Any, selected: KeyBarItem = item) -> None:
                await perform(selected, False)

            async def hold(_event: Any, selected: KeyBarItem = item) -> None:
                await perform(selected, True)

            chips.append(
                key_chip(
                    item,
                    state=state,
                    enabled=enabled,
                    on_press=press,
                    on_hold=hold if item.long_press else None,
                    key=f"tools-{item.id}",
                    size=CHIP_SIZE,
                )
            )
        if keys:
            chips.append(ft.Container(width=GROUP_GAP))
    return chips[:-1] if chips else chips


@ft.component
def ToolsPopover(
    *,
    top: float,
    on_close: Callable[[], Any],
    on_item: Callable[[KeyBarItem, bool], Any],
    modifier_states: dict[str, str],
    keys_enabled: bool,
    show_keys: bool,
    mode: str,
    on_mode: Callable[[str], Any] | None,
    actions: tuple[ToolAction, ...],
    visible: bool = True,
) -> Any:
    controls: list[Any] = [
        ft.Row(
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
            controls=[
                ft.Text(
                    "Keys and actions",
                    style=text_style(FAMILY_INTERFACE, _TITLE_STYLE, T.TEXT_MUTED),
                    expand=True,
                ),
                ft.IconButton(
                    ft.Icons.CLOSE,
                    key=CLOSE_KEY,
                    icon_size=18,
                    width=CHIP_SIZE,
                    height=CHIP_SIZE,
                    tooltip="Close",
                    on_click=lambda _e: on_close(),
                ),
            ],
        ),
        ft.Row(
            wrap=True,
            spacing=4,
            run_spacing=4,
            controls=_keys(on_item, modifier_states, keys_enabled, show_keys),
        ),
    ]
    bottom: list[Any] = []
    if on_mode is not None:

        def choose(value: str) -> None:
            on_close()
            on_mode(value)

        bottom.append(
            SegmentedSwitch(
                (
                    ("compose", "Compose", f"{MODE_KEY}-compose"),
                    ("direct", "Direct input", f"{MODE_KEY}-direct"),
                ),
                mode,
                choose,
                key=MODE_KEY,
            )
        )
    bottom.extend(_action_button(action, on_close) for action in actions)
    if bottom:
        controls.append(ft.Row(wrap=True, spacing=4, run_spacing=4, controls=bottom))
    panel = ft.Container(
        key=PANEL_KEY,
        top=top,
        left=4,
        right=4,
        padding=ft.Padding.only(left=12, right=4, top=4, bottom=12),
        border_radius=12,
        bgcolor=ft.Colors.with_opacity(0.92, T.PANEL),
        border=ft.Border(top=ft.BorderSide(1, T.BORDER)),
        content=ft.Semantics(
            container=True,
            label="Keys and actions",
            content=ft.Column(spacing=8, tight=True, controls=controls),
        ),
    )
    return ft.Container(
        key=LAYER_KEY,
        visible=visible,
        left=0,
        top=0,
        right=0,
        bottom=0,
        content=ft.Stack(
            controls=[
                ft.GestureDetector(
                    key=SCRIM_KEY,
                    left=0,
                    top=0,
                    right=0,
                    bottom=0,
                    on_tap=lambda _e: on_close(),
                    content=ft.Container(bgcolor=ft.Colors.TRANSPARENT),
                ),
                panel,
            ]
        ),
    )
