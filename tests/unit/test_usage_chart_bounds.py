"""The chart's bars always fit inside the plot. A one-pixel sliver segment used to push a full
(peak) bar past the plot's top, where it was clipped; the pixels it costs now come out of the
tallest segment. Checked for 1d, 7d, 30d and 90d with value labels shown, phone and wide."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import flet as ft
import pytest

from relaydeck.client.components.design_system.tokens import usage_palette
from relaydeck.client.features.a11y.focus import iter_controls
from relaydeck.client.features.usage import model
from relaydeck.client.features.usage import screen_model as sm
from relaydeck.client.features.usage import selectors as s
from relaydeck.client.features.usage.components import chart

TOKENS = usage_palette(False)
NOW = int(datetime(2026, 10, 12, 12, tzinfo=UTC).timestamp() * 1000)
EMPTY = {
    "input_uncached": 0,
    "cache_read": 0,
    "cache_write_short": 0,
    "cache_write_long": 0,
    "cache_write_unknown": 0,
    "output": 0,
    "reasoning": 0,
}


def _bar_stack(model_bars: list[list[float]]) -> sm.ChartModel:
    """A chart whose bars are given as segment fractions of the peak."""
    peak = max(sum(fractions) for fractions in model_bars)
    bars = []
    for fractions in model_bars:
        total = sum(fractions)
        bars.append(
            sm.BarModel(
                label="",
                value="9.99B" if total else "",
                segments=tuple(
                    sm.SegmentModel(("series_1", "series_2", "series_other")[i % 3], f / peak)
                    for i, f in enumerate(fractions)
                ),
                stub=total == 0,
                peak=total == peak,
                tooltip="",
            )
        )
    return sm.ChartModel(True, "", "", tuple(bars), (), "9.99B", "5B", False)


def _bar_heights(control: ft.Control) -> list[float]:
    """The summed segment heights of every plotted bar."""
    sums: list[float] = []
    for item in iter_controls([control]):
        key = getattr(item, "key", None)
        key_value = getattr(key, "value", key)
        if isinstance(key_value, str) and key_value.startswith("chart-bar-"):
            content = item.content  # type: ignore[attr-defined]
            if isinstance(content, ft.Column) and content.height is not None:
                sums.append(sum(float(c.height or 0) for c in content.controls))
    return sums


@pytest.mark.parametrize("count", [24, 7, 30, 14])  # 1d (hours), 7d, 30d, 90d (weeks)
@pytest.mark.parametrize("wide", [False, True])
def test_the_tallest_bar_is_never_taller_than_the_plot(count: int, wide: bool) -> None:
    # Every bar is full-height or close to it, each with a tiny trailing series (the sliver).
    bars = [[0.62, 0.30, 0.07, 0.0004] for _ in range(count)]
    bars[count // 2] = [0.5, 0.3, 0.2, 0.00001]  # the peak
    plot = chart.PLOT_WIDE if wide else chart.PLOT_PHONE
    column = chart.stacked_chart(_bar_stack(bars), tokens=TOKENS, wide=wide, plot_width=330.0)
    heights = _bar_heights(column)
    assert len(heights) == count
    assert max(heights) <= plot + 1e-6
    # The peak bar fills the plot (to the pixel), so nothing was lost either.
    assert max(heights) >= plot - 1e-6


def test_slivers_keep_a_pixel_and_the_tallest_segment_pays_for_them() -> None:
    heights = chart.segment_heights([0.6, 0.3, 0.0999, 0.0001], 150)
    assert sum(heights) == pytest.approx(150)
    assert heights[3] == 1.0 and min(heights) >= 1.0
    assert heights[0] < 0.6 * 150  # the biggest gave the pixel back
    assert chart.segment_heights([0.0, 0.5], 100) == [0.0, 50.0]
    assert chart.segment_heights([], 100) == []


def _row(day: str, kind: str, tokens: int, hour: int | None = None) -> dict[str, Any]:
    return {
        "day": day,
        "workspace_id": None,
        "kind": kind,
        "model": "m",
        "counters": {**EMPTY, "input_uncached": tokens},
        "sessions": 1,
        "cost_micro_usd": tokens,
        "scope": "overall",
        "price_snapshot": None,
        "priced_cost_micro_usd": tokens,
        "priced_tokens": tokens,
        "unpriced_tokens": 0,
        "hour": hour,
    }


@pytest.mark.parametrize("period", ["7d", "30d", "90d"])
def test_a_built_chart_from_a_report_stays_inside_the_plot(period: str) -> None:
    rows = []
    for back in range(0, 40):
        day = datetime.fromtimestamp((NOW - back * 86_400_000) / 1000, UTC).date().isoformat()
        rows.append(_row(day, "k1", 2_000_000_000 - back * 3))
        rows.append(_row(day, "k2", 900_000_000))
        rows.append(_row(day, "k3", 700_000_000))
        rows.append(_row(day, "k4", 10 + back))  # the "other" sliver
    kinds = [
        {
            "kind": f"k{i}",
            "display_name": f"Tool {i}",
            "tokens_reported": True,
            "limits_setup": None,
            "sources": [],
        }
        for i in range(1, 5)
    ]
    doc = {
        "schema_version": 1,
        "host_id": "h1",
        "generated_ms": NOW,
        "timezone": "UTC",
        "price_table_date": None,
        "next_cursor": None,
        "rows": rows,
        "limits": [],
        "coverage": [],
        "snapshot_labels": [],
        "kinds": kinds,
        "workspaces": [],
        "capacity": "ok",
    }
    screen = sm.build(
        model.parse_usage_page(doc),
        state=sm.STATE_READY,
        flt=s.Filter(period),
        now_ms=NOW,
        width=sm.WIDTH_COMPACT,
    )
    assert screen.chart is not None and screen.chart.bars
    column = chart.stacked_chart(screen.chart, tokens=TOKENS, wide=False, plot_width=330.0)
    heights = _bar_heights(column)
    assert heights and max(heights) <= chart.PLOT_PHONE + 1e-6
