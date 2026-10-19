"""Shared design-system primitives.

Controlled presentation only: immutable display props plus callbacks.
No HTTP/auth/PTY/durable state. Every interactive element keeps a
48px minimum hit region (canonical floor; the mock draws 44px).
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any

import flet as ft

from relaydeck.client.components.design_system import tokens as T
from relaydeck.client.components.design_system.fonts import (
    FAMILY_INTERFACE,
    FAMILY_MONO,
    FAMILY_SYMBOLS,
    dot_tile_src,
    face,
)

_WEIGHTS = {
    400: ft.FontWeight.NORMAL,
    500: ft.FontWeight.W_500,
    600: ft.FontWeight.W_600,
    700: ft.FontWeight.BOLD,
    800: ft.FontWeight.W_800,
}

# 20% white keyboard-focus wash (tokens.py is out of scope here).
_FOCUS_WASH = T.FOCUS_WASH


def _action_button(
    *,
    key: str | None,
    content: Any,
    on_press: Callable[[], Any],
    radius: float,
    width: float | None = None,
    height: float | None = None,
    expand: bool = False,
    tooltip: str | None = None,
    focus_ring: str = T.TEXT,
) -> ft.TextButton:
    """Keyboard-actionable carrier: Tab focus, Enter/Space, focus ring.

    The visual Container stays intact as `content` (same padding, fill,
    border, shadow); the button owns geometry and neutralizes the
    inherited chrome theme (transparent fill/border/hover in every
    state) so no second fill or border wraps the mock content. Focus
    shows a 3px ring plus wash; the ring width is identical in all
    states so geometry never shifts. One on_click per action. The
    handler is async so Flet awaits it: async session actions (take
    over, disconnect, paste) run instead of being dropped unawaited.
    """

    async def _on_click(event: Any) -> Any:
        del event
        result = on_press()
        if inspect.isawaitable(result):
            return await result
        return result

    return ft.TextButton(
        key=key,
        width=width,
        height=height,
        expand=expand,
        tooltip=tooltip,
        style=ft.ButtonStyle(
            padding=0,
            shape=ft.RoundedRectangleBorder(radius=radius),
            bgcolor=ft.Colors.TRANSPARENT,
            overlay_color={
                ft.ControlState.DEFAULT: ft.Colors.TRANSPARENT,
                ft.ControlState.FOCUSED: _FOCUS_WASH,
            },
            side={
                ft.ControlState.DEFAULT: ft.BorderSide(3, ft.Colors.TRANSPARENT),
                ft.ControlState.FOCUSED: ft.BorderSide(3, focus_ring),
            },
        ),
        on_click=_on_click,
        content=content,
    )


def text_style(
    family: str,
    spec: dict[str, float],
    color: str,
    letter_spacing_em: float = 0.0,
) -> ft.TextStyle:
    """Mock text style: bundled family alias + size/weight/height/spacing.

    Every style carries the symbols fallback (from fonts directly: this
    module must not import theme, which imports this package).
    """
    size = spec["size"]
    return ft.TextStyle(
        font_family=face(family, int(spec["weight"])),
        font_family_fallback=[FAMILY_SYMBOLS],
        size=size,
        weight=_WEIGHTS[int(spec["weight"])],
        height=spec["height"],
        letter_spacing=letter_spacing_em * size if letter_spacing_em else None,
        color=color,
    )


def _pill_colors(status: str, inverted: bool) -> tuple[str, str, str | None]:
    """(fill, text, border) for a status pill.

    `inverted` renders on the solid orange blocked card (dark pill);
    otherwise the pill carries the status fill (or outline for Unknown).
    """
    if status == T.STATUS_BLOCKED and inverted:
        return T.BLOCKED_PILL_BG, T.BLOCKED_PILL_TEXT, None
    if status == T.STATUS_BLOCKED:
        return T.BLOCKED, T.BLOCKED_TEXT, None
    if status == T.STATUS_WORKING:
        return T.WORKING, T.WORKING_TEXT, None
    if status == T.STATUS_DONE:
        return T.DONE, T.DONE_TEXT, None
    if status == T.STATUS_IDLE:
        return T.IDLE_FILL, T.IDLE_TEXT, None
    return T.PANEL, T.UNKNOWN_TEXT, T.UNKNOWN_TEXT


def StatusPill(
    status: str,
    label: str | None = None,
    inverted: bool = False,
    key: str | None = None,
) -> Any:
    """Status pill: 11.5px bold uppercase label on the status fill.

    22px tall at 100% scale via the 22px text line; no fixed height,
    so the pill grows with text scale instead of clipping (A1).
    """
    fill, ink, border = _pill_colors(status, inverted)
    word = (label or T.STATUS_LABELS.get(status, T.STATUS_LABELS[T.STATUS_UNKNOWN])).upper()
    return ft.Container(
        key=key,
        padding=ft.Padding.symmetric(horizontal=10),
        border_radius=ft.BorderRadius.all(11),
        bgcolor=fill,
        border=ft.Border.all(1, border) if border else None,
        alignment=ft.Alignment.CENTER,
        content=ft.Text(
            word,
            style=text_style(FAMILY_INTERFACE, T.TYPE_PILL, ink, 0.05),
            no_wrap=True,
        ),
    )


_AGENT_BADGE_TYPE = {"size": 11, "weight": 600, "height": 1.0}


def AgentPill(
    label: str,
    color: str,
    key: str | None = None,
    ink: str | None = None,
) -> Any:
    """Agent identity pill: 18px badge, 11px semibold label in identity ink
    on a 16% identity wash. `ink` overrides the text color where the
    identity color cannot read (solid orange blocked cards)."""
    return ft.Container(
        key=key,
        height=18,
        padding=ft.Padding.symmetric(horizontal=6),
        border_radius=ft.BorderRadius.all(9),
        bgcolor=ft.Colors.with_opacity(0.16, color),
        alignment=ft.Alignment.CENTER,
        content=ft.Text(
            label,
            style=text_style(FAMILY_INTERFACE, _AGENT_BADGE_TYPE, ink or color),
            no_wrap=True,
        ),
    )


COUNT_TILE_HEIGHT = 52
_COUNT_STRIP_NUMBER = {**T.TYPE_COUNT_NUMBER, "size": 24, "height": 1.0}


def CountTile(status: str, count: int, key: str | None = None) -> Any:
    """Compact count tile: the number and an uppercase label side by side, 52px tall."""
    label = T.STATUS_LABELS.get(status, T.STATUS_LABELS[T.STATUS_UNKNOWN]).upper()
    if status == T.STATUS_BLOCKED:
        bg: str | None = T.BLOCKED
        border = None
        number_ink, label_ink = T.BLOCKED_TEXT, T.BLOCKED_TEXT
    else:
        bg = T.CARD
        border = ft.Border.all(1, T.BORDER)
        number_ink = {
            T.STATUS_WORKING: T.WORKING,
            T.STATUS_DONE: T.DONE,
        }.get(status, T.TEXT_MUTED)
        label_ink = T.TEXT_SECONDARY
    return ft.Container(
        key=key or f"count-{status}",
        height=COUNT_TILE_HEIGHT,
        padding=ft.Padding.symmetric(horizontal=14),
        border_radius=ft.BorderRadius.all(T.RADIUS_TILE),
        bgcolor=bg,
        border=border,
        alignment=ft.Alignment.CENTER_LEFT,
        content=ft.Row(
            spacing=8,
            tight=True,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
            controls=[
                ft.Text(
                    str(count), style=text_style(FAMILY_INTERFACE, _COUNT_STRIP_NUMBER, number_ink)
                ),
                ft.Text(
                    label, style=text_style(FAMILY_INTERFACE, T.TYPE_COUNT_LABEL, label_ink, 0.05)
                ),
            ],
        ),
    )


def HostChip(
    host_id: str,
    label: str,
    selected: bool,
    on_select: Callable[[str], Any],
    key: str | None = None,
    compact: bool = False,
) -> Any:
    """Host filter chip; selected chip carries the primary fill.

    Default: 48px. `compact` (Console): the visible pill is 34px tall, but the tap
    target stays 44px, because the button keeps MIN_TOUCH and only the drawn pill shrinks."""
    visual = 34 if compact else None
    return _action_button(
        key=key or f"host-chip-{host_id}",
        height=T.MIN_TOUCH,
        radius=22,
        tooltip=label,
        on_press=lambda: on_select(host_id),
        content=ft.Container(
            height=visual,
            padding=ft.Padding.symmetric(horizontal=14 if compact else 16),
            border_radius=ft.BorderRadius.all(17 if compact else 22),
            bgcolor=T.PRIMARY_FILL if selected else T.CARD,
            border=None if selected else ft.Border.all(1, T.BORDER),
            alignment=ft.Alignment.CENTER,
            content=ft.Text(
                label,
                style=text_style(
                    FAMILY_INTERFACE,
                    (T.TYPE_CARD_LINE_STRONG if compact else T.TYPE_BUTTON)
                    if selected
                    else (T.TYPE_CARD_LINE if compact else T.TYPE_BUTTON_IDLE),
                    T.ON_PRIMARY if selected else T.TEXT_SECONDARY,
                ),
                no_wrap=True,
            ),
        ),
    )


def RoundIconButton(
    icon: ft.IconData,
    label: str,
    on_click: Callable[[], Any],
    tone: str = "ghost",
    key: str | None = None,
) -> Any:
    """48px round header button: ghost (card/border) or lime (primary new)."""
    lime = tone == "lime"
    return _action_button(
        key=key,
        width=T.MIN_TOUCH,
        height=T.MIN_TOUCH,
        radius=24,
        tooltip=label,
        focus_ring=T.DONE_TEXT if lime else T.TEXT,
        on_press=on_click,
        content=ft.Container(
            border_radius=ft.BorderRadius.all(24),
            bgcolor=T.DONE if lime else T.CARD,
            border=None if lime else ft.Border.all(1, T.BORDER),
            alignment=ft.Alignment.CENTER,
            content=ft.Icon(icon, size=20, color=T.DONE_TEXT if lime else T.TEXT),
        ),
    )


def DottedGround(content: Any, asset_base: str = "design_system") -> Any:
    """Page ground: #130F26 with the repeated 18px dot tile."""
    return ft.Container(
        key="console-ground",
        expand=True,
        bgcolor=T.GROUND,
        image=ft.DecorationImage(src=dot_tile_src(asset_base), repeat=ft.ImageRepeat.REPEAT),
        content=content,
    )


