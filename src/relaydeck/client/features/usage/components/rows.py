"""RankedRow / RankedList, AttentionList and LimitRow.

Render-only: the rows, values, shares, rollups and reasons come from `screen_model.py`.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

import flet as ft

from relaydeck.client.features.usage import screen_model as sm
from relaydeck.client.features.usage.components.primitives import (
    HIT,
    action_button,
    badge,
    monogram,
    num,
    ui,
)

BAR_HEIGHT = 5
MIN_BAR_PX = 3  # a positive value is always at least this wide (spec §6.7)


def _series_color(role: str | None, tokens: dict[str, str]) -> str:
    return tokens.get(role, tokens["series_1"]) if role else tokens["series_1"]


COST_W = 124  # the cost column: the amount and its Partial badge fit without ellipsis
TRACK_UNITS = 1000  # the share track is flex-filled: the fill's weight is its share of the track
MIN_FILL_UNITS = (
    12  # a positive share fills at least 1.2% of the track (about 3 px on a 250 px track)
)


def _share_track(share: float, role: str | None, tokens: dict[str, str]) -> ft.Control:
    """The share bar, spanning the whole row width: the fill is the share of the list's
    peak, the rest of the track is the empty track. A positive share always shows a visible fill."""
    fill = 0 if share <= 0 else max(MIN_FILL_UNITS, min(TRACK_UNITS, round(share * TRACK_UNITS)))
    controls: list[ft.Control] = []
    if fill:
        controls.append(
            ft.Container(
                expand=fill,
                height=BAR_HEIGHT,
                border_radius=3,
                bgcolor=_series_color(role, tokens),
            )
        )
    if fill < TRACK_UNITS:
        controls.append(
            ft.Container(
                expand=TRACK_UNITS - fill,
                height=BAR_HEIGHT,
                border_radius=3,
                bgcolor=tokens["border"],
            )
        )
    return ft.Row(controls=controls, spacing=0, height=BAR_HEIGHT)


def ranked_row(row: sm.RowModel, *, tokens: dict[str, str], provider: bool, key: str) -> ft.Control:
    """One row: name and value on line 1, the share bar on line 2, the cost or Unpriced on line 3.
    A provider row leads with its monogram tile. The whole row is the tap target."""
    name_color = tokens["text_2"] if row.italic else tokens["text"]
    name = ui(
        row.name,
        color=name_color,
        size=15,
        weight=700,
        italic=row.italic,
        max_lines=1,
        label=row.a11y,
    )
    value = (
        ui(row.value, color=tokens["text_muted"], size=14, weight=600, max_lines=1)
        if row.honest is not None
        else num(row.value, color=tokens["text_2"], size=15, weight=700, align=ft.TextAlign.RIGHT)
    )
    lines: list[ft.Control] = [
        ft.Row(controls=[ft.Container(content=name, expand=True), value], spacing=8),
    ]
    if row.honest is None:
        lines.append(_share_track(row.share, row.series, tokens))
    if row.subline:
        subline: ft.Control = ui(
            row.subline,
            color=tokens["warn"] if row.unpriced else tokens["text_muted"],
            size=12,
            weight=600,
            max_lines=1,
        )
        if row.inferred:
            # A small neutral marker: this space has sessions linked by inference.
            subline = ft.Row(
                controls=[
                    ft.Container(content=subline, expand=True),
                    badge("includes inferred", tokens=tokens, tone="neutral"),
                ],
                spacing=8,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            )
        lines.append(subline)
    if row.note:
        # The Not linked explanation, visible: what the group means.
        lines.append(
            ui(row.note, color=tokens["text_muted"], size=12, weight=600, max_lines=2, italic=True)
        )
    if row.links:
        lines.append(
            ui(
                row.links,
                color=tokens["text_2"],
                size=12,
                weight=600,
                max_lines=2,
                key=f"{key}-links",
            )
        )
    body = ft.Column(spacing=5, tight=True, expand=True, controls=lines)
    leading: list[ft.Control] = []
    if provider:
        leading.append(monogram(row.monogram, tokens=tokens, size=32))
    return ft.Container(
        key=key,
        padding=ft.Padding.symmetric(vertical=10, horizontal=2),
        tooltip=row.a11y,
        content=ft.Row(
            controls=[*leading, body], spacing=12, vertical_alignment=ft.CrossAxisAlignment.CENTER
        ),
    )


def ranked_list(
    ranked: sm.RankedModel,
    *,
    tokens: dict[str, str],
    provider: bool,
    key_prefix: str,
    on_rollup: Callable[[Any], Any] | None = None,
) -> ft.Column:
    """The rows, then the rollup (`Other · k spaces`) when there is one (no rollup of exactly one),
    then
    the names of status-only providers beyond the bound, as readable text."""
    controls: list[ft.Control] = [
        ranked_row(
            row,
            tokens=tokens,
            provider=provider,
            key=f"{key_prefix}-row-{row.key}",
        )
        for row in ranked.rows
    ]
    if ranked.rollup is not None:
        controls.append(
            ft.Container(
                key=f"{key_prefix}-rollup",
                padding=ft.Padding.symmetric(vertical=12, horizontal=2),
                on_click=on_rollup,
                content=ft.Row(
                    controls=[
                        ft.Container(
                            content=ui(
                                ranked.rollup.text, color=tokens["text_2"], size=14, weight=600
                            ),
                            expand=True,
                        ),
                        num(ranked.rollup.value, color=tokens["text_2"], size=14, weight=600),
                        ft.Icon(ft.Icons.CHEVRON_RIGHT, color=tokens["text_muted"], size=18),
                    ],
                    spacing=8,
                ),
            )
        )
    if ranked.overflow:
        controls.append(
            ui(
                f"Also without token totals: {', '.join(ranked.overflow)}",
                color=tokens["text_muted"],
                size=12,
                weight=600,
                max_lines=None,
                key=f"{key_prefix}-overflow",
            )
        )
    return ft.Column(spacing=2, tight=True, controls=controls)


def attention_list(
    attention: sm.AttentionModel,
    *,
    tokens: dict[str, str],
    on_action: Callable[[str, str], Callable[[Any], Any]],
    on_more: Callable[[Any], Any] | None = None,
) -> ft.Column:
    """Problems only. Each item has one action; a row that needs no action shows its reason alone.
    Problems only. Each item has one action; a row that needs no action shows its reason alone."""
    rows: list[ft.Control] = []
    for item in attention.items:
        reason_color = tokens["warn"] if item.tone == "warn" else tokens["text_2"]
        text = ft.Column(
            spacing=2,
            tight=True,
            expand=True,
            controls=[
                ui(item.name, color=tokens["text"], size=15, weight=700, max_lines=1),
                ui(item.reason, color=reason_color, size=13, weight=600, max_lines=2),
            ],
        )
        controls: list[ft.Control] = [monogram(item.monogram, tokens=tokens, size=36), text]
        if item.action is not None and item.action_label is not None:
            controls.append(
                action_button(
                    item.action_label,
                    tokens=tokens,
                    key=f"attention-{item.action}-{item.key}",
                    on_click=on_action(item.action, item.key),
                    primary=item.action == "setup",
                )
            )
        rows.append(
            ft.Container(
                key=f"attention-item-{item.key}",
                padding=ft.Padding.symmetric(vertical=8),
                content=ft.Row(
                    controls=controls, spacing=12, vertical_alignment=ft.CrossAxisAlignment.CENTER
                ),
            )
        )
    if attention.more:
        rows.append(
            ft.Container(
                key="attention-more",
                height=44,
                alignment=ft.Alignment.CENTER_LEFT,
                on_click=on_more,
                tooltip="See all items that need attention",
                content=ui(
                    f"+{attention.more} more ›",
                    color=tokens["link"],
                    size=14,
                    weight=700,
                    max_lines=1,
                ),
            )
        )
    return ft.Column(spacing=0, tight=True, controls=rows)


def attention_header(count: int, *, tokens: dict[str, str]) -> ft.Row:
    return ft.Row(
        controls=[
            ui("Needs attention", color=tokens["text"], size=18, weight=800, key="attention-title"),
            badge(str(count), tokens=tokens, tone="neutral", key="attention-count"),
        ],
        spacing=8,
        tight=True,
    )


def _refresh_control(
    refresh: sm.RefreshModel, *, tokens: dict[str, str], on_refresh: Callable[[str], None] | None
) -> ft.Control:
    """The small refresh button (44 px for touch). A spinner while the read runs; disabled during
    the server's cooldown."""
    if refresh.busy:
        content: ft.Control = ft.ProgressRing(
            width=20, height=20, stroke_width=2, color=tokens["text_2"]
        )
        return ft.Semantics(
            key=f"limit-refresh-{refresh.kind}",
            label=f"{refresh.label}, reading",
            content=ft.Container(
                width=44, height=44, alignment=ft.Alignment.CENTER, content=content
            ),
        )
    if refresh.cooldown_minutes is not None:
        wait = f"{refresh.cooldown_minutes} min" if refresh.cooldown_minutes else "<1 min"
        # Not an icon button: a paused refresh must not look tappable.
        return ft.Semantics(
            key=f"limit-refresh-{refresh.kind}",
            label=f"{refresh.label}, available in {wait}",
            disabled=True,
            content=ft.Container(
                width=44,
                height=44,
                alignment=ft.Alignment.CENTER,
                content=ft.Column(
                    spacing=0,
                    tight=True,
                    alignment=ft.MainAxisAlignment.CENTER,
                    horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                    controls=[
                        ft.Icon(ft.Icons.SCHEDULE, size=16, color=tokens["text_muted"]),
                        ui(wait, color=tokens["text_muted"], size=10, weight=600, max_lines=1),
                    ],
                ),
            ),
        )
    return ft.IconButton(
        key=f"limit-refresh-{refresh.kind}",
        icon=ft.Icons.REFRESH,
        icon_color=tokens["text_2"] if refresh.enabled else tokens["text_muted"],
        width=44,
        height=44,
        disabled=not refresh.enabled or on_refresh is None,
        tooltip=refresh.label,
        on_click=(lambda _e, kind=refresh.kind: on_refresh(kind)) if on_refresh else None,
    )


