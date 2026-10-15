"""The "Connecting" chip shown over a terminal while the app takes control on its own.

Three accent dots bob up and down in turn. It floats over the terminal viewport, so showing or
hiding it never changes the terminal's size. The bob is driven by each animation's end event, so
it only runs while the chip is visible (the chip is shown for well under a second per open).
"""

from __future__ import annotations

import flet as ft

DOT_SIZE = 8
BOB_MS = 320
CHIP_HEIGHT = 32
_LIFT = -0.6  # fraction of the dot's own height


@ft.component
def ConnectingDots(tokens: dict[str, str], visible: bool, cover: bool = False) -> ft.Control:
    """`cover` also hides the terminal beneath (it keeps laying out and reporting sizes), so a
    terminal that is still settling its size is never seen mid-resize."""
    visible = visible or cover
    up, set_up = ft.use_state(False)

    def flip(_: object) -> None:
        if visible:
            set_up(lambda current: not current)

    def start() -> None:
        # Nothing animates until a value changes: the first flip starts the chain, and each
        # finished bob flips the next.
        if visible:
            set_up(True)

    ft.use_effect(start, dependencies=[visible])

    def dot(index: int) -> ft.Control:
        lifted = up if index % 2 == 0 else not up
        return ft.Container(
            key=f"terminal-connecting-dot-{index}",
            width=DOT_SIZE,
            height=DOT_SIZE,
            border_radius=DOT_SIZE / 2,
            bgcolor=tokens["accent"],
            offset=ft.Offset(0, _LIFT if lifted and visible else 0),
            animate_offset=ft.Animation(BOB_MS, ft.AnimationCurve.EASE_IN_OUT),
            on_animation_end=flip if index == 0 else None,
        )

    return ft.Container(
        key="terminal-connecting",
        visible=visible,
        ignore_interactions=True,
        bgcolor=tokens["terminal_bg"] if cover else None,
        alignment=ft.Alignment.BOTTOM_CENTER,
        padding=ft.Padding.only(bottom=16),
        content=ft.Container(
            height=CHIP_HEIGHT,  # the viewport stack expands its children; the chip must not
            bgcolor=tokens["raised"],
            border_radius=CHIP_HEIGHT / 2,
            padding=ft.Padding.symmetric(horizontal=14),
            content=ft.Row(
                tight=True,
                spacing=6,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                controls=[
                    dot(0),
                    dot(1),
                    dot(2),
                    ft.Container(width=4),
                    ft.Text("Connecting…", size=12, color=tokens["muted"]),
                ],
            ),
        ),
    )
