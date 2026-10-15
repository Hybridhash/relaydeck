"""The hourly transport, the capacity result, recency under a space filter,
source-aware setup status, and the latest window set through the real storage shape. Synthetic data
only."""

from __future__ import annotations

import asyncio
import json
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest
from starlette.datastructures import QueryParams

from relaydeck.client.features.sync.transport import HttpxGatewayTransport
from relaydeck.client.features.usage import loop, model
from relaydeck.client.features.usage import selectors as s
from relaydeck.client.features.usage.api import UsageFetchError, UsageQuery, fetch_usage
from relaydeck.client.features.usage.source import gateway_usage_fetch
from relaydeck.gateway.usage import UsageService, parse_query
from relaydeck.usage.contracts import (
    Basis,
    Batch,
    Counters,
    Coverage,
    LimitSnapshot,
    LimitWindow,
    SessionMapping,
    UsageEvent,
)
from relaydeck.usage.contracts import (
    UsageQuery as StoreQuery,
)
from relaydeck.usage.store import SqliteUsageStore

ZONE = "UTC"
NOW = int(datetime(2026, 10, 11, 12, tzinfo=ZoneInfo(ZONE)).timestamp() * 1000)
TODAY = date(2026, 10, 11)
DAY = 86_400_000
EMPTY = {
    "input_uncached": 0,
    "cache_read": 0,
    "cache_write_short": 0,
    "cache_write_long": 0,
    "cache_write_unknown": 0,
    "output": 0,
    "reasoning": 0,
}


def row(
    scope: str,
    kind: str,
    tokens: int,
    *,
    hour: int | None = None,
    space: str | None = None,
    day: str = "2026-10-11",
) -> dict[str, Any]:
    return {
        "day": day,
        "workspace_id": space if scope == "workspace" else None,
        "kind": kind,
        "model": "model-a",
        "counters": {**EMPTY, "input_uncached": tokens},
        "sessions": 1,
        "cost_micro_usd": tokens,
        "scope": scope,
        "price_snapshot": None,
        "priced_cost_micro_usd": tokens,
        "priced_tokens": tokens,
        "unpriced_tokens": 0,
        "hour": hour,
    }


def kind(
    name: str, sources: list[dict[str, Any]] | None = None, legacy: str | None = None
) -> dict[str, Any]:
    return {
        "kind": name,
        "display_name": name.upper(),
        "tokens_reported": True,
        "limits_setup": None if legacy is None else {"state": legacy, "checked_ms": NOW},
        "sources": sources or [],
    }


def doc(
    rows=(), kinds=(), limits=(), workspaces=(), capacity="ok", next_cursor=None
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "host_id": "h1",
        "generated_ms": NOW,
        "timezone": ZONE,
        "price_table_date": None,
        "next_cursor": next_cursor,
        "rows": list(rows),
        "limits": list(limits),
        "coverage": [],
        "snapshot_labels": [],
        "kinds": list(kinds),
        "workspaces": list(workspaces),
        "capacity": capacity,
    }


def window(key: str, minutes: int, used: float, resets: int | None) -> dict[str, Any]:
    return {
        "window_key": key,
        "used_percent": used,
        "window_minutes": minutes,
        "resets_at_ms": resets,
        "status": "fresh",
    }


def reading(name: str, windows: list[dict[str, Any]], observed: int = NOW) -> dict[str, Any]:
    return {
        "kind": name,
        "quota_key": f"{name}-q",
        "observed_ms": observed,
        "status": "fresh",
        "windows": windows,
    }


# -- point 2: hourly transport, query identity, page check -----------------------------------------


def test_the_hour_is_part_of_the_query_identity_and_needs_one_day() -> None:
    daily = UsageQuery(host_id="h1", days=1)
    hourly = UsageQuery(host_id="h1", days=1, hour=True)
    assert daily.identity != hourly.identity
    assert (
        UsageQuery(host_id="h1", start="2026-10-11", end="2026-10-11", hour=True).identity[-1]
        is True
    )
    with pytest.raises(ValueError):
        UsageQuery(host_id="h1", days=7, hour=True)
    with pytest.raises(ValueError):
        UsageQuery(host_id="h1", start="2026-10-10", end="2026-10-11", hour=True)