def ConsoleHeader(
    eyebrow: str,
    title: str,
    on_search: Callable[[], Any],
    on_new: Callable[[], Any],
    on_toggle_collapse: Callable[[], Any] | None = None,
    collapsed: bool = False,
    on_refresh: Callable[[], Any] | None = None,
    count: str | None = None,
) -> Any:
    """Console title row: mono eyebrow + 34px title + round actions.

    `count` ("5 of 5") is small faint text beside the title, and `on_refresh`
    is an icon-only button in the action row (the bottom Refresh button is gone)."""
    actions: list[Any] = []
    if on_refresh is not None:
        actions.append(
            RoundIconButton(ft.Icons.REFRESH, "Refresh agents", on_refresh, key="console-refresh")
        )
    actions += [
        RoundIconButton(ft.Icons.SEARCH, "Search agents", on_search, key="console-search"),
        RoundIconButton(ft.Icons.ADD, "New agent", on_new, tone="lime", key="console-new-agent"),
    ]
    if on_toggle_collapse is not None:
        actions.append(
            RoundIconButton(
                ft.Icons.KEYBOARD_DOUBLE_ARROW_RIGHT
                if collapsed
                else ft.Icons.KEYBOARD_DOUBLE_ARROW_LEFT,
                "Expand agent list" if collapsed else "Collapse agent list",
                on_toggle_collapse,
                key="console-collapse-toggle",
            )
        )
    return ft.Row(
        key="console-header",
        spacing=T.GAP_CARD,
        vertical_alignment=ft.CrossAxisAlignment.CENTER,
        controls=[
            ft.Column(
                spacing=0,
                expand=True,
                controls=[
                    ft.Text(
                        eyebrow.upper(),
                        style=text_style(FAMILY_MONO, T.TYPE_EYEBROW, T.TEXT_MUTED, 0.06),
                        no_wrap=True,
                    ),
                    ft.Text(
                        title,
                        style=text_style(FAMILY_INTERFACE, T.TYPE_TITLE, T.TEXT, -0.02),
                        no_wrap=True,
                        overflow=ft.TextOverflow.ELLIPSIS,
                    ),
                    # The count sits on its own line under the title: beside it, a narrow list
                    # column pushed it under the action buttons.
                    *(
                        [
                            ft.Text(
                                count,
                                key="agents-count",
                                size=12,
                                color=T.TEXT_MUTED,
                                no_wrap=True,
                                overflow=ft.TextOverflow.ELLIPSIS,
                            )
                        ]
                        if count
                        else []
                    ),
                ],
            ),
            *actions,
        ],
    )


