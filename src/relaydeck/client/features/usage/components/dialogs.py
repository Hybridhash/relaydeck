"""Plain pickers, the attention list and the set-up command dialog.
Single-select lists with Cancel and Done. The command dialog shows the exact Mac command
with a copy button: it never changes the Mac from the phone.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

import flet as ft

from relaydeck.client.features.usage.components.primitives import ui

ALL = "all"


@ft.component
def CommandDialog(
    *,
    open: bool,
    title: str,
    lines: Sequence[str],
    note: str,
    doctor: str | None,
    tokens: dict[str, str],
    on_close: Callable[[], Any],
) -> ft.AlertDialog:
    """The exact offline command for the Mac, with a copy button. The copy button copies only the
    command lines, which work as typed. `doctor` (when there is no exact command) is shown
    separately and is never copied."""
    clipboard = ft.Clipboard()

    async def copy(_e: Any) -> None:
        await clipboard.set("\n".join(lines))

    body: list[ft.Control] = [
        ui(
            note,
            color=tokens["text_muted"],
            size=13,
            weight=500,
            max_lines=None,
            key="command-note",
        ),
    ]
    for line in lines:
        # Never ellipsized: a long command wraps so the whole text can be read and copied.
        body.append(
            ft.Container(
                padding=ft.Padding.all(10),
                bgcolor=tokens["surface_raised"],
                border_radius=10,
                content=ui(
                    line,
                    color=tokens["text"],
                    size=12,
                    weight=600,
                    max_lines=None,
                    key="command-line",
                ),
            )
        )
    if doctor is not None:
        body.append(
            ui(
                doctor,
                color=tokens["text_2"],
                size=13,
                weight=600,
                max_lines=None,
                key="command-doctor",
            )
        )
    actions: list[ft.Control] = []
    if lines:
        actions.append(
            ft.TextButton(
                key="usage-command-copy",
                content=ui("Copy command", color=tokens["link"], size=14, weight=800, max_lines=1),
                on_click=copy,
            )
        )
    actions.append(
        ft.TextButton(
            key="usage-command-close",
            content=ui("Close", color=tokens["text_2"], size=14, weight=700, max_lines=1),
            on_click=lambda _e: on_close(),
        )
    )
    return ft.AlertDialog(
        key="usage-command-dialog",
        open=open,
        title=ui(title, color=tokens["text"], size=17, weight=800, max_lines=2),
        content=ft.Container(width=340, content=ft.Column(spacing=10, tight=True, controls=body)),
        actions=actions,
        on_dismiss=lambda _e: on_close(),
    )
