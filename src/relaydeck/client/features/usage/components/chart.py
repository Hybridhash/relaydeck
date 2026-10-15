"""StackedBarChart. Render-only: bar heights (fractions of the peak),
labels and tooltips come from `screen_model.py`.

Geometry is fixed so the parts line up: the value row, a gap, the plot, a gap and the label row.
Every bar, stub and the 0 gridline stand on the plot's bottom edge (one baseline). A label that
needs more room than one bar slot spans several slots centered on its bar, so it never truncates or
wraps inside a word. The peak value shows on long charts (30 and 90 days); short charts show every
bar's value on the wide layout.

Value labels: every bar with a value above 0 shows it. `chart_labels`
picks flat, rotated, shortest-compact or tap-only text for the slot width; the peak keeps its bold
accent and the "Peak" meta line stays.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import flet as ft

from relaydeck.client.features.usage import screen_model as sm
from relaydeck.client.features.usage.components import chart_labels as cl
from relaydeck.client.features.usage.components.primitives import ui

PLOT_PHONE = 150
PLOT_WIDE = 250
VALUE_H = 18  # the value line above the plot
LABEL_H = 16  # the axis line below the plot
GAP_V = 6  # the same gap above and below the plot
AXIS_W = 44
STUB_PX = 4
_ROLE_COLOR = {"series_1": "series_1", "series_2": "series_2", "series_other": "series_other"}


def _span(count: int) -> int:
    """How many bar slots one value or axis label may use: one for short charts, more for long ones
    (odd, so the label centers on its bar)."""
    if count <= 8:
        return 1
    if count <= 14:
        return 3
    if count <= 31:
        return 5
    return 9


def _gap(count: int, wide: bool) -> float:
    if count > 14:
        return 2 if wide else 1
    return 14 if wide else 8


def _space(units: int) -> ft.Control:
    return ft.Container(expand=units)


def _picked(candidates: list[int], priority: set[int], span: int) -> set[int]:
    """Labels that do not overlap. Priority labels (the peak, the latest bucket) are placed
    first."""
    chosen: list[int] = []
    for index in sorted(candidates, key=lambda i: (i not in priority, i)):
        if all(abs(index - other) >= span for other in chosen):
            chosen.append(index)
    return set(chosen)


def _spanned_row(
    labels: dict[int, ft.Control],
    count: int,
    span: int,
    height: float,
    pad: int,
    alignment: ft.Alignment = ft.Alignment.CENTER,
) -> ft.Row:
    """A row with one unit per bar slot, `pad` units of room at each end. A label for bar i covers
    `span` units centered on its bar (padded unit i + pad), so it sits exactly under the bar."""
    controls: list[ft.Control] = []
    cursor = 0
    for index in sorted(labels):
        start = index + pad - span // 2
        if start > cursor:
            controls.append(_space(start - cursor))
        controls.append(
            ft.Container(expand=span, height=height, alignment=alignment, content=labels[index])
        )
        cursor = start + span
    total = count + 2 * pad
    if total > cursor:
        controls.append(_space(total - cursor))
    return ft.Row(controls=controls, spacing=0, height=height)


def segment_heights(fractions: list[float], plot: float) -> list[float]:
    """Pixel heights for one bar's segments, never summing to more than `plot`. A visible segment
    keeps at least one pixel, so a sliver is not lost; the pixels that costs are taken back from
    the tallest segments, so a full bar still ends inside the plot (it used to run past the top
    and be clipped)."""
    heights = [0.0 if f <= 0 else max(1.0, f * plot) for f in fractions]
    excess = sum(heights) - plot
    for index in sorted(range(len(heights)), key=lambda i: -heights[i]):
        if excess <= 1e-9:
            break
        give = min(excess, max(0.0, heights[index] - 1.0))
        heights[index] -= give
        excess -= give
    return heights


def _segments(bar: sm.BarModel, *, plot: float, tokens: dict[str, str]) -> list[ft.Control]:
    """Top-to-bottom controls for one bar. The series are stacked bottom-up in declaration order."""
    if bar.stub:
        return [ft.Container(height=STUB_PX, bgcolor=tokens["series_other"], border_radius=2)]
    ordered = list(reversed(bar.segments))
    heights = segment_heights([segment.fraction for segment in ordered], plot)
    controls: list[ft.Control] = []
    for segment, height in zip(ordered, heights, strict=True):
        if height <= 0:
            continue
        controls.append(
            ft.Container(
                height=height,
                bgcolor=tokens[_ROLE_COLOR.get(segment.role, "series_other")],
                border_radius=ft.BorderRadius.only(top_left=6, top_right=6) if not controls else 0,
            )
        )
    return controls or [
        ft.Container(height=STUB_PX, bgcolor=tokens["series_other"], border_radius=2)
    ]


def _future_track(plot: float, tokens: dict[str, str]) -> ft.Control:
    """An hour not reached yet: an empty, faint track, never a bar or a stub."""
    return ft.Container(height=plot, bgcolor=tokens["surface_raised"], border_radius=2)


def _legend(legend: tuple[tuple[str, str], ...], *, tokens: dict[str, str]) -> ft.Row:
    items: list[ft.Control] = []
    for role, name in legend:
        items.append(
            ft.Row(
                spacing=6,
                tight=True,
                controls=[
                    ft.Container(
                        width=10,
                        height=10,
                        border_radius=3,
                        bgcolor=tokens[_ROLE_COLOR.get(role, "series_other")],
                    ),
                    ui(name, color=tokens["text_2"], size=12, weight=600, max_lines=1),
                ],
            )
        )
    return ft.Row(controls=list(items), spacing=14, wrap=True)


def _value_text(text: str, *, size: int, peak: bool, tokens: dict[str, str]) -> ft.Text:
    """One bar's value. Never wraps and never ellipsizes: the plan already sized the row for it."""
    return ft.Text(
        text,
        size=size,
        color=tokens["accent"] if peak else tokens["text"],
        weight=ft.FontWeight.W_800 if peak else ft.FontWeight.W_500,
        no_wrap=True,
        max_lines=1,
        overflow=ft.TextOverflow.CLIP,
        text_align=ft.TextAlign.CENTER,
    )


