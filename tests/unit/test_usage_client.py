"""Usage client: model rules, pagination and the refresh lifecycle.

No Flet and no network.
"""

from __future__ import annotations

import asyncio
import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from relaydeck.client.features.usage import api, loop, model

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "usage" / "client"
TODAY = date(2026, 10, 11)
NOW_MS = 1791700000000


def fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text())


def page(name: str = "page-overall.json") -> model.UsagePage:
    return model.parse_usage_page(fixture(name))


# -- model: parsing ------------------------------------------------------------------------------


def test_a_valid_page_parses_and_the_setup_block_is_optional() -> None:
    parsed = page()
    assert parsed.host_id == "host-a" and len(parsed.rows) == 4
    kinds = {k.kind: k for k in parsed.kinds}
    assert kinds["kind-b"].setup is None  # no managed hook: unmanaged, not broken
    assert kinds["kind-a"].setup == model.LimitsSetup("installed", 1791699900000)


@pytest.mark.parametrize("value", [None, NOW_MS, True, -1, "recent"])
def test_asof_timestamp_is_optional_and_strict(value) -> None:
    doc = {**fixture("page-overall.json"), "as_of_ms": value}
    if value is None or type(value) is int and value >= 0:
        assert model.parse_usage_page(doc).as_of_ms == value
    else:
        with pytest.raises(model.UsageDocError):
            model.parse_usage_page(doc)


def test_cursor_pages_must_keep_the_same_asof() -> None:
    async def fetch(query):
        name = "page-second.json" if query.cursor else "page-first.json"
        return {**fixture(name), "as_of_ms": NOW_MS + int(bool(query.cursor))}

    with pytest.raises(api.UsageFetchError, match="inconsistent_pages"):
        asyncio.run(
            api.fetch_usage(fetch, api.UsageQuery("host-a", days=7), is_current=lambda: True)
        )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(schema_version=2),
        lambda d: d["rows"][0]["counters"].update(output=True),  # a bool is not a count
        lambda d: d["rows"][0].update(scope="everyone"),
        lambda d: d["limits"][0]["windows"][0].update(used_percent=140.0),
        lambda d: d["kinds"][0].update(limits_setup={"state": "maybe", "checked_ms": None}),
        lambda d: d["rows"][0].update(cost_micro_usd=-1),
    ],
)
def test_a_document_that_breaks_the_contract_is_rejected(mutate: Any) -> None:
    doc = fixture("page-overall.json")
    mutate(doc)
    with pytest.raises(model.UsageDocError):
        model.parse_usage_page(doc)


# -- model: counters -----------------------------------------------------------------------------


def test_reasoning_is_inside_output_and_not_added_to_tokens() -> None:
    counters = model.Counters(input_uncached=10, output=5, reasoning=4)
    assert counters.tokens == 15


# -- model: limits and the Console card ----------------------------------------------------------


def test_console_picks_the_highest_used_fresh_window_with_its_own_reset() -> None:
    card = model.console_card(page(), NOW_MS)
    assert card.has_today and card.cost_micro_usd is None  # kind-b unpriced today: unavailable
    assert card.pick is not None
    assert card.pick.window_key == "secondary" and card.pick.used_percent == 88.0
    assert card.pick.resets_at_ms == 1791900000000  # the reset of that same window


def test_console_ties_go_to_the_earliest_reset_and_expired_windows_never_win() -> None:
    doc = fixture("page-overall.json")
    doc["limits"][0]["windows"] = [
        {
            "window_key": "primary",
            "used_percent": 70.0,
            "window_minutes": 300,
            "resets_at_ms": 1791900000000,
            "status": "fresh",
        },
        {
            "window_key": "secondary",
            "used_percent": 70.0,
            "window_minutes": 10080,
            "resets_at_ms": 1791800000000,
            "status": "fresh",
        },
    ]
    doc["limits"][1]["windows"] = [
        {
            "window_key": "primary",
            "used_percent": 99.0,
            "window_minutes": 300,
            "resets_at_ms": 1791610000000,
            "status": "expired",
        }
    ]
    card = model.console_card(model.parse_usage_page(doc), NOW_MS)
    assert card.pick is not None and card.pick.window_key == "secondary"
    assert card.pick.used_percent == 70.0


def test_no_fresh_window_means_no_console_pick() -> None:
    card = model.console_card(page("page-no-limits.json"), NOW_MS)
    assert card.pick is None


