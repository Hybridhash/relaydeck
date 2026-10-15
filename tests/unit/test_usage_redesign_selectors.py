"""Selectors: filters, one total selector with reconciliation,
cost
states, ranking and rollup, provider status and limits, chart buckets, the space picker, formatting,
the console card and a 500-space / 40-provider synthetic scale page. Synthetic pages only."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from relaydeck.client.features.usage import model
from relaydeck.client.features.usage import selectors as s

ZONE = "UTC"
NOW = int(datetime(2026, 10, 11, 12, tzinfo=ZoneInfo(ZONE)).timestamp() * 1000)
TODAY = date(2026, 10, 11)
EMPTY_COUNTERS = {
    "input_uncached": 0,
    "cache_read": 0,
    "cache_write_short": 0,
    "cache_write_long": 0,
    "cache_write_unknown": 0,
    "output": 0,
    "reasoning": 0,
}


def row(
    day: str,
    scope: str,
    kind: str,
    tokens: int,
    *,
    space: str | None = None,
    hour: int | None = None,
    unpriced: int = 0,
    priced_cost: int | None = None,
    priced_tokens: int | None = None,
    model_name: str = "model-a",
) -> dict[str, Any]:
    priced_tokens = tokens - unpriced if priced_tokens is None else priced_tokens
    priced_cost = priced_tokens if priced_cost is None else priced_cost  # 1 micro-USD per token
    return {
        "day": day,
        "workspace_id": space if scope == "workspace" else None,
        "kind": kind,
        "model": model_name,
        "counters": {**EMPTY_COUNTERS, "input_uncached": tokens},
        "sessions": 1,
        "cost_micro_usd": None if unpriced else priced_cost,
        "scope": scope,
        "price_snapshot": None,
        "priced_cost_micro_usd": priced_cost,
        "priced_tokens": priced_tokens,
        "unpriced_tokens": unpriced,
        "hour": hour,
    }


def kind_doc(kind: str, name: str, setup: str | None = None) -> dict[str, Any]:
    return {
        "kind": kind,
        "display_name": name,
        "tokens_reported": True,
        "limits_setup": None if setup is None else {"state": setup, "checked_ms": NOW},
        "sources": [],
    }


def window(
    key: str, minutes: int, used: float, resets_ms: int | None, status: str = "fresh"
) -> dict[str, Any]:
    return {
        "window_key": key,
        "used_percent": used,
        "window_minutes": minutes,
        "resets_at_ms": resets_ms,
        "status": status,
    }


def reading(
    kind: str, windows: list[dict[str, Any]], observed: int = NOW, status: str = "fresh"
) -> dict[str, Any]:
    return {
        "kind": kind,
        "quota_key": f"{kind}-quota",
        "observed_ms": observed,
        "status": status,
        "windows": windows,
    }


def doc(rows=(), limits=(), kinds=(), workspaces=(), capacity="ok") -> dict[str, Any]:
    return {
        "schema_version": 1,
        "host_id": "h1",
        "generated_ms": NOW,
        "timezone": ZONE,
        "price_table_date": None,
        "next_cursor": None,
        "rows": list(rows),
        "limits": list(limits),
        "coverage": [],
        "snapshot_labels": [],
        "kinds": list(kinds),
        "workspaces": list(workspaces),
        "capacity": capacity,
    }


def page(**kw: Any) -> model.UsagePage:
    return model.parse_usage_page(doc(**kw))


def base_page() -> model.UsagePage:
    rows = [
        row("2026-10-11", "overall", "kind-a", 100),
        row("2026-10-11", "overall", "kind-b", 30),
        row("2026-10-05", "overall", "kind-a", 50),
        row("2026-10-11", "workspace", "kind-a", 60, space="w1"),
        row("2026-10-11", "workspace", "kind-b", 30, space="w1"),
        row("2026-10-11", "workspace", "kind-a", 40, space="w2"),
        row("2026-10-05", "workspace", "kind-a", 50, space="w2"),
        row("2026-10-11", "not_linked", "kind-a", 25),
    ]
    return page(
        rows=rows,
        kinds=[kind_doc("kind-a", "Tool A"), kind_doc("kind-b", "Tool B")],
        workspaces=[
            {"workspace_id": "w1", "label": "Space One", "last_used_day": "2026-10-11"},
            {"workspace_id": "w2", "label": "Space Two", "last_used_day": "2026-10-05"},
            {"workspace_id": "w3", "label": "Space Three", "last_used_day": None},
        ],
    )


# -- one total selector, reconciliation and filters --------------------------------------------


@pytest.mark.parametrize("period", ["today", "7d", "30d", "90d"])
@pytest.mark.parametrize("provider", [s.ALL, "kind-a", "kind-b"])
def test_space_buckets_reconcile_to_the_all_spaces_total_before_rollup(
    period: str, provider: str
) -> None:
    parsed = base_page()
    flt = s.Filter(period=period, provider=provider)
    total = s.usage_of(s.filtered_rows(parsed, flt, TODAY)).tokens
    buckets = s.space_buckets(parsed, flt, TODAY)
    assert sum(u.tokens for u in buckets.values()) == total
    assert total == sum(
        r.counters.tokens
        for r in parsed.rows
        if r.scope in ("overall", "not_linked")
        and s.window_bounds(period, TODAY)[0] <= date.fromisoformat(r.day)
        and (provider == s.ALL or r.kind == provider)
    )


def test_all_spaces_is_overall_plus_not_linked_each_once() -> None:
    parsed = base_page()
    assert s.usage_of(s.filtered_rows(parsed, s.Filter("7d"), TODAY)).tokens == 100 + 30 + 50 + 25
    assert s.usage_of(s.filtered_rows(parsed, s.Filter("today"), TODAY)).tokens == 155
    assert s.usage_of(s.filtered_rows(parsed, s.Filter("7d", space="w1"), TODAY)).tokens == 90
    assert s.usage_of(s.filtered_rows(parsed, s.Filter("7d", space=s.UNLINKED), TODAY)).tokens == 25


def test_space_and_provider_filters_narrow_the_same_rows() -> None:
    parsed = base_page()
    assert (
        s.usage_of(s.filtered_rows(parsed, s.Filter("today", provider="kind-b"), TODAY)).tokens
        == 30
    )
    assert (
        s.usage_of(
            s.filtered_rows(parsed, s.Filter("today", space="w2", provider="kind-a"), TODAY)
        ).tokens
        == 40
    )


def test_an_unknown_period_is_refused() -> None:
    with pytest.raises(ValueError):
        s.filtered_rows(base_page(), s.Filter(period="1y"), TODAY)


# -- cost states -------------------------------------------------------------------------------


def _cost(rows: list[dict[str, Any]]) -> s.Cost:
    return s.usage_of(page(rows=rows, kinds=[kind_doc("kind-a", "Tool A")]).rows).cost


def test_zero_priced_partial_and_unpriced_states() -> None:
    assert _cost([row("2026-10-11", "overall", "kind-a", 0)]).state == "zero"
    priced = _cost([row("2026-10-11", "overall", "kind-a", 1_000, priced_cost=900)])
    assert (priced.state, priced.amount_micro_usd) == ("priced", 900)
    partial = _cost(
        [
            row("2026-10-11", "overall", "kind-a", 1_000, priced_cost=900, model_name="a"),
            row("2026-10-11", "overall", "kind-a", 13, unpriced=13, model_name="b"),
        ]
    )
    assert (partial.state, partial.amount_micro_usd, partial.unpriced_tokens) == (
        "partial",
        900,
        13,
    )
    unpriced = _cost([row("2026-10-11", "overall", "kind-a", 13, unpriced=13)])
    assert (unpriced.state, unpriced.amount_micro_usd) == ("unpriced", None)


def test_a_free_model_is_priced_at_zero_never_unpriced() -> None:
    free = _cost([row("2026-10-11", "overall", "kind-a", 7, priced_cost=0)])
    assert (free.state, free.amount_micro_usd) == ("priced", 0)
    assert s.money_text(free.amount_micro_usd) == "$0.00"


def test_an_older_server_without_per_event_fields_is_unknown_not_zero() -> None:
    old_priced = {
        k: v
        for k, v in row("2026-10-11", "overall", "kind-a", 50).items()
        if k not in ("priced_cost_micro_usd", "priced_tokens", "unpriced_tokens")
    }
    old_unpriced = {**old_priced, "cost_micro_usd": None}
    assert _cost([old_priced]).state == "priced"  # a number means every token was priced
    assert _cost([old_unpriced]).state == "unknown"  # a null is unknown, never zero


# -- ranking -----------------------------------------------------------------------------------


def _bucket(name: str, tokens: int) -> s.Bucket:
    return s.Bucket(key=name, name=name, usage=s.Usage(tokens, s.Cost(tokens, tokens, 0)))


def test_rank_drops_zero_orders_ties_by_name_and_skips_a_rollup_of_one() -> None:
    items = [
        _bucket("B", 60),
        _bucket("A", 60),
        _bucket("C", 30),
        _bucket("D", 0),
        _bucket("E", 10),
        _bucket("F", 5),
    ]
    ranked = s.rank(items, 4)  # 5 positive items <= 4 + 1: everything, no rollup
    assert [b.name for b in ranked.top] == ["A", "B", "C", "E", "F"]
    assert ranked.other is None and ranked.peak == 60


def test_rank_rolls_up_the_rest_only_when_more_than_n_plus_one() -> None:
    items = [_bucket(f"s{i}", 100 - i) for i in range(10)]
    ranked = s.rank(items, 4)
    assert len(ranked.top) == 4
    assert ranked.other is not None and ranked.other.count == 6
    assert ranked.other.usage.tokens == sum(100 - i for i in range(4, 10))
    assert s.share(ranked.top[0].usage.tokens, ranked.peak) == 1.0


def test_rank_regular_width_shows_eight_then_rolls_up() -> None:
    items = [_bucket(f"s{i:02d}", 1000 - i) for i in range(9)]
    assert s.rank(items, 8).other is None  # 9 <= 8 + 1
    items = [_bucket(f"s{i:02d}", 1000 - i) for i in range(10)]
    assert s.rank(items, 8).other.count == 2


def test_rank_one_item_has_no_rollup() -> None:
    ranked = s.rank([_bucket("only", 5)], 4)
    assert [b.name for b in ranked.top] == ["only"] and ranked.other is None


def test_a_rollup_with_an_unknown_part_is_unknown_not_a_partial_sum() -> None:
    unknown = s.Bucket("x", "x", s.Usage(9, s.UNKNOWN_COST))
    ranked = s.rank([_bucket(f"s{i}", 100 - i) for i in range(5)] + [unknown], 4)
    assert ranked.other is not None and ranked.other.usage.cost.state == "unknown"


# -- limits and provider status ----------------------------------------------------------------


def test_limit_windows_come_from_the_latest_reading_and_are_labelled_by_duration() -> None:
    parsed = page(
        kinds=[kind_doc("kind-a", "Tool A")],
        limits=[
            reading("kind-a", [window("secondary", 300, 99.0, NOW)], observed=NOW - 60_000),
            reading("kind-a", [window("primary", 10080, 39.0, NOW + 86_400_000)]),
        ],
    )
    [view] = s.limits(parsed, NOW)
    assert [w.label for w in view.windows] == [
        "Weekly"
    ]  # the old 5-hour window never sits beside it
    assert view.windows[0].used_percent == 39.0 and view.status == s.STATUS_OK


def test_provider_status_covers_each_case() -> None:
    later = NOW + 86_400_000
    cases = {
        s.STATUS_UNMANAGED: (kind_doc("k", "K"), None),
        s.STATUS_NOT_REPORTED: (kind_doc("k", "K"), reading("k", [], status="not_reported")),
        s.STATUS_SETUP_NEEDED: (
            kind_doc("k", "K", "script_missing"),
            reading("k", [window("p", 10080, 1, later)]),
        ),
        s.STATUS_CONFLICT: (kind_doc("k", "K", "conflict"), None),
        s.STATUS_NO_READING: (kind_doc("k", "K", "installed"), None),
        s.STATUS_STALE: (
            kind_doc("k", "K", "installed"),
            reading("k", [window("p", 10080, 1, later)], observed=NOW - 16 * 60_000),
        ),
        s.STATUS_WINDOW_ENDED: (
            kind_doc("k", "K", "installed"),
            reading("k", [window("p", 10080, 1, NOW - 1)]),
        ),
        s.STATUS_NEAR_LIMIT: (
            kind_doc("k", "K", "installed"),
            reading("k", [window("p", 10080, 85, later)]),
        ),
        s.STATUS_OK: (
            kind_doc("k", "K", "installed"),
            reading("k", [window("p", 10080, 39, later)]),
        ),
    }
    for expected, (kind, limit) in cases.items():
        parsed = model.parse_usage_page(doc(kinds=[kind], limits=[limit] if limit else []))
        assert s.providers(parsed, NOW)[0].status == expected, expected


def test_no_reading_alone_does_not_prove_setup_is_needed() -> None:
    parsed = model.parse_usage_page(doc(kinds=[kind_doc("k", "K", "not_configured")]))
    assert s.providers(parsed, NOW)[0].status == s.STATUS_UNMANAGED


def test_attention_is_blocking_then_stale_then_near_limit_and_caps_on_compact() -> None:
    later = NOW + 86_400_000
    parsed = model.parse_usage_page(
        doc(
            kinds=[
                kind_doc("a", "Alpha", "installed"),
                kind_doc("b", "Beta", "script_missing"),
                kind_doc("c", "Gamma", "installed"),
                kind_doc("d", "Delta", "conflict"),
                kind_doc("e", "Echo", "installed"),
            ],
            limits=[
                reading("a", [window("p", 10080, 90, later)]),
                reading("c", [window("p", 10080, 1, later)], observed=NOW - 16 * 60_000),
                reading("e", [], observed=NOW - 16 * 60_000),
            ],
        )
    )
    found = s.attention(parsed, NOW, compact=True)
    # Blocking first (Beta, Delta by name), then stale (Echo, Gamma by name), then near-limit.
    assert [p.kind for p in found.items] == ["b", "d", "e"]
    assert found.more == 2  # the stale Gamma and the near-limit Alpha are left for "+2 more"
    everything = s.attention(parsed, NOW, compact=False)
    assert [p.kind for p in everything.items] == ["b", "d", "e", "c", "a"]


def test_limits_ignore_period_and_space_but_respect_the_provider() -> None:
    parsed = model.parse_usage_page(
        doc(
            kinds=[kind_doc("a", "Alpha", "installed"), kind_doc("b", "Beta", "installed")],
            limits=[
                reading("a", [window("p", 10080, 39, NOW + 86_400_000)]),
                reading("b", [window("p", 10080, 10, NOW + 86_400_000)]),
            ],
        )
    )
    assert [v.kind for v in s.limits(parsed, NOW, s.Filter("today", space="w9", provider="b"))] == [
        "b"
    ]
    assert len(s.limits(parsed, NOW, s.Filter("90d"))) == 2


@pytest.mark.parametrize(
    ("minutes", "label"),
    [
        (10080, "Weekly"),
        (300, "5-hour"),
        (1440, "Daily"),
        (2880, "2-day"),
        (None, "Window"),
        (45, "45-min"),
    ],
)
def test_window_labels_come_from_the_duration(minutes: int | None, label: str) -> None:
    assert s.window_label(minutes) == label


# -- chart --------------------------------------------------------------------------------------


def test_today_is_24_hourly_buckets_with_a_zero_for_a_skipped_hour() -> None:
    parsed = page(
        rows=[
            row("2026-10-11", "overall", "kind-a", 10, hour=1),
            row("2026-10-11", "overall", "kind-a", 7, hour=3),
            row("2026-10-11", "overall", "kind-b", 5, hour=3),
        ],
        kinds=[kind_doc("kind-a", "Tool A"), kind_doc("kind-b", "Tool B")],
    )
    chart = s.chart(parsed, s.Filter("today"), TODAY)
    assert chart.grain == "hour" and len(chart.buckets) == 24 and chart.hourly_available
    assert chart.buckets[2].tokens == (0, 0)  # a DST-skipped label is a zero stub
    assert chart.buckets[3].tokens == (7, 5)
    assert chart.series == ("kind-a", "kind-b")


def test_today_without_hourly_rows_says_so_instead_of_inventing_bars() -> None:
    chart = s.chart(base_page(), s.Filter("today"), TODAY)
    assert len(chart.buckets) == 24 and chart.hourly_available is False
    assert sum(sum(b.tokens) for b in chart.buckets) == 0


@pytest.mark.parametrize(("period", "count"), [("7d", 7), ("30d", 30)])
def test_week_and_month_charts_are_daily_with_zero_stubs(period: str, count: int) -> None:
    chart = s.chart(base_page(), s.Filter(period), TODAY)
    assert chart.grain == "day" and len(chart.buckets) == count
    assert chart.buckets[-1].start == "2026-10-11"
    assert (
        sum(sum(b.tokens) for b in chart.buckets)
        == s.usage_of(
            s.filtered_rows(base_page(), s.Filter(period, provider="kind-a"), TODAY)
        ).tokens
        + s.usage_of(
            s.filtered_rows(base_page(), s.Filter(period, provider="kind-b"), TODAY)
        ).tokens
    )


def test_ninety_days_are_monday_to_sunday_weeks_that_drop_no_day() -> None:
    chart = s.chart(base_page(), s.Filter("90d"), TODAY)
    first = TODAY - timedelta(days=89)
    expected_weeks = {
        (first + timedelta(days=i)) - timedelta(days=(first + timedelta(days=i)).weekday())
        for i in range(90)
    }
    assert chart.grain == "week" and len(chart.buckets) == len(expected_weeks)
    assert chart.buckets[0].start == first.isoformat()  # a partial first week keeps its days
    for bucket in chart.buckets[1:]:
        assert date.fromisoformat(bucket.start).weekday() == 0  # later weeks start on Monday
    assert sum(sum(b.tokens) for b in chart.buckets) == sum(
        s.usage_of(s.filtered_rows(base_page(), s.Filter("90d", provider=p), TODAY)).tokens
        for p in ("kind-a", "kind-b")
    )


def test_series_are_the_top_two_providers_then_other_and_one_provider_is_one_series() -> None:
    parsed = page(
        rows=[
            row("2026-10-11", "overall", k, t)
            for k, t in (("a", 100), ("b", 80), ("c", 30), ("d", 10))
        ],
        kinds=[kind_doc(k, k.upper()) for k in "abcd"],
    )
    chart = s.chart(parsed, s.Filter("7d"), TODAY)
    assert chart.series == ("a", "b", s.CHART_OTHER)
    assert chart.series_names == ("A", "B", "Other")
    last = chart.buckets[-1].tokens
    assert last == (100, 80, 40)
    single = s.chart(parsed, s.Filter("7d", provider="c"), TODAY)
    assert single.series == ("c",) and single.buckets[-1].tokens == (30,)


# -- counts, spaces picker -----------------------------------------------------------------------


def test_counts_exclude_not_linked_and_count_every_known_space() -> None:
    counts = s.counts(base_page(), s.Filter("7d"), TODAY)
    assert (counts.active_spaces, counts.active_providers, counts.total_spaces) == (2, 2, 3)


def test_space_picker_sorts_without_inventing_activity() -> None:
    parsed = base_page()
    most = [e.space_id for e in s.spaces(parsed, s.Filter("7d"), TODAY, "most_tokens")]
    assert most == ["w1", "w2", "w3"]  # w1 90, w2 90 (tie: label A-Z), w3 zero
    assert [e.label for e in s.spaces(parsed, s.Filter("7d"), TODAY, "a_z")] == [
        "Space One",
        "Space Three",
        "Space Two",
    ]
    recent = [e.space_id for e in s.spaces(parsed, s.Filter("7d"), TODAY, "recent")]
    assert recent == ["w1", "w2", "w3"]  # 10-11, 10-05, then unknown last
    entries = {e.space_id: e for e in s.spaces(parsed, s.Filter("7d"), TODAY)}
    assert entries["w3"].last_used_day is None and "unlinked" not in entries
    with pytest.raises(ValueError):
        s.spaces(parsed, s.Filter("7d"), TODAY, "random")


# -- formatting ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("count", "text"),
    [
        (812, "812"),
        (9_400, "9.4K"),
        (18_200_000, "18.2M"),
        (741_000_000, "741.0M"),
        (116_500_000, "116.5M"),
        (3_340_000_000, "3.34B"),
        (999_960, "1.0M"),
    ],
)
def test_token_text_follows_the_compact_rules(count: int, text: str) -> None:
    assert s.tokens_text(count) == text


@pytest.mark.parametrize(
    ("count", "text"),
    [
        (310_000_000, "0.31B"),
        (2_020_000_000, "2.02B"),
        (18_200_000, "18M"),
        (9_400_000, "9.4M"),
        (940_000, "940K"),
    ],
)
def test_chart_label_text_uses_two_significant_figures(count: int, text: str) -> None:
    assert s.tokens_chart_text(count) == text


def test_money_percent_and_dates() -> None:
    assert s.money_text(543_850_000) == "$543.85"
    assert s.money_text(1_204_100_000) == "$1,204.10"
    assert s.money_text(None) == "—"
    assert s.percent_text(39.4) == "39%" and s.percent_text(80) == "80%"
    assert s.date_range(date(2026, 10, 6), date(2026, 10, 12), TODAY) == "Oct 6 – Oct 12"
    assert s.date_range(TODAY, TODAY, TODAY) == "Today · Oct 11"


def test_relative_and_reset_times() -> None:
    assert s.relative_time(NOW, NOW - 30_000, ZONE) == "just now"
    assert s.relative_time(NOW, NOW - 5 * 60_000, ZONE) == "5 min ago"
    assert s.relative_time(NOW, NOW - 3 * 3_600_000, ZONE) == "3 h ago"
    assert s.relative_time(NOW, NOW - 6 * 86_400_000, ZONE) == "Mon 12:00"  # 2026-10-05 12:00 UTC
    sunday_0658 = int(datetime(2026, 10, 11, 6, 58, tzinfo=ZoneInfo(ZONE)).timestamp() * 1000)
    assert s.reset_text(sunday_0658, ZONE) == "Resets Sun 06:58"


@pytest.mark.parametrize(
    ("name", "text"),
    [("Alpha", "Al"), ("Beta CLI", "Be"), ("OpenFoo", "Op"), ("Open Foo", "OF"), ("", "")],
)
def test_monogram_from_the_display_name(name: str, text: str) -> None:
    assert s.monogram(name) == text


# -- console card --------------------------------------------------------------------------------


def test_console_card_totals_come_from_the_shared_selector() -> None:
    parsed = page(
        rows=[
            row("2026-10-11", "overall", "kind-a", 100),
            row("2026-10-11", "not_linked", "kind-a", 25),
        ],
        kinds=[kind_doc("kind-a", "Tool A")],
    )
    card = model.console_card(parsed, NOW)
    assert card.has_today and card.tokens == 125
    assert card.cost_micro_usd == 125  # every token priced: the legacy number is present


def test_console_card_names_partial_cost_as_partial() -> None:
    parsed = page(
        rows=[
            row("2026-10-11", "overall", "kind-a", 100, priced_cost=90, model_name="a"),
            row("2026-10-11", "overall", "kind-a", 13, unpriced=13, model_name="b"),
        ],
        kinds=[kind_doc("kind-a", "Tool A")],
    )
    card = model.console_card(parsed, NOW)
    assert (
        card.cost_micro_usd is None and card.priced_micro_usd == 90 and card.unpriced_tokens == 13
    )


# -- scale: synthetic 500 spaces / 40 providers (a selector and rendering scale test, not a
# collection claim)


def test_five_hundred_spaces_and_forty_providers_stay_bounded_on_the_screen() -> None:
    rows, workspaces, kinds = (
        [],
        [],
        [kind_doc(f"k{i:02d}", f"Provider {i:02d}") for i in range(40)],
    )
    for index in range(500):
        space = f"space-{index:03d}"
        kind = f"k{index % 40:02d}"
        workspaces.append(
            {"workspace_id": space, "label": f"Space {index:03d}", "last_used_day": "2026-10-11"}
        )
        rows.append(row("2026-10-11", "workspace", kind, index + 1, space=space))
    for index in range(40):
        rows.append(row("2026-10-11", "overall", f"k{index:02d}", 0))  # zero: dropped everywhere
    rows = [r for r in rows if r["counters"]["input_uncached"] or r["scope"] == "workspace"]
    rows.append(row("2026-10-11", "not_linked", "k00", 5))
    overall = {}
    for r in rows:
        if r["scope"] == "workspace":
            overall[r["kind"]] = overall.get(r["kind"], 0) + r["counters"]["input_uncached"]
    rows += [row("2026-10-11", "overall", k, t) for k, t in overall.items()]
    parsed = page(rows=rows, kinds=kinds, workspaces=workspaces)
    flt = s.Filter("today")
    total = s.usage_of(s.filtered_rows(parsed, flt, TODAY)).tokens
    buckets = s.space_buckets(parsed, flt, TODAY)
    assert sum(u.tokens for u in buckets.values()) == total == sum(range(1, 501)) + 5
    ranked = s.rank([s.Bucket(k, k, u) for k, u in buckets.items()], s.RANK_REGULAR)
    assert (
        len(ranked.top) == 8 and ranked.other is not None and ranked.other.count == len(buckets) - 8
    )
    picker = s.spaces(parsed, flt, TODAY, "most_tokens")
    assert len(picker) == 500 and picker[0].space_id == "space-499"
    assert len(s.providers(parsed, NOW)) == 40
    assert len(s.attention(parsed, NOW, compact=True).items) <= s.COMPACT_ATTENTION
    chart = s.chart(parsed, s.Filter("7d"), TODAY)
    assert len(chart.series) == 3 and chart.series[-1] == s.CHART_OTHER and len(chart.buckets) == 7
    assert sum(sum(b.tokens) for b in chart.buckets) == total


# A reading is never picked across accounts ------------------------------------


def _plane(kind: str, account: str | None, used: float, observed: int, **extra: Any) -> dict:
    later = NOW + 86_400_000
    return {
        **reading(kind, [window("p", 10080, used, later)], observed=observed),
        "source": "provider-format",
        "account_ref": account,
        "error": None,
        **extra,
    }


def test_readings_naming_two_accounts_are_never_picked_by_timestamp() -> None:
    parsed = page(
        kinds=[kind_doc("k", "K", "installed")],
        limits=[_plane("k", "acct-a", 91.0, NOW - 10_000), _plane("k", "acct-b", 5.0, NOW)],
    )
    assert s.latest_limit(parsed, "k") is None  # ambiguous: shown as no reading, never a guess
    assert model.console_card(parsed, NOW).pick is None


def test_one_account_with_two_quotas_still_picks_the_latest_reading() -> None:
    parsed = page(
        kinds=[kind_doc("k", "K", "installed")],
        limits=[
            {**_plane("k", "acct-a", 91.0, NOW - 10_000), "quota_key": "q1"},
            {**_plane("k", "acct-a", 5.0, NOW), "quota_key": "q2"},
        ],
    )
    latest = s.latest_limit(parsed, "k")
    assert latest is not None and latest.quota_key == "q2"


def test_a_failed_reading_is_reading_unavailable_with_no_percentage() -> None:
    failed = {
        **reading("k", [], status="not_reported"),
        "source": "provider-format",
        "account_ref": None,
        "error": "auth_required",
    }
    parsed = page(kinds=[kind_doc("k", "K", "installed")], limits=[failed])
    assert s.providers(parsed, NOW)[0].status == s.STATUS_READING_UNAVAILABLE
    assert s.limits(parsed, NOW)[0].windows == ()


def test_the_status_line_reading_has_no_account_and_still_works() -> None:
    parsed = page(
        kinds=[kind_doc("k", "K", "installed")],
        limits=[reading("k", [window("p", 10080, 39, NOW + 86_400_000)])],
    )
    latest = s.latest_limit(parsed, "k")
    assert latest is not None and latest.source == "" and latest.account_ref is None
    assert s.providers(parsed, NOW)[0].status == s.STATUS_OK