def test_a_daily_page_never_answers_an_hourly_request_and_the_reverse() -> None:
    async def scenario(query: UsageQuery, rows: list[dict[str, Any]]) -> None:
        async def fetch(q: UsageQuery) -> dict[str, Any]:
            return doc(rows=rows, kinds=[kind("kind-a")])

        with pytest.raises(UsageFetchError, match="bucket_mismatch"):
            await fetch_usage(fetch, query, is_current=lambda: True)

    asyncio.run(scenario(UsageQuery("h1", days=1, hour=True), [row("overall", "kind-a", 5)]))
    asyncio.run(scenario(UsageQuery("h1", days=1), [row("overall", "kind-a", 5, hour=3)]))


def test_transport_to_selectors_to_chart_buckets_keeps_the_hour_and_the_cursor() -> None:
    seen: list[dict[str, str]] = []
    hourly_rows = [
        row("overall", "kind-a", 10, hour=1),
        row("overall", "kind-a", 7, hour=3),
        row("overall", "kind-b", 5, hour=3),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        seen.append(params)
        if "cursor" not in params:
            return httpx.Response(
                200,
                json=doc(
                    rows=hourly_rows[:1],
                    kinds=[kind("kind-a"), kind("kind-b")],
                    next_cursor="page-2",
                ),
            )
        return httpx.Response(
            200, json=doc(rows=hourly_rows[1:], kinds=[kind("kind-a"), kind("kind-b")])
        )

    async def scenario() -> s.Chart:
        async def token() -> str:
            return "synthetic-token"

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            transport = HttpxGatewayTransport("http://gw", token, client)
            fetch = gateway_usage_fetch(transport)
            page = await fetch_usage(
                fetch, UsageQuery("h1", days=1, hour=True), is_current=lambda: True
            )
        return s.chart(page, s.Filter("today"), TODAY)

    chart = asyncio.run(scenario())
    assert all(params["bucket"] == "hour" and params["days"] == "1" for params in seen)
    assert "cursor" in seen[1] and seen[1]["cursor"] == "page-2"  # pagination keeps the hour
    assert chart.grain == "hour" and chart.hourly_available and len(chart.buckets) == 24
    assert chart.buckets[3].tokens == (7, 5)
    assert chart.buckets[2].tokens == (0, 0)


def test_a_daily_request_sends_no_bucket_parameter() -> None:
    seen: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(dict(request.url.params))
        return httpx.Response(200, json=doc(kinds=[kind("kind-a")]))

    async def scenario() -> None:
        async def token() -> str:
            return "synthetic-token"

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await fetch_usage(
                gateway_usage_fetch(HttpxGatewayTransport("http://gw", token, client)),
                UsageQuery("h1", days=1),
                is_current=lambda: True,
            )

    asyncio.run(scenario())
    assert "bucket" not in seen[0]


# -- point 3: capacity is unavailable, never zero ----


def exceeded_page() -> s.model.UsagePage:
    return model.parse_usage_page(
        doc(
            rows=[],
            kinds=[kind("kind-a")],
            workspaces=[{"workspace_id": "w1", "label": "One", "last_used_day": "2026-10-11"}],
            capacity="exceeded",
        )
    )


def test_exceeded_reproduces_no_zero_usage_on_any_selector() -> None:
    page = exceeded_page()
    flt = s.Filter("today")
    total = s.totals(page, flt, TODAY)
    assert total.tokens is None and total.cost.state == "unknown" and not total.available
    assert s.space_buckets(page, flt, TODAY) == {}
    entries = s.spaces(page, flt, TODAY)
    assert [e.usage.tokens for e in entries] == [None]
    counts = s.counts(page, flt, TODAY)
    assert (
        counts.active_spaces is None
        and counts.active_providers is None
        and counts.total_spaces == 1
    )
    chart = s.chart(page, flt, TODAY)
    assert chart.available is False and chart.buckets == ()
    provider = s.providers(page, NOW, flt)
    assert provider and provider[0].usage is not None and provider[0].usage.tokens is None
    assert s.rank([s.Bucket("w1", "One", total)], 4).top == ()


def test_console_card_for_an_exceeded_report_is_unavailable_with_no_money_or_usage() -> None:
    card = model.console_card(exceeded_page(), NOW)
    assert card.available is False
    assert card.has_today is False and card.cost_micro_usd is None and card.priced_micro_usd is None
    assert card.tokens == 0 and card.unpriced_tokens == 0


def test_the_feed_never_exposes_an_exceeded_page_as_data() -> None:
    feed = loop.UsageFeed()
    key = ("acct", "h1", 1, "")
    generation = feed.set_key(key)
    assert feed.accept(generation, exceeded_page(), NOW) is True
    state = feed.visible(key, active=True)
    assert state.page is None and state.status == loop.STATUS_UNAVAILABLE
    assert state.error == "capacity_exceeded"


# -- point 4: recency under a space filter (store level) -------------------------------------------


def test_recency_is_read_before_the_space_filter_and_keeps_every_space(tmp_path: Path) -> None:
    now = NOW
    store = SqliteUsageStore(tmp_path / "state/usage.db", ZONE, clock=lambda: now)
    try:
        cursor = store.cursor("h1/kind-a/format-a")
        events = (
            UsageEvent(
                "s1", "e1", NOW - 3600_000, "model-a", Counters(input_uncached=10), Basis.PER_CALL
            ),
            UsageEvent(
                "s2", "e2", NOW - 2 * DAY, "model-a", Counters(input_uncached=4), Basis.PER_CALL
            ),
        )
        maps = (
            SessionMapping("h1/kind-a/format-a", "kind-a", "s1", "i1", "h1", "w1", 0, 2**62),
            SessionMapping("h1/kind-a/format-a", "kind-a", "s2", "i2", "h1", "w2", 0, 2**62),
        )
        store.apply(
            "kind-a",
            Batch(events, cursor, Coverage.COMPLETE, cursor.revision, ()),
            maps,
            source_key="h1/kind-a/format-a",
        )
        filtered = store.query(StoreQuery("h1", "2026-10-05", "2026-10-11", space_id="w2"), now)
        assert {r.workspace_id for r in filtered.rows if r.scope == "workspace"} == {"w2"}
        assert dict(filtered.workspace_days) == {"w1": "2026-10-11", "w2": "2026-10-09"}
    finally:
        store.close()


# -- point 5: source-aware setup status, both orders ----


@pytest.mark.parametrize("order", [("conflict", "installed"), ("installed", "conflict")])
def test_a_conflict_source_wins_whatever_the_source_order(order: tuple[str, str]) -> None:
    sources = [
        {"format": f"fmt-{i}", "limits_setup": {"state": st, "checked_ms": NOW}}
        for i, st in enumerate(order)
    ]
    parsed = model.parse_usage_page(
        doc(
            kinds=[kind("kind-a", sources=sources, legacy="installed")],
            limits=[reading("kind-a", [window("p", 10080, 10, NOW + DAY)])],
        )
    )
    assert s.providers(parsed, NOW)[0].status == s.STATUS_CONFLICT


@pytest.mark.parametrize(
    "order", [("script_missing", "installed"), ("installed", "script_missing")]
)
def test_a_missing_hook_source_wins_whatever_the_order(order: tuple[str, str]) -> None:
    sources = [
        {"format": f"fmt-{i}", "limits_setup": {"state": st, "checked_ms": NOW}}
        for i, st in enumerate(order)
    ]
    parsed = model.parse_usage_page(
        doc(
            kinds=[kind("kind-a", sources=sources)],
            limits=[reading("kind-a", [window("p", 10080, 10, NOW + DAY)])],
        )
    )
    assert s.providers(parsed, NOW)[0].status == s.STATUS_SETUP_NEEDED


def test_the_legacy_field_is_used_only_when_sources_are_absent() -> None:
    fresh = [reading("kind-a", [window("p", 10080, 10, NOW + DAY)])]
    legacy_only = model.parse_usage_page(
        doc(kinds=[kind("kind-a", legacy="conflict")], limits=fresh)
    )
    assert s.providers(legacy_only, NOW)[0].status == s.STATUS_CONFLICT
    with_sources = model.parse_usage_page(
        doc(
            kinds=[
                kind(
                    "kind-a",
                    sources=[
                        {"format": "f", "limits_setup": {"state": "installed", "checked_ms": NOW}}
                    ],
                    legacy="conflict",
                )
            ],
            limits=fresh,
        )
    )
    assert s.providers(with_sources, NOW)[0].status == s.STATUS_OK


# -- point 6: latest window set through the real storage shape -------------------------------------


def _service(store: SqliteUsageStore, state_dir: Path) -> UsageService:
    source = SimpleNamespace(
        kind="kind-a",
        format="jsonl-message-usage-v1",
        display_name="Kind A",
        limits_settings=None,
        limits_cache=None,
    )
    service = UsageService(
        store,
        SimpleNamespace(sources=(source,), timezone=ZONE, inferred_linking=True),
        "h1",
        state_dir,
        lambda: NOW,
        workspace_labels=lambda: {},
    )
    service._checks = {
        ("kind-a", "jsonl-message-usage-v1"): SimpleNamespace(check=lambda now: None)
    }
    return service


def _limits_write(store: SqliteUsageStore, snapshot: LimitSnapshot) -> None:
    cursor = store.cursor("h1/kind-a/format-a")
    store.apply(
        "kind-a",
        Batch((), cursor, Coverage.COMPLETE, cursor.revision, (snapshot,)),
        (),
        source_key="h1/kind-a/format-a",
    )


def _through_the_wire(store: SqliteUsageStore, state_dir: Path) -> s.model.UsagePage:
    """The gateway's body for a 7-day report, serialised as JSON and parsed by the client."""
    query, _selector = parse_query(
        SimpleNamespace(query_params=QueryParams("days=7")), "h1", ZONE, NOW
    )
    body = _service(store, state_dir)._report(query, NOW)
    return model.parse_usage_page(json.loads(json.dumps(body, default=str)))


def test_a_newer_reading_never_shows_an_older_window_store_to_wire_to_selector(
    tmp_path: Path,
) -> None:
    store = SqliteUsageStore(tmp_path / "state/usage.db", ZONE, clock=lambda: NOW)
    try:
        _limits_write(
            store,
            LimitSnapshot(
                "kind-a", "q1", NOW - 2 * DAY, (LimitWindow("secondary", 99.0, 300, NOW - 1000),)
            ),
        )
        _limits_write(
            store,
            LimitSnapshot(
                "kind-a", "q1", NOW - 60_000, (LimitWindow("primary", 39.0, 10080, NOW + 5 * DAY),)
            ),
        )
        page = _through_the_wire(store, tmp_path)
        [view] = s.limits(page, NOW)
        assert [w.label for w in view.windows] == ["Weekly"]
        assert [w.key for w in view.windows] == ["primary"]
    finally:
        store.close()


def test_a_newer_reading_with_no_windows_replaces_the_older_set_entirely(tmp_path: Path) -> None:
    store = SqliteUsageStore(tmp_path / "state/usage.db", ZONE, clock=lambda: NOW)
    try:
        _limits_write(
            store,
            LimitSnapshot(
                "kind-a", "q1", NOW - 2 * DAY, (LimitWindow("primary", 50.0, 10080, NOW + 5 * DAY),)
            ),
        )
        _limits_write(store, LimitSnapshot("kind-a", "q1", NOW - 60_000, ()))
        page = _through_the_wire(store, tmp_path)
        [view] = s.limits(page, NOW)
        assert view.windows == ()
    finally:
        store.close()