# -- model: host day and timezone ----------------------------------------------------------------


def test_today_is_the_host_day_not_the_device_day() -> None:
    doc = fixture("page-overall.json")
    doc["timezone"] = "Pacific/Auckland"  # 10:00 UTC is already in Auckland
    doc["generated_ms"] = 1791745200000  # 23:00 UTC is in Auckland
    assert model.page_today(model.parse_usage_page(doc)) == date(2026, 10, 12)
    assert model.page_today(page()) == TODAY


def test_an_unknown_host_timezone_is_rejected() -> None:
    doc = fixture("page-overall.json")
    doc["timezone"] = "Not/AZone"
    with pytest.raises(model.UsageDocError):
        model.page_today(model.parse_usage_page(doc))


# -- api: request paths and pagination -----------------------------------------------------------


def test_a_query_is_days_or_an_explicit_range_and_days_are_bounded() -> None:
    assert api.UsageQuery("host-a", days=90).days == 90
    assert api.UsageQuery("host-a", start="2026-10-01", end="2026-10-11").days is None
    with pytest.raises(ValueError):
        api.UsageQuery("host-a", days=7, start="2026-10-01", end="2026-10-11")
    with pytest.raises(ValueError):
        api.UsageQuery("host-a")
    with pytest.raises(ValueError):
        api.UsageQuery("host-a", days=91)


async def _always_current() -> bool:
    return True


def _pages(name_by_cursor: dict[str | None, str], calls: list[Any]) -> Any:
    async def fetch(query: api.UsageQuery) -> dict[str, Any]:
        calls.append(query)
        return fixture(name_by_cursor[query.cursor])

    return fetch


def test_every_page_is_consumed_before_the_totals_are_published() -> None:
    calls: list[Any] = []
    query = api.UsageQuery("host-a", days=7)
    get = _pages({None: "page-first.json", "cursor-2": "page-second.json"}, calls)
    merged = asyncio.run(api.fetch_usage(get, query, is_current=lambda: True))
    assert len(calls) == 2 and merged.next_cursor is None
    assert len(merged.rows) == 4  # rows from both pages, one complete report
    assert sum(1 for r in merged.rows if r.scope == "overall") == 2


def test_a_superseded_request_makes_no_further_calls() -> None:
    calls: list[Any] = []
    state = {"current": True}
    get = _pages({None: "page-first.json", "cursor-2": "page-second.json"}, calls)

    def current() -> bool:
        return state["current"]

    async def run() -> None:
        state["current"] = True

        async def flip(query: api.UsageQuery) -> dict[str, Any]:
            state["current"] = False  # the host changed while the first page was in flight
            return await get(query)

        with pytest.raises(api.StaleUsageRequest):
            await api.fetch_usage(flip, api.UsageQuery("host-a", days=7), is_current=current)

    asyncio.run(run())
    assert len(calls) == 1


def test_an_unfinished_report_is_never_returned_as_complete() -> None:
    calls: list[Any] = []
    get = _pages({None: "page-first.json", "cursor-2": "page-first.json"}, calls)
    with pytest.raises(api.UsageFetchError, match="too_many_pages"):
        asyncio.run(
            api.fetch_usage(
                get,
                api.UsageQuery("host-a", days=7),
                is_current=lambda: True,
                max_pages=2,
            )
        )


def test_a_malformed_page_is_a_safe_fetch_error() -> None:
    async def get_json(query: api.UsageQuery) -> dict[str, Any]:
        return {"schema_version": 1, "rows": "not-a-list"}

    with pytest.raises(api.UsageFetchError) as caught:
        asyncio.run(
            api.fetch_usage(
                get_json,
                api.UsageQuery("host-a", days=7),
                is_current=lambda: True,
            )
        )
    assert caught.value.code == "malformed_page" and "rows" not in str(caught.value)


def test_pages_of_different_snapshots_are_not_merged() -> None:
    second = fixture("page-second.json")
    second["generated_ms"] = 1

    async def get_mismatched(query: api.UsageQuery) -> dict[str, Any]:
        return second if query.cursor else fixture("page-first.json")

    with pytest.raises(api.UsageFetchError, match="inconsistent_pages"):
        asyncio.run(
            api.fetch_usage(
                get_mismatched,
                api.UsageQuery("host-a", days=7),
                is_current=lambda: True,
            )
        )


