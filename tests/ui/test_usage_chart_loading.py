"""Chart value labels and the loading state.

Labels: every bar with a value above 0 shows it; no label is ellipsized; a narrow slot rotates the
text, then shortens it, then falls back to tap-only. Loading: a fetch in flight shows the loading
state, never "unavailable"; a failure shows unavailable; Reduce Motion makes the icon static.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import flet as ft
import pytest

from relaydeck.client.components.design_system.tokens import usage_palette
from relaydeck.client.features.a11y.focus import iter_controls
from relaydeck.client.features.usage import screen_model as sm
from relaydeck.client.features.usage.api import UsageFetchError, UsageQuery
from relaydeck.client.features.usage.components import chart as chart_mod
from relaydeck.client.features.usage.components import chart_labels as cl
from relaydeck.client.features.usage.panel import UsageCard, UsagePanel
from tests.support.host_harness import render_component, run

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "usage" / "client"
TOKENS = {
    "series_1": "#111111",
    "series_2": "#222222",
    "series_other": "#333333",
    "surface_raised": "#444444",
    "text": "#000000",
    "text_muted": "#555555",
    "accent": "#0066cc",
    "border": "#cccccc",
}


def _bars(values: list[str]) -> tuple[sm.BarModel, ...]:
    peak = max(range(len(values)), key=lambda i: len(values[i]) and i, default=0)
    return tuple(
        sm.BarModel(
            label=str(index + 1) if index % 3 == 0 else "",
            value=value,
            segments=(),
            stub=not value,
            peak=index == peak,
            tooltip=f"Day {index + 1} · {value or 'no usage'}",
        )
        for index, value in enumerate(values)
    )


def _chart(values: list[str], grain: str = "day") -> sm.ChartModel:
    return sm.ChartModel(
        available=True,
        meta="Peak 0.31B · Tue",
        summary="Daily tokens",
        bars=_bars(values),
        legend=(),
        axis_max="0.4B",
        axis_mid="0.2B",
        hourly_missing=False,
        grain=grain,
    )


def _values(count: int, *, zero_every: int = 5) -> list[str]:
    forms = ["0.31B", "85M", "4.2K", "18M", "940K", "2.02B"]
    return [
        "" if index % zero_every == zero_every - 1 else forms[index % len(forms)]
        for index in range(count)
    ]


def _texts_in(control: ft.Control) -> list[ft.Text]:
    return [c for c in iter_controls([control]) if isinstance(c, ft.Text)]


def _value_row(column: ft.Column) -> ft.Container:
    for control in iter_controls([column]):
        if isinstance(control, ft.Container) and str(control.key or "").startswith("chart-values-"):
            return control
    raise AssertionError("no value row")


CASES = [
    # (name, bar count, grain, wide, plot width, expected mode)
    ("phone 7d", 7, "day", False, 318.0, cl.FLAT),
    ("phone 30d", 30, "day", False, 318.0, cl.TAP),
    ("phone today", 24, "hour", False, 318.0, cl.ROTATED),
    ("phone 90d", 90, "day", False, 318.0, cl.TAP),
    ("ipad portrait 30d", 30, "day", False, 696.0, cl.ROTATED),
    ("ipad wide 7d", 7, "day", True, 560.0, cl.FLAT),
    ("ipad wide 30d", 30, "day", True, 560.0, cl.ROTATED),
]


@pytest.mark.parametrize(("name", "count", "grain", "wide", "width", "mode"), CASES)
def test_the_value_labels_follow_the_slot_width(
    name: str, count: int, grain: str, wide: bool, width: float, mode: str
) -> None:
    chart = _chart(_values(count), grain)
    plan = chart_mod.value_plan(chart, wide=wide, plot_width=width)
    assert plan.mode == mode, name
    column = chart_mod.stacked_chart(chart, tokens=TOKENS, wide=wide, plot_width=width)
    row = _value_row(column)
    assert row.key == f"chart-values-{mode}"
    assert row.data["mode"] == mode
    texts = _texts_in(row)
    with_value = [i for i, bar in enumerate(chart.bars) if bar.value]
    if mode == cl.TAP:
        assert texts == []  # no labels on the bars; the value shows on tap
        assert row.height == 0
    else:
        # Every bar with a value above 0 has a label; bars without one have none.
        assert sorted(plan.texts) == with_value
        assert len(texts) == len(with_value)
        assert min(plan.size, 99) >= cl.MIN_SIZE
        for text in texts:
            assert text.size is not None and text.size >= cl.MIN_SIZE
            assert text.overflow != ft.TextOverflow.ELLIPSIS  # never ellipsized
            assert text.no_wrap is True
        if mode == cl.ROTATED:
            rotated = [c for c in iter_controls([row]) if isinstance(c, ft.RotatedBox)]
            assert len(rotated) == len(with_value)
            assert all(box.quarter_turns == 3 for box in rotated)  # reads bottom to top
        else:
            assert not [c for c in iter_controls([row]) if isinstance(c, ft.RotatedBox)]


def test_the_peak_label_keeps_its_highlight_and_the_meta_line_stays() -> None:
    chart = _chart(_values(7))
    column = chart_mod.stacked_chart(chart, tokens=TOKENS, wide=False, plot_width=318.0)
    row = _value_row(column)
    peak_index = next(i for i, bar in enumerate(chart.bars) if bar.peak)
    texts = _texts_in(row)
    shown = [c for c in texts if c.value == chart.bars[peak_index].value]
    assert any(c.weight == ft.FontWeight.W_800 and c.color == TOKENS["accent"] for c in shown)
    meta = next(c for c in iter_controls([column]) if getattr(c, "key", None) == "chart-meta")
    assert meta.value == "Peak 0.31B · Tue"


def test_the_tap_fallback_shows_the_value_of_the_tapped_bar() -> None:
    chart = _chart(_values(30))
    column = chart_mod.stacked_chart(
        chart, tokens=TOKENS, wide=False, plot_width=318.0, selected=None, on_select=lambda _i: None
    )
    keys = {getattr(c, "key", None) for c in iter_controls([column])}
    assert "chart-readout-hint" in keys and "chart-readout" not in keys
    picked = chart_mod.stacked_chart(
        chart, tokens=TOKENS, wide=False, plot_width=318.0, selected=2, on_select=lambda _i: None
    )
    readout = next(c for c in iter_controls([picked]) if getattr(c, "key", None) == "chart-readout")
    assert readout.value == chart.bars[2].tooltip


def test_a_label_never_overlaps_its_neighbour_in_the_flat_form() -> None:
    chart = _chart(_values(7))
    plan = chart_mod.value_plan(chart, wide=False, plot_width=318.0)
    slot = 318.0 / 7
    assert plan.mode == cl.FLAT
    assert all(cl.text_width(text, plan.size) <= slot for text in plan.texts.values())


@pytest.mark.parametrize(
    ("text", "expected"),
    [("0.31B", "0.3B"), ("2.02B", "2.0B"), ("4.2K", "4K"), ("18M", "18M"), ("0.5K", "0.5K")],
)
def test_the_shortest_compact_form(text: str, expected: str) -> None:
    assert cl.shortest(text) == expected


# --- loading ---------------------------------------------------------------------------------


def _raw_page() -> dict[str, Any]:
    document: dict[str, Any] = json.loads((FIXTURES / "page-overall.json").read_text())
    return document


@ft.component
def Panel(fetch: Any, reduce_motion: bool = False) -> Any:
    return UsagePanel(
        fetch=fetch,
        identity="acct-a",
        host_id="host-a",
        active=True,
        dark=False,
        wide=False,
        reduce_motion=reduce_motion,
    )


def _all_texts(app: Any) -> list[str]:
    return [
        str(c.value) for c in iter_controls([app.page.views[0].controls]) if isinstance(c, ft.Text)
    ]


def _has(app: Any, key: str) -> bool:
    try:
        app.find(key)
    except KeyError:
        return False
    return True


async def _until(app: Any, check: Callable[[], bool]) -> None:
    async with asyncio.timeout(4):
        while True:
            await app.settle()
            if check():
                return
            await asyncio.sleep(0.01)


def _blocked() -> Callable[[UsageQuery], Any]:
    async def fetch(_query: UsageQuery) -> Mapping[str, object]:
        await asyncio.Event().wait()
        return {}

    return fetch


def _served_then_blocked() -> Callable[[UsageQuery], Any]:
    calls: list[UsageQuery] = []

    async def fetch(query: UsageQuery) -> Mapping[str, object]:
        calls.append(query)
        if len(calls) == 1:
            return _raw_page()
        await asyncio.Event().wait()
        return {}

    return fetch


def test_an_in_flight_first_load_shows_loading_not_unavailable() -> None:
    async def scenario() -> None:
        app = await render_component(Panel, fetch=_blocked())
        await app.settle()
        assert _has(app, "usage-skeleton")
        assert _has(app, "usage-loading-icon")
        assert "Loading usage…" in _all_texts(app)
        assert not _has(app, "usage-state-message")
        assert not any("unavailable" in text.lower() for text in _all_texts(app))
        assert _has(app, "period-today")  # the header stays live

    run(scenario())


def test_an_in_flight_period_switch_shows_loading_not_unavailable() -> None:
    async def scenario() -> None:
        app = await render_component(Panel, fetch=_served_then_blocked())
        await _until(app, lambda: _has(app, "summary-tokens"))
        await app.set_props_and_fire("period-today", {}, "click")
        await app.settle()
        assert _has(app, "usage-skeleton")
        assert not _has(app, "summary-tokens")  # the other period's numbers are not shown
        assert not _has(app, "usage-state-message")
        assert not any("unavailable" in text.lower() for text in _all_texts(app))
        assert any("loading…" in text for text in _all_texts(app))  # the meta line says so

    run(scenario())


def test_a_failed_fetch_shows_unavailable() -> None:
    async def failing(_query: UsageQuery) -> Mapping[str, object]:
        raise UsageFetchError("malformed_page")

    async def scenario() -> None:
        app = await render_component(Panel, fetch=failing)
        await _until(app, lambda: _has(app, "usage-state-message"))
        assert not _has(app, "usage-skeleton")
        assert "Usage is unavailable right now." in _all_texts(app)

    run(scenario())


def test_reduce_motion_gives_the_static_icon() -> None:
    async def scenario() -> None:
        animated = await render_component(Panel, fetch=_blocked())
        await animated.settle()
        assert animated.find("usage-loading-icon").data == {"animated": True}
        static = await render_component(Panel, fetch=_blocked(), reduce_motion=True)
        await static.settle()
        icon = static.find("usage-loading-icon")
        assert icon.data == {"animated": False}
        assert all(
            getattr(bar, "animate", None) is None
            for bar in iter_controls([icon])
            if isinstance(bar, ft.Container) and bar is not icon
        )

    run(scenario())


def test_the_console_strip_says_loading_during_its_first_fetch() -> None:
    async def scenario() -> None:
        app = await render_component(
            UsageCard,
            fetch=_blocked(),
            identity="acct-a",
            host_id="host-a",
            active=True,
            dark=True,
            on_open=lambda: None,
        )
        await app.settle()
        assert app.find("console-headline").value == "Loading…"
        assert not any("unavailable" in text.lower() for text in _all_texts(app))

    run(scenario())


def test_the_medium_summary_cards_are_fixed_width_and_not_expanded() -> None:
    """`expand=False` on a child of the wrapped row crashed the tablet layout (grey page): the
    cards keep a fixed width and no expand value."""
    from relaydeck.client.features.usage.components import cards

    summary = sm.SummaryModel(
        tokens="13.45B",
        context="3 providers",
        cost="$1.00",
        partial=False,
        partial_note=None,
        cost_caption=None,
        busiest_value="2.96B",
        busiest_caption="Mon",
    )
    row = cards.kpi_row(summary, tokens=usage_palette(True), wide=False)
    assert all(c.expand is None and c.width == 230 for c in row.controls)  # type: ignore[attr-defined]