def limit_row(
    row: sm.LimitRowModel,
    *,
    tokens: dict[str, str],
    key: str,
    on_refresh: Callable[[str], None] | None = None,
) -> ft.Control:
    """A connected provider's limit windows. Each window has its own percent and bar; an ended
    window says so in words (never by colour alone). A tool that can be read on demand has a
    small refresh button at the end of its header, and its note under the text."""
    text_lines: list[ft.Control] = [
        ui(row.name, color=tokens["text"], size=15, weight=700, max_lines=1),
        ui(row.subline, color=tokens["text_muted"], size=12, weight=600, max_lines=2),
    ]
    if row.refresh is not None and row.refresh.note:
        text_lines.append(
            ui(
                row.refresh.note,
                color=tokens["text_muted"],
                size=12,
                weight=600,
                max_lines=2,
                key=f"limit-refresh-note-{row.key}",
            )
        )
    header_controls: list[ft.Control] = [
        monogram(row.monogram, tokens=tokens, size=36),
        ft.Column(spacing=2, tight=True, expand=True, controls=text_lines),
    ]
    if row.refresh is not None:
        header_controls.append(_refresh_control(row.refresh, tokens=tokens, on_refresh=on_refresh))
    header = ft.Row(
        controls=header_controls,
        spacing=12,
        vertical_alignment=ft.CrossAxisAlignment.CENTER,
    )
    blocks: list[ft.Control] = [header]
    for window in row.windows:
        blocks.append(_window_block(window, tokens=tokens))
    return ft.Container(
        key=key,
        padding=ft.Padding.symmetric(vertical=8),
        content=ft.Column(spacing=6, tight=True, controls=blocks),
    )