def test_gateway_refusals_pass_through_with_their_safe_code() -> None:
    async def get_json(query: api.UsageQuery) -> dict[str, Any]:
        raise api.UsageHttpError(404, "usage_disabled")

    with pytest.raises(api.UsageHttpError) as caught:
        asyncio.run(
            api.fetch_usage(
                get_json,
                api.UsageQuery("host-a", days=7),
                is_current=lambda: True,
            )
        )
    assert caught.value.code == "usage_disabled"


# -- loop: feed generations, last good data, lifecycle --------------------------------------------


def test_a_result_from_an_old_generation_is_discarded() -> None:
    feed = loop.UsageFeed()
    first = feed.set_key(("host-a", "7d"))
    feed.begin()
    feed.set_key(("host-b", "7d"))  # the host changed while the request was in flight
    assert feed.accept(first, page(), NOW_MS) is False
    assert feed.state.page is None and feed.state.status == loop.STATUS_LOADING


def test_a_failure_keeps_the_last_good_page_with_its_error() -> None:
    feed = loop.UsageFeed()
    generation = feed.set_key(("host-a", "7d"))
    feed.accept(generation, page(), NOW_MS)
    feed.fail(generation, "timeout")
    assert feed.state.status == loop.STATUS_ERROR
    assert feed.state.page is not None and feed.state.error == "timeout"
    assert feed.state.observed_ms == NOW_MS


def test_a_failure_before_any_page_is_unavailable_and_disabled_is_its_own_state() -> None:
    feed = loop.UsageFeed()
    generation = feed.set_key(("host-a", "7d"))
    feed.fail(generation, "malformed_page")  # a definitive error is unavailable at once
    assert feed.state.status == loop.STATUS_UNAVAILABLE
    feed.fail(generation, "usage_disabled", disabled=True)
    assert feed.state.status == loop.STATUS_DISABLED and feed.state.page is None


def test_hiding_the_page_makes_in_flight_results_stale() -> None:
    feed = loop.UsageFeed()
    generation = feed.set_key(("host-a", "7d"))
    feed.stop()
    assert feed.accept(generation, page(), NOW_MS) is False


def test_the_loop_refreshes_without_overlap_and_waits_the_interval() -> None:
    events: list[str] = []
    state = {"active": True, "inflight": 0, "refreshes": 0}

    async def refresh() -> None:
        assert state["inflight"] == 0  # never two requests at once
        state["inflight"] += 1
        state["refreshes"] += 1
        events.append("refresh")
        await asyncio.sleep(0)
        state["inflight"] -= 1
        if state["refreshes"] == 3:
            state["active"] = False

    async def sleep(seconds: float) -> None:
        events.append(f"sleep {seconds}")

    asyncio.run(loop.run_usage_loop(active=lambda: state["active"], refresh=refresh, sleep=sleep))
    assert events == ["refresh", "sleep 5.0", "refresh", "sleep 5.0", "refresh"]


def test_the_loop_stops_at_once_when_hidden_and_resumes_by_refreshing_first() -> None:
    calls: list[Any] = []

    async def refresh() -> None:
        calls.append("refresh")

    async def sleep(seconds: float) -> None:
        calls.append("sleep")

    asyncio.run(loop.run_usage_loop(active=lambda: False, refresh=refresh, sleep=sleep))
    assert calls == []  # hidden: nothing runs
    asyncio.run(
        loop.run_usage_loop(
            active=lambda: len(calls) < 2,
            refresh=refresh,
            sleep=sleep,
        )
    )
    assert calls[0] == "refresh"  # resume fetches immediately


def test_cancelling_the_loop_stops_it_during_the_wait() -> None:
    async def refresh() -> None:
        return None

    async def run() -> None:
        task = asyncio.create_task(loop.run_usage_loop(active=lambda: True, refresh=refresh))
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())


# -- transport and gateway adapter (addendum E) ----------------------------------------------------


def _transport(handler: Any) -> Any:
    import httpx

    from relaydeck.client.features.sync.transport import HttpxGatewayTransport

    async def token() -> str:
        return "token-a"

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return HttpxGatewayTransport(base_url="https://gateway.test", token=token, client=client)


