"""Small pieces of the Server health panel: tile, sparkline, routes table, count lists, pill.

Declarative and prop-driven; the panel owns state and polling."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import flet as ft
import flet.canvas as cv

from relaydeck.client.components.design_system import tokens as T
from relaydeck.client.components.design_system.fonts import FAMILY_INTERFACE, FAMILY_MONO
from relaydeck.client.components.design_system.primitives import text_style
from relaydeck.client.components.design_system.tokens import usage_palette
from relaydeck.client.features.settings.server_health_model import (
    DEFAULT_SPARK_WIDTH,
    TONE_DANGER,
    TONE_OK,
    TONE_WARN,
    RefreshRow,
    RouteRow,
    Tile,
    format_age,
    sparkline_points,
)

SPARK_HEIGHT = 36.0
TYPE_LABEL = {"size": 11, "weight": 500, "height": 14 / 11}
TYPE_BIG = {"size": 30, "weight": 700, "height": 34 / 30}
TYPE_UNIT = {"size": 12, "weight": 500, "height": 16 / 12}
TYPE_NOTE = {"size": 12, "weight": 400, "height": 16 / 12}
TYPE_ROW = {"size": 13, "weight": 400, "height": 18 / 13}


def tone_color(tone: str) -> str:
    """Success, amber and danger come from the theme; muted is the secondary text colour."""
    return {
        TONE_OK: T.DONE,
        TONE_WARN: T.BLOCKED,
        TONE_DANGER: usage_palette(True)["danger"],
    }.get(tone, T.TEXT_MUTED)


@ft.component
def Sparkline(series: tuple[float, ...], *, label: str, name: str) -> Any:
    """A light line that fills its tile; the canvas reports its size and the line is redrawn."""
    size, set_size = ft.use_state((0.0, 0.0))
    width = size[0] or DEFAULT_SPARK_WIDTH
    height = size[1] or SPARK_HEIGHT
    points = sparkline_points(series, width, height)

    def resized(event: Any) -> None:
        if (event.width, event.height) != size:
            set_size((event.width, event.height))

    elements: list[Any] = [cv.Path.MoveTo(*points[0])] if points else []
    elements += [cv.Path.LineTo(x, y) for x, y in points[1:]]
    return ft.Semantics(
        key=f"health-spark-{name}-semantics",
        label=label,
        content=ft.Container(
            key=f"health-spark-{name}",
            height=SPARK_HEIGHT,
            content=cv.Canvas(
                expand=True,
                on_resize=resized,
                shapes=[
                    cv.Path(
                        elements,
                        paint=ft.Paint(
                            color=T.PRIMARY_TEXT,
                            stroke_width=1.5,
                            style=ft.PaintingStyle.STROKE,
                            stroke_join=ft.StrokeJoin.ROUND,
                        ),
                    )
                ]
                if elements
                else [],
            ),
        ),
    )


@ft.component
def HealthTile(tile: Tile) -> Any:
    key = f"health-tile-{tile.key}"
    return ft.Container(
        key=key,
        expand=True,
        padding=ft.Padding.all(12),
        border_radius=ft.BorderRadius.all(T.RADIUS_TILE),
        bgcolor=T.CARD,
        border=ft.Border.all(1, T.BORDER),
        content=ft.Column(
            spacing=4,
            controls=[
                ft.Text(tile.label, style=text_style(FAMILY_MONO, TYPE_LABEL, T.TEXT_MUTED, 0.08)),
                ft.Row(
                    spacing=6,
                    vertical_alignment=ft.CrossAxisAlignment.END,
                    controls=[
                        ft.Text(
                            tile.value,
                            key=f"{key}-value",
                            style=text_style(FAMILY_INTERFACE, TYPE_BIG, T.TEXT),
                        ),
                        ft.Text(tile.unit, style=text_style(FAMILY_MONO, TYPE_UNIT, T.TEXT_MUTED)),
                    ],
                ),
                Sparkline(tile.series, label=tile.caption, name=tile.key),
                ft.Text(
                    tile.note,
                    key=f"{key}-note",
                    style=text_style(FAMILY_MONO, TYPE_NOTE, T.TEXT_MUTED),
                ),
            ],
        ),
    )


def _cell(text: str, width: float, *, muted: bool = False, align: Any = None) -> Any:
    return ft.Container(
        width=width,
        content=ft.Text(
            text,
            text_align=align or ft.TextAlign.RIGHT,
            style=text_style(FAMILY_MONO, TYPE_ROW, T.TEXT_MUTED if muted else T.TEXT),
        ),
    )


def _route_label(row: RouteRow) -> Any:
    return ft.Text(
        f"{row.method} {row.path}",
        expand=True,
        max_lines=1,
        overflow=ft.TextOverflow.ELLIPSIS,
        style=text_style(FAMILY_MONO, TYPE_ROW, T.TEXT),
    )


@ft.component
def RoutesTable(rows: tuple[RouteRow, ...], *, phone: bool) -> Any:
    heading = text_style(FAMILY_MONO, TYPE_LABEL, T.TEXT_MUTED, 0.08)
    title = ft.Semantics(
        header=True,
        content=ft.Text("SLOWEST ROUTES · P95, LAST 15 MIN", style=heading),
    )
    if not rows:
        body: list[Any] = [
            ft.Text("No requests yet", style=text_style(FAMILY_MONO, TYPE_ROW, T.TEXT_MUTED))
        ]
    elif phone:
        body = [
            ft.Column(
                key=f"health-route-{index}",
                spacing=2,
                controls=[
                    _route_label(row),
                    ft.Text(
                        f"{row.calls} calls · p50 {row.p50} · p95 {row.p95} · {row.errors} errors",
                        style=text_style(FAMILY_MONO, TYPE_NOTE, T.TEXT_MUTED),
                    ),
                ],
            )
            for index, row in enumerate(rows)
        ]
    else:
        body = [
            ft.Row(
                spacing=8,
                controls=[
                    ft.Semantics(
                        header=True,
                        content=ft.Text("ROUTE", expand=True, style=heading),
                    ),
                    *[
                        ft.Semantics(
                            header=True,
                            content=ft.Container(
                                width=width,
                                content=ft.Text(name, text_align=ft.TextAlign.RIGHT, style=heading),
                            ),
                        )
                        for name, width in (
                            ("CALLS", 56),
                            ("P50", 64),
                            ("P95", 64),
                            ("ERRORS", 64),
                        )
                    ],
                ],
            ),
            *[
                ft.Row(
                    key=f"health-route-{index}",
                    spacing=8,
                    controls=[
                        _route_label(row),
                        _cell(row.calls, 56),
                        _cell(row.p50, 64),
                        _cell(row.p95, 64),
                        _cell(row.errors, 64, muted=row.errors == "0"),
                    ],
                )
                for index, row in enumerate(rows)
            ],
        ]
    return ft.Column(
        key="health-routes",
        spacing=8,
        controls=[title, *body],
    )


@ft.component
def CountList(title: str, rows: tuple[tuple[str, str], ...], *, slug: str) -> Any:
    return ft.Column(
        key=f"health-{slug}",
        spacing=4,
        controls=[
            ft.Semantics(
                header=True,
                content=ft.Text(
                    title.upper(), style=text_style(FAMILY_MONO, TYPE_LABEL, T.TEXT_MUTED, 0.08)
                ),
            ),
            *[
                ft.Row(
                    alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                    controls=[
                        ft.Text(name, style=text_style(FAMILY_MONO, TYPE_ROW, T.TEXT_MUTED)),
                        ft.Text(value, style=text_style(FAMILY_MONO, TYPE_ROW, T.TEXT)),
                    ],
                )
                for name, value in rows
            ],
        ],
    )


@ft.component
def RefreshPill(row: RefreshRow) -> Any:
    color = tone_color(row.tone)
    return ft.Row(
        key=f"health-refresh-row-{row.kind}",
        alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
        controls=[
            ft.Text(
                f"{row.kind.capitalize()} · {row.when}",
                style=text_style(FAMILY_MONO, TYPE_ROW, T.TEXT_MUTED),
            ),
            ft.Container(
                padding=ft.Padding.symmetric(horizontal=8, vertical=2),
                border_radius=ft.BorderRadius.all(999),
                bgcolor=T.RAISED,
                border=ft.Border.all(1, color),
                content=ft.Text(
                    row.label, style=text_style(FAMILY_MONO, TYPE_NOTE, color), no_wrap=True
                ),
            ),
        ],
    )


@ft.component
def UpdatedAgo(generated_ms: int, *, active: bool) -> Any:
    """'updated 2 s ago', ticking on its own so the rest of the panel stays still."""
    _, set_tick = ft.use_state(0)
    task: Any = ft.use_ref(None)

    def start() -> None:
        if not active:
            return

        async def tick() -> None:
            while True:
                await asyncio.sleep(1)
                set_tick(lambda value: value + 1)

        task.current = asyncio.ensure_future(tick())

    def stop() -> None:
        if task.current is not None and not task.current.done():
            task.current.cancel()
        task.current = None

    ft.use_effect(start, dependencies=[active], cleanup=stop)
    age = format_age(time.time() - generated_ms / 1000)
    return ft.Text(
        f"updated {age}",
        key="health-updated",
        style=text_style(FAMILY_MONO, TYPE_ROW, T.TEXT_MUTED),
    )
