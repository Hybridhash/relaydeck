"""Gestures around an agent card: swipe, long-press, right-click and F2 all reach Rename."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

import flet as ft

from relaydeck.client.components.design_system import tokens as T
from relaydeck.client.components.design_system.tokens import usage_palette
from relaydeck.client.features.inventory.rename_presentation import use_close_gate
from relaydeck.client.features.terminal.diagnostics import diagnostics_logger

LOG = logging.getLogger("relaydeck.inventory")
SWIPE_THRESHOLD = 0.3
MENU_WIDTH = 250
MENU_ROW = 50


def _reveal(tokens: dict[str, str]) -> Any:
    """What a left swipe uncovers behind the card."""
    return ft.Container(
        bgcolor=tokens["accent"],
        border_radius=T.RADIUS_CARD,
        alignment=ft.Alignment.CENTER_RIGHT,
        padding=ft.Padding.only(right=24),
        content=ft.Row(
            tight=True,
            spacing=8,
            alignment=ft.MainAxisAlignment.END,
            controls=[
                ft.Icon(ft.Icons.EDIT, size=20, color=tokens["accent_on"]),
                ft.Text("Rename", size=15, weight=ft.FontWeight.W_800, color=tokens["accent_on"]),
            ],
        ),
    )


def AgentGestures(
    key: str,
    tile: Any,
    label: str,
    dark: bool,
    on_menu: Callable[[str], Any],
    on_rename: Callable[[], Any],
    on_select: Callable[[], Any],
) -> Any:
    """Wrap a card: a left swipe past the threshold, F2, or the long-press menu opens Rename.

    The swipe never dismisses the card: the pending dismissal is always declined, so the card
    springs back while the sheet opens.
    """
    tokens = usage_palette(dark)

    async def confirm(event: Any) -> None:
        # Settle the pending dismissal while this control is still mounted, before the render
        # that opens the sheet replaces it. The call's own timeout cleans up a missing answer.
        try:
            await event.control._invoke_method("confirm_dismiss", {"dismiss": False}, timeout=2)
        except (TimeoutError, RuntimeError):
            LOG.debug("swipe reset not acknowledged", exc_info=True)
        on_rename()

    def on_key(event: Any) -> None:
        if getattr(event, "key", "") == "F2":
            on_rename()

    gesture = ft.GestureDetector(
        key=f"agent-gesture-{key}",
        on_long_press_start=lambda _e: on_menu("long_press"),
        on_secondary_tap=lambda _e: on_menu("secondary"),
        content=tile,
    )
    keyed = ft.KeyboardListener(key=f"agent-keys-{key}", content=gesture, on_key_down=on_key)
    return ft.Dismissible(
        key=f"agent-swipe-{key}",
        content=ft.Semantics(
            label=label,
            button=True,
            on_tap=lambda _e: on_select(),
            on_long_press=lambda _e: on_menu("semantics"),
            on_long_press_hint_text="Rename and more",
            exclude_semantics=True,
            content=keyed,
        ),
        background=_reveal(tokens),
        dismiss_direction=ft.DismissDirection.END_TO_START,
        dismiss_thresholds={ft.DismissDirection.END_TO_START: SWIPE_THRESHOLD},
        on_confirm_dismiss=confirm,
    )


def _menu_row(
    key: str, label: str, icon: Any, tokens: dict[str, str], on_click: Callable[[], Any], lead: bool
) -> Any:
    return ft.Container(
        key=key,
        height=MENU_ROW,
        padding=ft.Padding.symmetric(horizontal=16),
        bgcolor=tokens["border"] if lead else None,
        on_click=lambda _e: on_click(),
        ink=True,
        content=ft.Row(
            alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
            controls=[
                ft.Text(label, size=16, weight=ft.FontWeight.W_600, color=tokens["text"]),
                ft.Icon(icon, size=20, color=tokens["text_2"]),
            ],
        ),
    )


@ft.component
def AgentMenu(
    agent_key: str,
    on_rename: Callable[[], Any],
    on_open: Callable[[], Any],
    on_close: Callable[[], Any],
    dark: bool,
    gone: bool = False,
    pinned: bool = False,
    on_pin: Callable[[], Any] | None = None,
) -> Any:
    """Rename first and highlighted, then Open terminal.

    A choice, a dismissal or the agent leaving closes the dialog natively before it unmounts.
    """
    tokens = usage_palette(dark)
    after: Any = ft.use_ref(None)

    def closed() -> None:
        on_close()
        action, after.current = after.current, None
        if action is not None:
            action()

    gate = use_close_gate(True, closed)
    ft.use_effect(lambda: diagnostics_logger().info("agent_menu shown"), dependencies=[])
    ft.use_effect(lambda: gate.request() if gone else None, dependencies=[gone])

    def choose(action: Callable[[], Any], *, after_close: bool = False) -> Callable[[], None]:
        def pick() -> None:
            if after_close:
                # Navigating away unmounts the menu, so it closes natively first.
                after.current = action
                gate.request()
            else:
                gate.request()
                action()

        return pick

    return ft.AlertDialog(
        key=f"agent-menu-{agent_key}",
        open=not gate.closing,
        modal=False,
        bgcolor=tokens["surface_raised"],
        shape=ft.RoundedRectangleBorder(radius=16),
        content_padding=0,
        content=ft.Container(
            width=MENU_WIDTH,
            border_radius=16,
            clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
            content=ft.Column(
                spacing=0,
                tight=True,
                controls=[
                    _menu_row(
                        f"agent-rename-{agent_key}",
                        "Rename",
                        ft.Icons.EDIT,
                        tokens,
                        choose(on_rename),
                        True,
                    ),
                    *(
                        [
                            _menu_row(
                                f"agent-pin-{agent_key}",
                                "Unpin" if pinned else "Pin to top",
                                ft.Icons.PUSH_PIN_OUTLINED if pinned else ft.Icons.PUSH_PIN,
                                tokens,
                                choose(on_pin),
                                False,
                            )
                        ]
                        if on_pin is not None
                        else []
                    ),
                    _menu_row(
                        f"agent-open-{agent_key}",
                        "Open terminal",
                        ft.Icons.TERMINAL,
                        tokens,
                        choose(on_open, after_close=True),
                        False,
                    ),
                ],
            ),
        ),
        on_dismiss=lambda _e: gate.reported() if gate.closing else on_close(),
    )
