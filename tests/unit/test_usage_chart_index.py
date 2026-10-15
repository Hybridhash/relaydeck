"""The chart's per-day token index gives the same buckets as the per-day rescan it replaced.

The reference below is the earlier algorithm (one scan of every row per day). Rows are synthetic and
seeded: several providers (so "Other" appears), overall / workspace / not-linked scopes, rows
spread across the period edges, and a zone whose DST change falls inside the window.
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from dataclasses import replace
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from relaydeck.client.features.usage import model
from relaydeck.client.features.usage import selectors as s

EMPTY = {
    "input_uncached": 0,
    "cache_read": 0,
    "cache_write_short": 0,
    "cache_write_long": 0,
    "cache_write_unknown": 0,
    "output": 0,
    "reasoning": 0,
}
KINDS = ["kind-a", "kind-b", "kind-c", "kind-d", "kind-e"]
SPACES = ["w1", "w2"]


def _day_tokens_reference(
    rows: Sequence[s.Row], day: date, series: tuple[str, ...]
) -> tuple[int, ...]:
    values = [0] * len(series)
    for row in rows:
        if row.day != day.isoformat():
            continue
        index = s._series_index(row.kind, series)
        if index is not None:
            values[index] += row.counters.tokens
    return tuple(values)


def chart_reference(
    page: model.UsagePage, flt: s.Filter, today: date
) -> tuple[tuple[str, ...], list[tuple[str, str, tuple[int, ...]]]]:
    rows = s.filtered_rows(page, replace(flt, provider=s.ALL), today)
    series, _names = s._series(page, rows, flt)
    width = len(series)
    first, _last = s.window_bounds(flt.period, today)
    out: list[tuple[str, str, tuple[int, ...]]] = []
    if flt.period in ("7d", "30d"):
        for offset in range(s.PERIOD_DAYS[flt.period]):
            day = first + timedelta(days=offset)
            out.append(
                (s._day_label(day), day.isoformat(), _day_tokens_reference(rows, day, series))
            )
        return series, out
    weeks: dict[date, list[date]] = {}
    for offset in range(s.PERIOD_DAYS[flt.period]):
        day = first + timedelta(days=offset)
        weeks.setdefault(s._monday(day), []).append(day)
    for days in (weeks[week] for week in sorted(weeks)):
        total = [0] * width
        for day in days:
            for index, value in enumerate(_day_tokens_reference(rows, day, series)):
                total[index] += value
        out.append((s._day_label(days[0]), days[0].isoformat(), tuple(total)))
    return series, out


def make_page(seed: int, count: int, today: date, zone: str) -> model.UsagePage:
    rng = random.Random(seed)
    rows: list[dict[str, Any]] = []
    for _ in range(count):
        scope = rng.choice(["overall", "overall", "workspace", "not_linked"])
        tokens = rng.choice([0, 1, 7, 1000, 123_456])
        rows.append(
            {
                # Include days past both ends of every window.
                "day": (today - timedelta(days=rng.randint(-3, 100))).isoformat(),
                "workspace_id": rng.choice(SPACES) if scope == "workspace" else None,
                "kind": rng.choice(KINDS),
                "model": "model-a",
                "counters": {**EMPTY, "input_uncached": tokens},
                "sessions": 1,
                "cost_micro_usd": tokens,
                "scope": scope,
                "price_snapshot": None,
                "priced_cost_micro_usd": tokens,
                "priced_tokens": tokens,
                "unpriced_tokens": 0,
                "hour": rng.choice([None, rng.randint(0, 23)]),
            }
        )
    noon = datetime(today.year, today.month, today.day, 12, tzinfo=ZoneInfo(zone))
    return model.parse_usage_page(
        {
            "schema_version": 1,
            "host_id": "h1",
            "generated_ms": int(noon.timestamp() * 1000),
            "timezone": zone,
            "price_table_date": None,
            "next_cursor": None,
            "rows": rows,
            "limits": [],
            "coverage": [],
            "snapshot_labels": [],
            "kinds": [
                {
                    "kind": kind,
                    "display_name": kind.upper(),
                    "tokens_reported": True,
                    "limits_setup": None,
                    "sources": [],
                }
                for kind in KINDS
            ],
            "workspaces": [{"workspace_id": w, "label": w, "last_used_day": None} for w in SPACES],
            "capacity": "ok",
        }
    )


@pytest.mark.parametrize("seed", [1, 2, 3])
@pytest.mark.parametrize("period", ["7d", "30d", "90d"])
@pytest.mark.parametrize("provider", [s.ALL, "kind-a", "kind-d"])
@pytest.mark.parametrize("space", [s.ALL, "w1", s.UNLINKED])
@pytest.mark.parametrize(
    ("today", "zone"),
    [
        (date(2026, 10, 14), "UTC"),
        (date(2026, 11, 3), "America/New_York"),  # the fall-back day is inside the 30d window
        (date(2026, 3, 10), "America/New_York"),  # the spring-forward day is inside the 7d window
    ],
)
def test_the_indexed_chart_equals_the_per_day_rescan(
    seed: int, period: str, provider: str, space: str, today: date, zone: str
) -> None:
    page = make_page(seed, 400, today, zone)
    flt = s.Filter(period=period, space=space, provider=provider)
    expected_series, expected = chart_reference(page, flt, today)
    chart = s.chart(page, flt, today)
    assert chart.series == expected_series
    assert [(b.label, b.start, b.tokens) for b in chart.buckets] == expected


def test_a_dense_report_keeps_the_weekly_partial_edges_and_other_series() -> None:
    today = date(2026, 10, 14)  # a Wednesday: the last week is partial
    page = make_page(9, 2000, today, "UTC")
    flt = s.Filter(period="90d")
    expected_series, expected = chart_reference(page, flt, today)
    chart = s.chart(page, flt, today)
    assert s.CHART_OTHER in chart.series
    assert [(b.label, b.start, b.tokens) for b in chart.buckets] == expected