def _window_block(window: sm.WindowModel, *, tokens: dict[str, str]) -> ft.Control:
    fill = {
        "series_1": tokens["series_1"],
        "warn": tokens["warn"],
        "danger": tokens["danger"],
    }.get(window.role, tokens["border_strong"])
    if window.ended:
        fill = tokens["border_strong"]
    top = ft.Row(
        controls=[
            ft.Container(
                content=ui(
                    window.amount or window.label,
                    color=tokens["text_2"],
                    size=13,
                    weight=700,
                    max_lines=2 if window.amount else 1,
                    key="limit-amount" if window.amount else None,
                ),
                expand=True,
            ),
            *(
                [
                    num(
                        window.percent,
                        color=tokens["text"],
                        size=18,
                        weight=700,
                        align=ft.TextAlign.RIGHT,
                    )
                ]
                if window.bar
                else []
            ),
        ],
        spacing=8,
    )
    track = ft.ProgressBar(
        value=window.fraction,
        height=6,
        color=fill,
        bgcolor=tokens["border"],
        border_radius=3,
    )
    controls: list[ft.Control] = [top, track] if window.bar else [top]
    if window.resets:
        controls.append(
            ui(window.resets, color=tokens["text_muted"], size=12, weight=600, max_lines=1)
        )
    return ft.Column(spacing=4, tight=True, controls=controls)


