"""CREDITS-UI: credit-metered tools on the Usage page. Credits are their own meter: shown as
credits, never added to the token total or the cost, and an older server (no credit fields) shows
nothing for them. Synthetic data; tool names come only from the page."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from relaydeck.client.features.usage import model
from relaydeck.client.features.usage import screen_model as sm
from relaydeck.client.features.usage import selectors as s

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


def _token_row(kind: str, tokens: int, *, scope: str = "overall", space: str | None = None) -> Any:
    return {
        "day": "2026-10-11",
        "workspace_id": space,
        "kind": kind,
        "model": "m",
        "counters": {**EMPTY, "input_uncached": tokens},
        "sessions": 1,
        "cost_micro_usd": tokens,
        "scope": scope,
        "price_snapshot": None,
        "priced_cost_micro_usd": tokens,
        "priced_tokens": tokens,
        "unpriced_tokens": 0,
        "hour": None,
    }


def _credit_row(
    kind: str,
    milli: int,
    *,
    day: str = "2026-10-11",
    scope: str = "overall",
    space: str | None = None,
) -> Any:
    return {
        "day": day,
        "workspace_id": space,
        "kind": kind,
        "model": None,
        "counters": dict(EMPTY),
        "sessions": 1,
        "cost_micro_usd": None,
        "scope": scope,
        "price_snapshot": None,
        "priced_cost_micro_usd": 0,
        "priced_tokens": 0,
        "unpriced_tokens": 0,
        "hour": None,
        "meter": "credits",
        "credits_milli": milli,
    }


def _kind(kind: str, name: str, *, tokens: bool = True, credits: bool | None = None) -> Any:
    doc: dict[str, Any] = {
        "kind": kind,
        "display_name": name,
        "tokens_reported": tokens,
        "limits_setup": None,
        "sources": [],
    }
    if credits is not None:
        doc["credits_reported"] = credits
    return doc


def _doc(rows: list[Any], kinds: list[Any], workspaces: list[Any] | None = None) -> Any:
    return {
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
        "workspaces": workspaces or [],
        "capacity": "ok",
    }


def _build(
    doc: Any, period: str = "30d", provider: str = s.ALL, width: str = sm.WIDTH_COMPACT
) -> Any:
    return sm.build(
        model.parse_usage_page(doc),
        state=sm.STATE_READY,
        flt=s.Filter(period, provider=provider),
        now_ms=NOW,
        width=width,
    )


KINDS = [_kind("k-a", "Alpha"), _kind("k-c", "Credit Tool", tokens=False, credits=True)]


def test_a_credit_only_provider_shows_credits_never_tokens_or_dollars() -> None:
    doc = _doc([_token_row("k-a", 2_000_000), _credit_row("k-c", 162_200)], KINDS)
    result = _build(doc, width=sm.WIDTH_WIDE)
    assert result.providers is not None
    by_key = {row.key: row for row in result.providers.rows}
    assert by_key["k-c"].value == "162.2 credits"
    assert by_key["k-c"].honest is None
    assert "$" not in by_key["k-c"].value and "tokens" not in by_key["k-c"].value
    values = {kind: value for kind, _label, value in result.provider_options}
    assert values["k-c"] == "162.2 credits"


def test_credits_are_never_added_to_the_token_total_or_the_cost() -> None:
    with_credits = _doc([_token_row("k-a", 2_000_000), _credit_row("k-c", 162_200)], KINDS)
    without = _doc([_token_row("k-a", 2_000_000)], KINDS)
    a, b = _build(with_credits), _build(without)
    assert a.summary.tokens == b.summary.tokens == "2.0M"
    assert a.summary.cost == b.summary.cost
    assert a.summary.credits_line == "+ 162.2 credits (Credit Tool)"
    assert b.summary.credits_line == ""
    page = model.parse_usage_page(with_credits)
    assert all(row.kind != "k-c" for row in page.rows)  # credit rows never enter the token rows
    assert s.totals(page, s.Filter("30d"), datetime(2026, 10, 12, tzinfo=UTC).date()).tokens == (
        2_000_000
    )


def test_the_token_chart_omits_credits() -> None:
    doc = _doc([_token_row("k-a", 2_000_000), _credit_row("k-c", 162_200)], KINDS)
    result = _build(doc)
    assert result.chart is not None
    assert result.chart.title == ""  # the tokens chart
    assert all("credit" not in bar.tooltip.lower() for bar in result.chart.bars)
    assert all("Credit Tool" not in name for _role, name in result.chart.legend)


def test_a_credit_tool_filter_shows_credits_per_day() -> None:
    doc = _doc(
        [
            _credit_row("k-c", 1_500, day="2026-10-10"),
            _credit_row("k-c", 162_200, day="2026-10-11"),
        ],
        KINDS,
    )
    result = _build(doc, provider="k-c")
    chart = result.chart
    assert chart is not None and chart.title == "Daily credits"
    assert chart.meta.startswith("Peak 162.2")
    assert any("credits" in bar.tooltip for bar in chart.bars if bar.value)
    assert all("tokens" not in bar.tooltip for bar in chart.bars)
    assert result.summary.tokens == "0"


def test_mixed_tokens_and_credits_in_one_space() -> None:
    doc = _doc(
        [
            _token_row("k-a", 1_200_000_000, scope="workspace", space="sp-1"),
            _credit_row("k-c", 42_000, scope="workspace", space="sp-1"),
        ],
        KINDS,
        workspaces=[{"workspace_id": "sp-1", "label": "Space One", "last_used_day": "2026-10-11"}],
    )
    page = model.parse_usage_page(doc)
    today = datetime(2026, 10, 12, tzinfo=UTC).date()
    assert s.credit_space_buckets(page, s.Filter("30d"), today) == {"sp-1": 42_000}
    assert s.space_buckets(page, s.Filter("30d"), today)["sp-1"].tokens == 1_200_000_000


def test_a_space_row_carries_its_credits_beside_the_tokens() -> None:
    doc = _doc(
        [
            _token_row("k-a", 1_200_000_000, scope="workspace", space="sp-1"),
            _token_row("k-a", 1_200_000_000),
            _credit_row("k-c", 42_000, scope="workspace", space="sp-1"),
        ],
        KINDS,
        workspaces=[{"workspace_id": "sp-1", "label": "Space One", "last_used_day": "2026-10-11"}],
    )
    result = _build(doc)
    assert result.spaces is not None
    row = next(r for r in result.spaces.rows if r.key == "sp-1")
    assert row.value.endswith("tokens") or "B" in row.value
    assert row.note == "42.0 credits"


def test_an_old_server_with_no_credit_fields_shows_nothing_for_credits() -> None:
    kinds = [_kind("k-a", "Alpha"), _kind("k-b", "Beta", tokens=False)]  # no credits_reported
    doc = _doc([_token_row("k-a", 2_000_000)], kinds)
    page = model.parse_usage_page(doc)
    assert page.credits == () and not any(k.credits_reported for k in page.kinds)
    result = _build(doc)
    assert result.summary.credits_line == ""
    assert result.providers is not None
    assert not any("credit" in row.value for row in result.providers.rows)
    assert result.providers is not None
    beta = next(row for row in result.providers.rows if row.key == "k-b")
    assert beta.honest == "Tokens not reported"  # the old wording stays


def test_the_caveat_names_a_credit_tool_as_reporting_credits_not_tokens() -> None:
    doc = _doc([_token_row("k-a", 2_000_000), _credit_row("k-c", 162_200)], KINDS)
    result = _build(doc)
    assert "Credit Tool (reports credits, not tokens)" in result.caveat
    assert "no token counts" not in result.caveat


def test_the_providers_screen_says_credits_used_remaining_not_reported() -> None:
    doc = _doc([_token_row("k-a", 2_000_000), _credit_row("k-c", 162_200)], KINDS)
    result = _build(doc)
    line = next(item for item in result.connected if item.kind == "k-c")
    assert line.status == "Credits used · remaining not reported"
    quiet = _build(_doc([_token_row("k-a", 2_000_000)], KINDS))
    line = next(item for item in quiet.connected if item.kind == "k-c")
    assert "remaining not reported" in line.status


def test_credit_formatting_one_decimal_and_thousands_separators() -> None:
    assert s.credits_text(162_200) == "162.2"
    assert s.credits_text(1_234_560) == "1,234.6"
    assert s.credits_text(0) == "0.0"
    assert s.credits_chart_text(12_300_000) == "12.3K"


def test_a_credit_only_period_is_not_called_empty() -> None:
    doc = _doc([_credit_row("k-c", 5_000)], KINDS)
    assert _build(doc).empty is False
    assert _build(_doc([], KINDS)).empty is True
