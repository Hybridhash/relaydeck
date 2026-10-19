"""Rename sheet (phone) and popover (wide): presentation only."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import flet as ft

from relaydeck.client.components.design_system.tokens import usage_palette
from relaydeck.client.features.inventory import rename_model as model
from relaydeck.client.theme import MIN_TOUCH

POPOVER_WIDTH = 320
FIELD_HEIGHT = 56
CHIP_HEIGHT = 36
HIT = 44  # minimum touch target
_NO_BORDER = ft.BorderSide(0, ft.Colors.TRANSPARENT)
_PLAIN = ft.ButtonStyle(
    bgcolor=ft.Colors.TRANSPARENT, side=_NO_BORDER, shape=ft.RoundedRectangleBorder(radius=8)
)
HELPER = "Shown on cards, terminals and Usage"


def ui(
    value: str,
    *,
    color: str,
    size: float = 15,
    weight: int = 700,
    key: str | None = None,
    max_lines: int | None = 1,
    visible: bool = True,
    expand: bool = False,
) -> ft.Text:
    return ft.Text(
        value,
        key=key,
        color=color,
        size=size,
        weight=ft.FontWeight(f"w{weight}"),
        max_lines=max_lines,
        overflow=ft.TextOverflow.ELLIPSIS,
        visible=visible,
        expand=expand,
    )


@dataclass(frozen=True)
class SheetContext:
    """Which agent is being renamed, for the strip above the field."""

    task: str
    provider: str
    space: str
    status: str
    bar_color: str


@dataclass(frozen=True)
class SheetProps:
    open: bool
    phone: bool
    dark: bool
    value: str
    context: SheetContext
    verdict: model.Verdict
    error: str
    status: str
    busy: bool
    uncertain: bool
    can_save: bool
    save_label: str
    select_all: bool
    suggestions: tuple[str, ...]


@dataclass(frozen=True)
class SheetActions:
    on_change: Callable[[str], Any]
    on_save: Callable[[], Any]
    on_cancel: Callable[[], Any]
    on_dismiss: Callable[[], Any]
    on_check: Callable[[], Any]
    on_pick: Callable[[str], Any]


def _strip(props: SheetProps, tokens: dict[str, str]) -> Any:
    ctx = props.context
    detail = " · ".join(part for part in (ctx.provider, ctx.space, ctx.status.upper()) if part)
    return ft.Container(
        key="agent-rename-context",
        bgcolor=tokens["bg"],
        border=ft.Border.all(1, tokens["border"]),
        border_radius=14,
        padding=ft.Padding.symmetric(horizontal=14, vertical=12),
        content=ft.Row(
            spacing=12,
            controls=[
                ft.Container(width=4, height=34, border_radius=2, bgcolor=ctx.bar_color),
                ft.Column(
                    spacing=2,
                    tight=True,
                    expand=True,
                    controls=[
                        ui(ctx.task, color=tokens["text_2"], size=13, max_lines=1)
                        if ctx.task
                        else ft.Container(height=0),
                        ui(detail, color=tokens["text_muted"], size=12, max_lines=1),
                    ],
                ),
            ],
        ),
    )


def _field(props: SheetProps, actions: SheetActions, tokens: dict[str, str]) -> Any:
    shown_error = props.verdict.text if props.verdict.blocking_error else props.error
    length = model.utf16_length(props.value)
    return ft.TextField(
        key="agent-rename-name",
        value=props.value,
        autofocus=True,
        selection=ft.TextSelection(base_offset=0, extent_offset=length)
        if props.select_all
        else None,
        capitalization=ft.TextCapitalization.WORDS,
        text_size=20,
        text_style=ft.TextStyle(weight=ft.FontWeight.W_800, color=tokens["text"]),
        color=tokens["text"],
        cursor_color=tokens["accent"],
        filled=True,
        fill_color=tokens["surface_raised"],
        height=FIELD_HEIGHT,
        content_padding=ft.Padding.only(left=16, right=4),
        border={
            ft.ControlState.DEFAULT: ft.OutlineInputBorder(
                border_radius=14, side=ft.BorderSide(1, tokens["border"])
            ),
            ft.ControlState.FOCUSED: ft.OutlineInputBorder(
                border_radius=14, side=ft.BorderSide(2, tokens["accent"])
            ),
            ft.ControlState.ERROR: ft.OutlineInputBorder(
                border_radius=14, side=ft.BorderSide(2, tokens["danger"])
            ),
        },
        error=shown_error or None,
        error_style=ft.TextStyle(color=tokens["danger"], size=12),
        error_max_lines=2,
        disabled=props.busy,
        suffix=ft.Container(
            key="agent-rename-clear",
            width=HIT,
            height=HIT,
            alignment=ft.Alignment.CENTER,
            border_radius=HIT // 2,
            ink=True,
            visible=bool(props.value) and not props.busy,
            tooltip="Clear",
            on_click=lambda _e: actions.on_change(""),
            content=ft.Icon(ft.Icons.CLOSE, size=18, color=tokens["text_muted"]),
        ),
        on_change=lambda e: actions.on_change(str(e.control.value)),
        on_submit=lambda _e: actions.on_save(),
    )


def _helper_row(props: SheetProps, tokens: dict[str, str]) -> Any:
    tone = model.counter_tone(props.value)
    ink = {"danger": tokens["danger"], "warn": tokens["warn"]}.get(tone, tokens["text_faint"])
    return ft.Row(
        alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
        controls=[
            ui(HELPER, color=tokens["text_faint"], size=12, key="agent-rename-helper", expand=True),
            ui(
                model.counter_text(props.value),
                color=ink,
                size=12,
                weight=700,
                key="agent-rename-count",
            ),
        ],
    )


def _chips(props: SheetProps, actions: SheetActions, tokens: dict[str, str]) -> list[Any]:
    if not props.suggestions:
        return []
    return [
        ui("SUGGESTIONS FROM ITS TASK", color=tokens["text_muted"], size=12, weight=700),
        ft.Row(
            wrap=True,
            spacing=8,
            run_spacing=8,
            controls=[
                ft.TextButton(
                    key=f"agent-rename-suggestion-{index}",
                    height=HIT,
                    disabled=props.busy,
                    on_click=lambda _e, name=name: actions.on_pick(name),
                    style=ft.ButtonStyle(
                        padding=0,
                        bgcolor=ft.Colors.TRANSPARENT,
                        overlay_color=ft.Colors.TRANSPARENT,
                        shape=ft.StadiumBorder(),
                        side={
                            ft.ControlState.DEFAULT: ft.BorderSide(0, ft.Colors.TRANSPARENT),
                            ft.ControlState.FOCUSED: ft.BorderSide(2, tokens["accent"]),
                        },
                    ),
                    # A focusable button (Tab, Enter, Space): the 36-point chip sits inside it.
                    content=ft.Container(
                        height=CHIP_HEIGHT,
                        padding=ft.Padding.symmetric(horizontal=14),
                        border_radius=CHIP_HEIGHT // 2,
                        bgcolor=tokens["bg"],
                        border=ft.Border.all(1, tokens["border_strong"]),
                        alignment=ft.Alignment.CENTER,
                        content=ui(name, color=tokens["text"], size=14, weight=600),
                    ),
                )
                for index, name in enumerate(props.suggestions)
            ],
        ),
    ]


def _nav(props: SheetProps, actions: SheetActions, tokens: dict[str, str]) -> Any:
    def word(label: str, color: str, weight: int = 600) -> Any:
        return ft.Text(label, color=color, size=15, weight=ft.FontWeight(f"w{weight}"))

    return ft.Row(
        alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
        vertical_alignment=ft.CrossAxisAlignment.CENTER,
        controls=[
            ft.TextButton(
                key="agent-rename-cancel",
                content=word("Cancel", tokens["text_2"]),
                height=MIN_TOUCH,
                style=_PLAIN,
                on_click=lambda _e: actions.on_cancel(),
            ),
            ui("Rename agent", color=tokens["text"], size=17, weight=800),
            ft.TextButton(
                key="agent-rename-save",
                content=props.save_label,
                height=MIN_TOUCH,
                disabled=not props.can_save,
                style=ft.ButtonStyle(
                    color={
                        ft.ControlState.DEFAULT: tokens["link"],
                        ft.ControlState.DISABLED: tokens["text_faint"],
                    },
                    text_style=ft.TextStyle(size=15, weight=ft.FontWeight.W_800),
                    bgcolor=ft.Colors.TRANSPARENT,
                    side=_NO_BORDER,
                    shape=ft.RoundedRectangleBorder(radius=8),
                ),
                on_click=lambda _e: actions.on_save(),
            ),
        ],
    )


def RenameSheetView(key: str, props: SheetProps, actions: SheetActions) -> Any:
    """The phone bottom sheet or the wide popover, same content either way."""
    tokens = usage_palette(props.dark)
    body = ft.Column(
        tight=True,
        spacing=12,
        horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
        width=None if props.phone else POPOVER_WIDTH,
        controls=[
            _nav(props, actions, tokens),
            _strip(props, tokens),
            ui("AGENT NAME", color=tokens["text_muted"], size=12, weight=700),
            _field(props, actions, tokens),
            _helper_row(props, tokens),
            *_chips(props, actions, tokens),
            ui(
                props.status,
                color=tokens["text_muted"],
                size=12,
                key="agent-rename-status",
                visible=bool(props.status),
            ),
            ft.ProgressBar(visible=props.busy),
            ft.TextButton(
                key="agent-rename-check",
                content="Check status",
                height=MIN_TOUCH,
                visible=props.uncertain,
                disabled=props.busy,
                style=_PLAIN,
                on_click=lambda _e: actions.on_check(),
            ),
        ],
    )
    if props.phone:
        return ft.BottomSheet(
            key=key,
            open=props.open,
            scrollable=True,
            bgcolor=tokens["surface_sheet"],
            shape=ft.RoundedRectangleBorder(radius=24),
            show_drag_handle=True,
            content=ft.Container(
                padding=ft.Padding.only(left=16, right=16, top=4, bottom=20), content=body
            ),
            on_dismiss=lambda _e: actions.on_dismiss(),
        )
    return ft.AlertDialog(
        key=key,
        open=props.open,
        modal=True,
        bgcolor=tokens["surface_sheet"],
        shape=ft.RoundedRectangleBorder(radius=24),
        content=ft.Container(width=POPOVER_WIDTH, content=body),
        on_dismiss=lambda _e: actions.on_dismiss(),
    )
