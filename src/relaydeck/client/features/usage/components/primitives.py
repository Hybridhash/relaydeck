"""Usage screen primitives. Plain builders: no I/O.

Each builder receives prepared values from `screen_model.py` and the usage token set, and
returns Flet controls, except `MatchedRow`, the one component here (it keeps a row's equal-height
state).
Components never compute numbers, rankings or states. Fonts are the app's bundled faces:
Bricolage Grotesque for UI text, JetBrains Mono for numbers (no new font files).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

import flet as ft

from relaydeck.client.components.design_system.fonts import FAMILY_INTERFACE, FAMILY_MONO, face

HIT = 44  # the minimum touch target, in logical pixels (spec §4.3)
TILE = 36
RADIUS_CARD = 20
RADIUS_CONTROL = 14
RADIUS_SMALL = 10


def ui(
    value: str,
    *,
    color: str,
    size: float = 15,
    weight: int = 700,
    key: str | None = None,
    max_lines: int | None = 1,
    italic: bool = False,
    align: Any = None,
    label: str | None = None,
) -> ft.Text:
    """UI text. Long values ellipsize unless `max_lines` allows wrapping (XXL text reflows)."""
    return ft.Text(
        value,
        key=key,
        color=color,
        size=size,
        weight=ft.FontWeight.W_700 if weight >= 700 else ft.FontWeight.W_500,
        font_family=face(FAMILY_INTERFACE, weight),
        max_lines=max_lines,
        overflow=ft.TextOverflow.ELLIPSIS if max_lines else ft.TextOverflow.CLIP,
        italic=italic,
        text_align=align,
        semantics_label=label,
    )


def num(
    value: str,
    *,
    color: str,
    size: float = 15,
    weight: int = 700,
    key: str | None = None,
    align: Any = None,
    max_lines: int | None = 1,
) -> ft.Text:
    """A number: JetBrains Mono, so columns of figures align. Single-line by default; a caller that
    has the
    room lets it wrap (`max_lines=None`) so it never clips at XXL text."""
    return ft.Text(
        value,
        key=key,
        color=color,
        size=size,
        weight=ft.FontWeight.W_700 if weight >= 700 else ft.FontWeight.W_600,
        font_family=face(FAMILY_MONO, weight),
        max_lines=max_lines,
        overflow=ft.TextOverflow.ELLIPSIS if max_lines else ft.TextOverflow.CLIP,
        text_align=align,
    )


def overline(value: str, *, color: str, key: str | None = None) -> ft.Text:
    return ui(value.upper(), color=color, size=12, weight=700, key=key, max_lines=None)


def card(
    children: Sequence[ft.Control],
    *,
    tokens: dict[str, str],
    key: str | None = None,
    padding: float = 18,
    expand: bool | int = False,
    fill: str | None = None,
) -> ft.Container:
    return ft.Container(
        key=key,
        bgcolor=fill or tokens["surface"],
        border=ft.Border.all(1, tokens["border"]),
        border_radius=RADIUS_CARD,
        padding=ft.Padding.all(padding),
        expand=expand,
        content=ft.Column(controls=list(children), spacing=10, tight=True),
    )


@ft.component
def MatchedRow(
    *,
    cards: Sequence[ft.Container | None],
    expands: Sequence[int],
    spacing: float = 16,
) -> ft.Row:
    """A row of cards that share one height. Flutter cannot stretch a row inside the scrolling page,
    so each card's content reports its natural height, and every shorter card gets a transparent
    spacer under its content up to the tallest one. Each render builds fresh containers from the
    given cards, which are never mutated. A `None` slot is an empty spacer of its share."""
    heights: dict[int, float]
    set_heights: Any
    heights, set_heights = ft.use_state({})
    present = [index for index, card in enumerate(cards) if card is not None]
    target: float | None = None
    if present and all(index in heights for index in present):
        target = max(heights[index] for index in present)
    controls: list[ft.Control] = []
    for index, card in enumerate(cards):
        if card is None:
            controls.append(ft.Container(expand=expands[index]))
            continue
        column = card.content
        assert isinstance(column, ft.Column)

        def measure(event: Any, slot: int = index) -> None:
            set_heights(
                lambda current: current
                if current.get(slot) == event.height
                else {**current, slot: event.height}
            )

        natural = ft.Container(
            content=ft.Column(controls=list(column.controls), spacing=10, tight=True),
            on_size_change=measure,
        )
        gap = 0 if target is None else max(0, target - heights.get(index, 0))
        controls.append(
            ft.Container(
                key=card.key,
                bgcolor=card.bgcolor,
                border=card.border,
                border_radius=card.border_radius,
                padding=card.padding,
                expand=expands[index],
                content=ft.Column(
                    controls=[natural, ft.Container(height=gap)], spacing=0, tight=True
                ),
            )
        )
    return ft.Row(
        controls=controls, spacing=spacing, vertical_alignment=ft.CrossAxisAlignment.START
    )


def monogram(
    name: str, *, tokens: dict[str, str], dashed: bool = False, size: float = TILE
) -> ft.Container:
    """A neutral two-letter tile from the display name. The same style for every provider."""
    return ft.Container(
        width=size,
        height=size,
        alignment=ft.Alignment.CENTER,
        bgcolor=None if dashed else tokens["surface_accent"],
        border=ft.Border.all(1, tokens["border_strong"]) if dashed else None,
        border_radius=RADIUS_SMALL,
        content=ui(
            name,
            color=tokens["text_muted"] if dashed else tokens["link"],
            size=13,
            weight=700,
            max_lines=1,
        ),
    )


def badge(
    label: str, *, tokens: dict[str, str], tone: str = "warn", key: str | None = None
) -> ft.Container:
    """A pill. `warn` for data-quality words, `neutral` for setup, `count` shows a number."""
    if tone == "warn":
        fill, ink = tokens["warn_bg"], tokens["warn"]
    elif tone == "danger":
        fill, ink = tokens["danger_bg"], tokens["danger"]
    else:
        fill, ink = tokens["border"], tokens["text_2"]
    return ft.Container(
        key=key,
        height=24,
        padding=ft.Padding.symmetric(horizontal=10, vertical=0),
        bgcolor=fill,
        border_radius=999,
        alignment=ft.Alignment.CENTER,
        content=ui(label, color=ink, size=12, weight=700, max_lines=1),
    )


def picker(
    label: str,
    value: str,
    *,
    tokens: dict[str, str],
    key: str,
    active: bool,
    on_click: Callable[[Any], Any],
    expand: bool | int = True,
    reset: Callable[[Any], Any] | None = None,
) -> ft.Container:
    """A picker button: the label over the value, a chevron, and a reset when a value is chosen."""
    row: list[ft.Control] = [
        ft.Column(
            spacing=0,
            tight=True,
            expand=True,
            controls=[
                overline(label, color=tokens["text_muted"]),
                ui(value, color=tokens["text"], size=15, weight=700, max_lines=2),
            ],
        ),
    ]
    if active and reset is not None:
        row.append(
            ft.IconButton(
                key=f"{key}-reset",
                icon=ft.Icons.CLOSE,
                icon_color=tokens["text_2"],
                tooltip=f"Reset {label.lower()} to all",
                on_click=reset,
                width=HIT,
                height=HIT,
            )
        )
    row.append(ft.Icon(ft.Icons.KEYBOARD_ARROW_DOWN, color=tokens["text_muted"], size=20))
    return ft.Container(
        key=key,
        expand=expand,
        padding=ft.Padding.symmetric(horizontal=14, vertical=8),
        bgcolor=tokens["surface"],
        border=ft.Border.all(1, tokens["accent"] if active else tokens["border"]),
        border_radius=RADIUS_CONTROL,
        on_click=on_click,
        tooltip=f"{label.title()}: {value}",
        content=ft.Row(controls=row, spacing=6, alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
    )


def segmented(
    options: Sequence[tuple[str, str, bool]],
    *,
    tokens: dict[str, str],
    on_select: Callable[[str], Callable[[Any], Any]],
    key_prefix: str = "period",
    fixed_width: float | None = None,
) -> ft.Container:
    """The period control: a track of equal buttons. Each segment is a real button of at least 44
    px,
    so the whole segment is the tap target, and the selected one says so to assistive technology."""
    segments: list[ft.Control] = []
    for value, label, selected in options:
        segments.append(
            ft.Button(
                key=f"{key_prefix}-{value}",
                expand=True,
                height=HIT,
                # Tight side padding so "Today" fits a phone segment without ellipsis.
                style=ft.ButtonStyle(padding=ft.Padding.symmetric(horizontal=4, vertical=0)),
                bgcolor=tokens["accent"] if selected else tokens["surface"],
                color=tokens["accent_on"] if selected else tokens["text_muted"],
                on_click=on_select(value),
                tooltip=label,
                content=ui(
                    label,
                    color=tokens["accent_on"] if selected else tokens["text_muted"],
                    size=14,
                    weight=700 if selected else 600,
                    max_lines=1,
                    label=f"{label}, selected" if selected else label,
                ),
            )
        )
    return ft.Container(
        width=fixed_width,
        padding=ft.Padding.all(4),
        bgcolor=tokens["surface"],
        border=ft.Border.all(1, tokens["border"]),
        border_radius=RADIUS_CONTROL,
        content=ft.Row(controls=segments, spacing=4),
    )


def action_button(
    label: str,
    *,
    tokens: dict[str, str],
    key: str,
    on_click: Callable[[Any], Any],
    primary: bool = False,
) -> ft.Button:
    """A row action (Set up, Refresh). Filled for primary, outlined otherwise; at least 44 px tall.
    A row action (Set up, Refresh). Filled for primary, outlined otherwise; at least 44 px tall."""
    return ft.Button(
        key=key,
        content=ui(
            label,
            color=tokens["accent_on"] if primary else tokens["text"],
            size=14,
            weight=700,
            max_lines=1,
        ),
        height=HIT,
        bgcolor=tokens["accent"] if primary else tokens["surface_raised"],
        color=tokens["accent_on"] if primary else tokens["text"],
        on_click=on_click,
        tooltip=label,
    )


def refresh_button(
    *, tokens: dict[str, str], on_click: Callable[[Any], Any], disabled: bool
) -> ft.IconButton:
    return ft.IconButton(
        key="usage-refresh",
        icon=ft.Icons.REFRESH,
        icon_color=tokens["text"],
        bgcolor=tokens["surface"],
        width=HIT,
        height=HIT,
        disabled=disabled,
        tooltip="Refresh readings",
        on_click=on_click,
    )


def divider(tokens: dict[str, str]) -> ft.Container:
    return ft.Container(height=1, bgcolor=tokens["border"])