def SectionHeader(
    eyebrow: str,
    title: str,
    *,
    key: str = "settings-header",
    title_key: str = "destination-title",
) -> Any:
    """Section header: mono eyebrow over the 34px title (settings page)."""
    return ft.Column(
        key=key,
        spacing=0,
        controls=[
            ft.Text(
                eyebrow,
                style=text_style(FAMILY_MONO, T.TYPE_EYEBROW, T.TEXT_MUTED, 0.06),
            ),
            ft.Text(
                title,
                key=title_key,
                style=text_style(FAMILY_INTERFACE, T.TYPE_TITLE, T.TEXT, -0.02),
            ),
        ],
    )


_CTA_TEXT = {"size": 16, "weight": 700, "height": 1.2}


def ActionButton(
    label: str,
    *,
    key: str,
    on_press: Callable[[], Any],
    primary: bool = False,
    disabled: bool = False,
) -> Any:
    """48px+ CTA: primary fill or ghost outline, focus wash and ring."""
    return ft.TextButton(
        key=key,
        disabled=disabled,
        style=ft.ButtonStyle(
            padding=0,
            shape=ft.RoundedRectangleBorder(radius=26),
            bgcolor=T.PRIMARY_FILL if primary else ft.Colors.TRANSPARENT,
            overlay_color={
                ft.ControlState.DEFAULT: ft.Colors.TRANSPARENT,
                ft.ControlState.FOCUSED: T.FOCUS_WASH,
            },
            side={
                ft.ControlState.DEFAULT: (
                    ft.BorderSide(1, T.PRIMARY_TEXT)
                    if not primary
                    else ft.BorderSide(3, ft.Colors.TRANSPARENT)
                ),
                ft.ControlState.FOCUSED: ft.BorderSide(3, T.TEXT),
            },
        ),
        on_click=lambda e: on_press(),
        content=ft.Container(
            height=52,
            alignment=ft.Alignment.CENTER,
            content=ft.Text(
                label,
                style=text_style(
                    FAMILY_INTERFACE,
                    _CTA_TEXT,
                    T.ON_PRIMARY if primary else T.PRIMARY_TEXT_LIGHT,
                ),
            ),
        ),
    )
