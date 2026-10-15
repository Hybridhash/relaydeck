"""Plain pickers, the attention list and the set-up command dialog.
Single-select lists with Cancel and Done. The command dialog shows the exact Mac command
with a copy button: it never changes the Mac from the phone.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

import flet as ft

from relaydeck.client.components.design_system import tokens as T
from relaydeck.client.features.usage.components.primitives import ui

ALL = "all"


@ft.component
def PickerDialog(
    *,
    open: bool,
    key_prefix: str,
    title: str,
    all_value: str,
    options: Sequence[tuple[str, str, str]],
    selected: str,
    tokens: dict[str, str],
    on_done: Callable[[str], Any],
    on_cancel: Callable[[], Any],
) -> ft.AlertDialog:
    """A single-select list: "All" first, then each option. The pending choice is applied on Done.
    Values the server did not supply arrive as "—" (never zero) from the screen model."""
    pending, set_pending = ft.use_state(selected)

    def reset() -> None:
        set_pending(selected)

    ft.use_effect(reset, dependencies=[open, selected])
    rows: list[ft.Control] = []
    for option_id, label, value in [(ALL, "All", all_value), *options]:
        chosen = option_id == pending
        rows.append(
            ft.Container(
                key=f"{key_prefix}-option-{option_id}",
                # No fixed height: text reflows at XXL. The 12 px vertical padding keeps one line >=
                # 44 px.
                padding=ft.Padding.symmetric(horizontal=12, vertical=12),
                border_radius=T.RADIUS_KEY,
                bgcolor=tokens["surface_accent"] if chosen else None,
                border=ft.Border.all(1, tokens["accent"]) if chosen else None,
                on_click=(lambda chosen_id: lambda _e: set_pending(chosen_id))(option_id),
                content=ft.Row(
                    controls=[
                        ft.Container(
                            content=ui(
                                label, color=tokens["text"], size=15, weight=700, max_lines=2
                            ),
                            expand=True,
                        ),
                        # A genuine zero reads faint (the list sorts it last); "—" stays text_2.
                        ui(
                            value,
                            color=tokens["text_faint"] if value == "0" else tokens["text_2"],
                            size=14,
                            weight=600,
                            max_lines=2,
                        ),
                        ft.Icon(ft.Icons.CHECK, color=tokens["accent"], size=18, visible=chosen),
                    ],
                    spacing=8,
                ),
            )
        )
    # The sheet's nav row: Cancel left, the title centered, Done right (the mock's sheet style).
    nav = ft.Row(
        controls=[
            ft.TextButton(
                key=f"{key_prefix}-cancel",
                content=ui("Cancel", color=tokens["text_2"], size=15, weight=700, max_lines=1),
                on_click=lambda _e: on_cancel(),
            ),
            ft.Container(
                expand=True,
                alignment=ft.Alignment.CENTER,
                content=ui(
                    title,
                    color=tokens["text"],
                    size=17,
                    weight=800,
                    max_lines=1,
                    align=ft.TextAlign.CENTER,
                ),
            ),
            ft.TextButton(
                key=f"{key_prefix}-done",
                content=ui("Done", color=tokens["link"], size=15, weight=800, max_lines=1),
                on_click=lambda _e: on_done(pending),
            ),
        ],
        spacing=4,
        vertical_alignment=ft.CrossAxisAlignment.CENTER,
    )
    return ft.AlertDialog(
        key=f"{key_prefix}-dialog",
        open=open,
        bgcolor=tokens["surface_sheet"],
        shape=ft.RoundedRectangleBorder(radius=24),
        title=nav,
        title_padding=ft.Padding.only(left=8, right=8, top=12, bottom=4),
        content=ft.Container(
            width=340,
            content=ft.Column(scroll=ft.ScrollMode.AUTO, spacing=4, tight=True, controls=rows),
        ),
        actions=[],
        on_dismiss=lambda _e: on_cancel(),
    )


@ft.component
def ListDialog(
    *,
    open: bool,
    key_prefix: str,
    title: str,
    body: Sequence[ft.Control],
    tokens: dict[str, str],
    on_close: Callable[[], Any],
) -> ft.AlertDialog:
    """A temporary, scrollable list (the "+k more" attention items). Closing it changes no
    filter."""
    return ft.AlertDialog(
        key=f"{key_prefix}-dialog",
        open=open,
        title=ui(title, color=tokens["text"], size=17, weight=800, max_lines=2),
        content=ft.Container(
            width=340,
            content=ft.Column(
                scroll=ft.ScrollMode.AUTO, spacing=4, tight=True, controls=list(body)
            ),
        ),
        actions=[
            ft.TextButton(
                key=f"{key_prefix}-close",
                content=ui("Close", color=tokens["text_2"], size=14, weight=700, max_lines=1),
                on_click=lambda _e: on_close(),
            ),
        ],
        on_dismiss=lambda _e: on_close(),
    )


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