def limit_rows(
    rows: Sequence[sm.LimitRowModel],
    *,
    tokens: dict[str, str],
    on_refresh: Callable[[str], None] | None = None,
) -> list[ft.Control]:
    return [
        limit_row(row, tokens=tokens, key=f"limit-{row.key}", on_refresh=on_refresh) for row in rows
    ]


__all__ = [
    "attention_header",
    "attention_list",
    "limit_rows",
    "ranked_list",
]


def spaces_table(
    ranked: sm.RankedModel,
    *,
    tokens: dict[str, str],
    total: int,
    on_show_all: Callable[[Any], Any] | None,
    show_all: bool,
) -> ft.Column:
    """The iPad spaces table (table variant): Name | Share | Tokens | Cost, with a header
    row,
    the rollup, and "Show all n spaces". Search and sort arrive with the full picker."""

    def heading(
        label: str, *, expand: bool | int = False, width: float | None = None, align: Any = None
    ) -> ft.Control:
        return ft.Container(
            width=width,
            expand=expand,
            content=ui(
                label.upper(),
                color=tokens["text_muted"],
                size=11,
                weight=700,
                max_lines=1,
                align=align,
            ),
        )

    header = ft.Row(
        controls=[
            heading("Space", expand=12),
            heading("Share", expand=10),
            heading("Tokens", width=80, align=ft.TextAlign.RIGHT),
            heading("Cost", width=COST_W, align=ft.TextAlign.RIGHT),
        ],
        spacing=10,
    )
    controls: list[ft.Control] = [header]
    for row in ranked.rows:
        controls.append(
            ft.Container(
                key=f"usage-table-row-{row.key}",
                padding=ft.Padding.symmetric(vertical=8),
                tooltip=row.a11y,
                content=ft.Row(
                    controls=[
                        ft.Container(
                            expand=12,
                            content=ft.Column(
                                spacing=2,
                                tight=True,
                                controls=[
                                    ui(
                                        row.name,
                                        color=tokens["text_2"] if row.italic else tokens["text"],
                                        size=14,
                                        weight=700,
                                        italic=row.italic,
                                        max_lines=1,
                                    ),
                                    *(
                                        [badge("includes inferred", tokens=tokens, tone="neutral")]
                                        if row.inferred
                                        else []
                                    ),
                                    *(
                                        [
                                            ui(
                                                row.note,
                                                color=tokens["text_muted"],
                                                size=11,
                                                weight=600,
                                                italic=True,
                                                max_lines=2,
                                            )
                                        ]
                                        if row.note
                                        else []
                                    ),
                                ],
                            ),
                        ),
                        ft.Container(
                            expand=10, content=_share_track(row.share, row.series, tokens)
                        ),
                        ft.Container(
                            width=80,
                            content=num(
                                row.value, color=tokens["text_2"], size=14, align=ft.TextAlign.RIGHT
                            ),
                        ),
                        ft.Container(
                            width=COST_W,
                            content=ft.Column(
                                spacing=3,
                                tight=True,
                                horizontal_alignment=ft.CrossAxisAlignment.END,
                                controls=[
                                    ui(
                                        row.amount or "—",
                                        color=tokens["warn"] if row.unpriced else tokens["text_2"],
                                        size=13,
                                        weight=600,
                                        max_lines=None,
                                        align=ft.TextAlign.RIGHT,
                                    ),
                                    *(
                                        [badge("Partial", tokens=tokens, tone="warn")]
                                        if row.partial
                                        else []
                                    ),
                                ],
                            ),
                        ),
                    ],
                    spacing=10,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
            )
        )
    if ranked.rollup is not None:
        controls.append(
            ui(
                f"{ranked.rollup.text} · {ranked.rollup.value}",
                color=tokens["text_2"],
                size=13,
                weight=600,
                max_lines=1,
                key="usage-table-rollup",
            )
        )
    if show_all and on_show_all is not None:
        controls.append(
            ft.Button(
                key="usage-table-show-all",
                content=ui(
                    f"Show all {total} spaces",
                    color=tokens["text"],
                    size=14,
                    weight=700,
                    max_lines=1,
                ),
                height=HIT,
                bgcolor=tokens["surface_raised"],
                color=tokens["text"],
                on_click=on_show_all,
            )
        )
    return ft.Column(spacing=4, tight=True, controls=controls)