def value_plan(chart: sm.ChartModel, *, wide: bool, plot_width: float | None) -> cl.ValuePlan:
    """The value-label plan for this chart at this width (a pure function of the model)."""
    count = max(1, len(chart.bars))
    label_span = 3 if (wide and chart.grain == "hour") else _span(count)
    pad = max(_span(count), label_span) // 2
    width = plot_width if plot_width is not None else (560.0 if wide else 300.0)
    return cl.plan_values(
        [bar.value for bar in chart.bars],
        slot_px=width / (count + 2 * pad),
        base_size=12 if wide else 11,
        min_height=VALUE_H,
    )


def stacked_chart(
    chart: sm.ChartModel,
    *,
    tokens: dict[str, str],
    wide: bool,
    plot_width: float | None = None,
    selected: int | None = None,
    on_select: Callable[[int], None] | None = None,
) -> ft.Column:
    """The bars with the legend and the peak. `chart.available` False is handled by the caller,
    which shows the capacity or unavailable state instead. `plot_width` is the estimated pixel
    width of the bars; `selected` and `on_select` drive the tap readout."""
    plot = PLOT_WIDE if wide else PLOT_PHONE
    count = len(chart.bars)
    value_span = _span(count)
    # Clock labels every 3 hours on the wide layout need 3 slots each; everything else follows
    # `_span`.
    label_span = 3 if (wide and chart.grain == "hour") else value_span
    pad = max(value_span, label_span) // 2
    gap = _gap(count, wide)
    # Value labels: every bar with a value above 0 (the plan picks flat, rotated, short or tap).
    peak_index = next((index for index, bar in enumerate(chart.bars) if bar.peak), -1)
    plan = value_plan(chart, wide=wide, plot_width=plot_width)
    value_h = plan.height
    values: dict[int, ft.Control] = {}
    for index, text in plan.texts.items():
        label = _value_text(text, size=plan.size, peak=index == peak_index, tokens=tokens)
        values[index] = (
            ft.RotatedBox(quarter_turns=3, content=label) if plan.mode == cl.ROTATED else label
        )
    # Axis labels: kept apart so none overlap; the peak has priority over the rest.
    label_candidates = [index for index, bar in enumerate(chart.bars) if bar.label]
    labels: dict[int, ft.Control] = {
        index: ui(
            chart.bars[index].label,
            color=tokens["text"] if chart.bars[index].peak else tokens["text_muted"],
            size=11,
            weight=700 if chart.bars[index].peak else 500,
            max_lines=None,
            align=ft.TextAlign.CENTER,
            label=chart.bars[index].tooltip,
        )
        for index in _picked(
            label_candidates,
            {peak_index, count - 1} if chart.grain != "hour" else {peak_index},
            label_span,
        )
    }
    slots: list[ft.Control] = []
    if pad:
        slots.append(_space(pad))
    for index, bar in enumerate(chart.bars):
        slots.append(
            ft.Container(
                key=f"chart-bar-{index}",
                expand=1,
                padding=ft.Padding.symmetric(horizontal=gap / 2),
                tooltip=bar.tooltip,
                on_click=(lambda _e, i=index: on_select(i)) if on_select is not None else None,
                # The current hour's right edge carries a thin accent marker: "now".
                border=ft.Border(right=ft.BorderSide(2, tokens["accent"])) if bar.now else None,
                content=_future_track(plot, tokens)
                if bar.future
                else ft.Column(
                    spacing=0,
                    tight=True,
                    alignment=ft.MainAxisAlignment.END,
                    height=plot,
                    controls=_segments(bar, plot=plot, tokens=tokens),
                ),
            )
        )
    if pad:
        slots.append(_space(pad))
    bars_row = ft.Row(
        controls=slots, spacing=0, height=plot, vertical_alignment=ft.CrossAxisAlignment.START
    )
    if wide:
        # Three gridlines (max, middle, the 0 baseline) share the plot's edges with the bars.
        plot_block: ft.Control = ft.Stack(
            height=plot,
            controls=[
                ft.Container(
                    left=0,
                    right=0,
                    top=0,
                    height=plot,
                    content=ft.Column(
                        spacing=0,
                        alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                        controls=[
                            ft.Container(height=1, bgcolor=tokens["border"]) for _ in range(3)
                        ],
                    ),
                ),
                bars_row,
            ],
        )
    else:
        plot_block = bars_row
    plot_column = ft.Column(
        spacing=0,
        tight=True,
        expand=True,
        horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
        controls=[
            ft.Container(
                key=f"chart-values-{plan.mode}",
                height=value_h,
                data={"mode": plan.mode, "size": plan.size, "count": len(plan.texts)},
                content=_spanned_row(
                    values,
                    count,
                    1,
                    value_h,
                    pad,
                    alignment=ft.Alignment.BOTTOM_CENTER,
                ),
            ),
            ft.Container(height=GAP_V),
            plot_block,
            ft.Container(height=GAP_V),
            _spanned_row(labels, count, label_span, LABEL_H, pad),
        ],
    )
    plot_controls: list[ft.Control] = [plot_column]
    if wide:
        axis = ft.Column(
            width=AXIS_W,
            spacing=0,
            tight=True,
            controls=[
                ft.Container(height=value_h + GAP_V),
                ft.Column(
                    height=plot,
                    spacing=0,
                    alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                    controls=[
                        ui(
                            chart.axis_max,
                            color=tokens["text_muted"],
                            size=11,
                            weight=500,
                            max_lines=1,
                            align=ft.TextAlign.RIGHT,
                        ),
                        ui(
                            chart.axis_mid,
                            color=tokens["text_muted"],
                            size=11,
                            weight=500,
                            max_lines=1,
                            align=ft.TextAlign.RIGHT,
                        ),
                        ui(
                            "0",
                            color=tokens["text_muted"],
                            size=11,
                            weight=500,
                            max_lines=1,
                            align=ft.TextAlign.RIGHT,
                        ),
                    ],
                ),
                ft.Container(height=GAP_V + LABEL_H),
            ],
        )
        plot_controls = [axis, plot_column]
    meta = ft.Row(
        controls=[
            ui(
                chart.meta,
                color=tokens["text_muted"],
                size=13,
                weight=600,
                max_lines=2,
                key="chart-meta",
                label=chart.summary,
            ),
        ],
        alignment=ft.MainAxisAlignment.END,
    )
    controls: list[ft.Control] = [meta]
    if selected is not None and 0 <= selected < len(chart.bars) and chart.bars[selected].tooltip:
        controls.append(
            ui(
                chart.bars[selected].tooltip,
                color=tokens["text"],
                size=13,
                weight=700,
                max_lines=None,
                key="chart-readout",
            )
        )
    elif plan.mode == cl.TAP and any(bar.value for bar in chart.bars):
        controls.append(
            ui(
                "Tap a bar to see its value.",
                color=tokens["text_muted"],
                size=12,
                weight=600,
                max_lines=None,
                key="chart-readout-hint",
            )
        )
    if chart.legend:
        controls.append(_legend(chart.legend, tokens=tokens))
    if chart.hourly_missing:
        controls.append(
            ui(
                "Hourly data is not in this report yet.",
                color=tokens["text_muted"],
                size=12,
                weight=600,
            )
        )
    controls.append(
        ft.Row(
            controls=plot_controls,
            spacing=8 if wide else 0,
            vertical_alignment=ft.CrossAxisAlignment.START,
        )
    )
    return ft.Column(
        key="usage-chart", spacing=10, tight=True, tooltip=chart.summary, controls=controls
    )


@ft.component
def UsageChart(
    *, chart: sm.ChartModel, tokens: dict[str, str], wide: bool, plot_width: float | None = None
) -> Any:
    """The chart with its tap readout. A tap selects one bar; tapping it again clears it. The
    selection belongs to the period and grain, so a new chart starts clean."""
    picked: Any
    picked, set_picked = ft.use_state(None)  # (chart identity, bar index)
    ident = (chart.grain, len(chart.bars), chart.meta)
    selected = picked[1] if picked is not None and picked[0] == ident else None

    def choose(index: int) -> None:
        set_picked(None if selected == index else (ident, index))

    return stacked_chart(
        chart, tokens=tokens, wide=wide, plot_width=plot_width, selected=selected, on_select=choose
    )