def test_the_transport_sends_days_and_returns_the_gateway_document() -> None:
    import httpx

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=fixture("page-overall.json"))

    transport = _transport(handler)
    document = asyncio.run(transport.usage("host-a", days=7, space_id="space-a"))
    assert document["host_id"] == "host-a"
    url = seen[0].url
    assert url.path == "/v1/hosts/host-a/usage"
    assert url.params["days"] == "7" and url.params["space_id"] == "space-a"
    assert "from" not in url.params  # days and an explicit range are never sent together


def test_a_refused_usage_request_keeps_its_safe_code_for_the_feed() -> None:
    import httpx

    from relaydeck.client.features.usage.api import UsageHttpError
    from relaydeck.client.features.usage.source import gateway_usage_fetch

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "usage_disabled"})

    fetch = gateway_usage_fetch(_transport(handler))
    with pytest.raises(UsageHttpError) as caught:
        asyncio.run(fetch(api.UsageQuery("host-a", days=7)))
    assert caught.value.code == "usage_disabled"


def test_the_base_transport_reports_usage_unavailable_without_a_network() -> None:
    from relaydeck.client.features.sync.transport import GatewayTransport, TransportError

    class Minimal(GatewayTransport):  # type: ignore[misc]
        async def me(self) -> dict[str, Any]:
            return {}

        async def hosts(self) -> dict[str, Any]:
            return {}

        async def snapshot(self, host_id: str) -> dict[str, Any]:
            return {}

        async def agents(self, host_id: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
            return {}

        def stream(self, host_id: str, cursor: str) -> Any:  # pragma: no cover
            raise NotImplementedError

        async def submit_command(self, command: dict[str, Any]) -> dict[str, Any]:
            return {}

        async def command_status(self, host_id: str, command_id: str) -> Any:
            return None

        async def terminal_ticket(self, lease_id: str) -> dict[str, Any]:
            return {}

        async def create_attachment(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            return {}

        async def create_lease(self, attachment_id: str, *, mode: str) -> dict[str, Any]:
            return {}

        async def renew_lease(self, lease_id: str) -> dict[str, Any]:
            return {}

    with pytest.raises(TransportError) as caught:
        asyncio.run(Minimal().usage("host-a", days=1))
    assert caught.value.code == "usage_unavailable"


# -- cache identity, selected scope, render-time freshness, Console, footer


MINUTE = 60_000


def test_a_host_switch_with_a_failing_fetch_shows_no_data_from_the_old_host() -> None:
    feed = loop.UsageFeed()
    first = feed.set_key(("acct", "host-a", 7, ""))
    feed.accept(first, page(), NOW_MS)
    second = feed.set_key(("acct", "host-b", 7, ""))  # the host changed
    assert feed.state.page is None  # host-A data is not interpreted under host B
    feed.fail(second, "malformed_page")
    assert feed.state.page is None and feed.state.status == loop.STATUS_UNAVAILABLE


def test_the_same_context_keeps_its_last_good_page_and_another_account_clears_it() -> None:
    feed = loop.UsageFeed()
    generation = feed.set_key(("acct-1", "host-a", 7, ""))
    feed.accept(generation, page(), NOW_MS)
    assert feed.set_key(("acct-1", "host-a", 7, "")) == generation
    assert feed.state.page is not None
    feed.set_key(("acct-2", "host-a", 7, ""))
    assert feed.state.page is None


def test_going_inactive_keeps_the_same_context_page_but_drops_any_in_flight_result() -> None:
    # Polling eligibility is separate from display. The same context's last page stays
    # (it is shown with its age); an in-flight response from before the stop is obsolete.
    key = ("acct", "host-a", 7, "")
    feed = loop.UsageFeed()
    generation = feed.set_key(key)
    feed.accept(generation, page(), NOW_MS)
    feed.stop()
    assert feed.visible(key, active=False).page is not None
    assert feed.accept(generation, page(), NOW_MS) is False


def test_a_context_change_drops_the_page_even_when_it_comes_back_later() -> None:
    feed = loop.UsageFeed()
    first = ("acct", "host-a", 7, "")
    generation = feed.set_key(first)
    feed.accept(generation, page(), NOW_MS)
    assert feed.visible(("acct", "host-b", 7, ""), active=True).page is None
    feed.set_key(("acct", "host-b", 7, ""))
    assert feed.visible(first, active=True).page is None


def test_console_picks_a_fresh_weekly_window_even_when_the_aggregate_is_expired() -> None:
    doc = fixture("page-overall.json")
    doc["limits"][0]["status"] = "expired"  # the aggregate snapshot is expired ...
    parsed = model.parse_usage_page(doc)
    card = model.console_card(parsed, NOW_MS)  # ... but the weekly window is still fresh
    assert card.pick is not None and card.pick.window_key == "secondary"
    assert card.pick.resets_at_ms == 1791900000000


# -- transient failures keep loading; the page cache ----------------------------


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_a_busy_answer_keeps_loading_and_backs_off_until_the_window_ends() -> None:
    clock = _Clock()
    feed = loop.UsageFeed(clock=clock)
    generation = feed.set_key(("host-a", "7d"))
    delays = []
    for _ in range(5):
        feed.fail(generation, "usage_busy")
        assert feed.state.status == loop.STATUS_LOADING and feed.state.page is None
        delays.append(feed.retry_delay())
        clock.now += 1
    assert delays == [1.0, 2.0, 4.0, 8.0, 8.0]
    clock.now += loop.UNAVAILABLE_AFTER_SECONDS
    feed.fail(generation, "usage_busy")
    assert feed.state.status == loop.STATUS_UNAVAILABLE  # repeated failure ends in unavailable


def test_a_success_ends_the_backoff() -> None:
    feed = loop.UsageFeed(clock=_Clock())
    generation = feed.set_key(("host-a", "7d"))
    feed.fail(generation, "host_offline")
    assert feed.retry_delay() == 1.0
    feed.accept(generation, page(), NOW_MS)
    assert feed.state.status == loop.STATUS_READY and feed.retry_delay() == loop.INTERVAL_SECONDS


def test_a_transient_failure_with_a_page_keeps_it_as_an_error_state() -> None:
    feed = loop.UsageFeed(clock=_Clock())
    generation = feed.set_key(("host-a", "7d"))
    feed.accept(generation, page(), NOW_MS)
    feed.fail(generation, "usage_busy")
    assert feed.state.status == loop.STATUS_ERROR and feed.state.page is not None


def test_a_cached_period_shows_at_once_and_refreshes() -> None:
    clock = _Clock()
    feed = loop.UsageFeed(clock=clock)
    week = ("acct", "host-a", 7, "", False)
    today = ("acct", "host-a", 1, "", True)
    scope = ("acct", "host-a")
    generation = feed.set_key(week, scope)
    feed.accept(generation, page(), NOW_MS)
    feed.set_key(today, scope)
    assert feed.state.page is None  # nothing cached for Today: loading
    # Back to 7d: the cached page is visible even before the effect runs, and while loading.
    shown = feed.visible(week, True, scope)
    assert shown.page is not None and shown.status == loop.STATUS_LOADING
    feed.set_key(week, scope)
    assert feed.state.page is not None and feed.state.status == loop.STATUS_LOADING
    assert feed.begin() is not None  # the refresh runs from the cached page


def test_the_cache_expires_and_is_bounded() -> None:
    clock = _Clock()
    feed = loop.UsageFeed(clock=clock)
    scope = ("acct", "host-a")
    for days in (1, 7, 30, 90, 2):
        generation = feed.set_key(("acct", "host-a", days, "", False), scope)
        feed.accept(generation, page(), NOW_MS)
    assert feed.visible(("acct", "host-a", 1, "", False), True, scope).page is None  # evicted
    assert feed.visible(("acct", "host-a", 90, "", False), True, scope).page is not None
    clock.now += loop.CACHE_MAX_AGE_SECONDS + 1
    assert feed.visible(("acct", "host-a", 90, "", False), True, scope).page is None  # too old


def test_a_host_or_account_change_clears_the_cache() -> None:
    feed = loop.UsageFeed(clock=_Clock())
    generation = feed.set_key(("acct", "host-a", 7, "", False), ("acct", "host-a"))
    feed.accept(generation, page(), NOW_MS)
    other = ("acct", "host-b")
    # A render for another host before the effect runs never shows host A's cache.
    assert feed.visible(("acct", "host-b", 7, "", False), True, other).page is None
    feed.set_key(("acct", "host-b", 7, "", False), other)
    feed.set_key(("acct", "host-a", 7, "", False), ("acct", "host-a"))
    assert feed.state.page is None  # coming back to host A starts clean: the cache was cleared
