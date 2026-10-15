"""Store: per-event priced and unpriced aggregates, hourly buckets with DST, hourly
cursor paging and the explicit capacity state. Synthetic events and a temporary database only."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from relaydeck.usage.contracts import (
    CAPACITY_EXCEEDED,
    Basis,
    Batch,
    Counters,
    Coverage,
    PriceRate,
    RowScope,
    SessionMapping,
    UsageEvent,
    UsageQuery,
)
from relaydeck.usage.pricing import PriceTable
from relaydeck.usage.store import SqliteUsageStore, UsageUnavailable

SOURCE = "h1/kind-a/format-a"
HOST = "h1"
UTC = ZoneInfo("UTC")


def at(zone: str, *args: int) -> int:
    return int(datetime(*args, tzinfo=ZoneInfo(zone)).timestamp() * 1000)


def event(
    key: str,
    t: int,
    counters: Counters,
    *,
    model: str = "model-a",
    session: str = "s1",
    verified: bool = True,
) -> UsageEvent:
    return UsageEvent(
        session, key, t, model, counters, Basis.PER_CALL, accounting_verified=verified
    )


def mapping(space: str = "w1", session: str = "s1") -> SessionMapping:
    return SessionMapping(SOURCE, "kind-a", session, "i1", HOST, space, 0, 2**62)


def write(
    store: SqliteUsageStore, events: tuple[UsageEvent, ...], maps: tuple[SessionMapping, ...] = ()
) -> None:
    cursor = store.cursor(SOURCE)
    batch = Batch(events, cursor, Coverage.COMPLETE, cursor.revision, ())
    store.apply("kind-a", batch, maps or (mapping(),), source_key=SOURCE)


def priced() -> PriceTable:
    """Synthetic rates: model-a costs $1 per million input tokens; free is priced at 0."""
    return PriceTable(
        rates=(
            PriceRate("model-a", "2026-01-01", 1.0, 2.0),
            PriceRate("free", "2026-01-01", 0.0, 0.0),
        ),
        provenance=("2026-10-01", "2026-10-01"),
        snapshot_sources=(("2026-10-01", "synthetic"),),
        snapshot_labels=(("2026-10-01", "Synthetic"),),
    )


def rows_of(store: SqliteUsageStore, first: str, last: str, now: int, **kw: object) -> tuple:
    return store.query(UsageQuery(HOST, first, last, **kw), now).rows  # type: ignore[arg-type]


def test_per_event_partial_aggregates_keep_the_priced_part(tmp_path: Path) -> None:
    now = at("UTC", 2026, 10, 11, 23)
    with SqliteUsageStore(
        tmp_path / "state/usage.db", "UTC", prices=priced(), clock=lambda: now
    ) as store:
        write(
            store,
            (
                event("priced", at("UTC", 2026, 10, 11, 12), Counters(input_uncached=1_000_000)),
                event(
                    "unknown",
                    at("UTC", 2026, 10, 11, 13),
                    Counters(input_uncached=10, cache_write_unknown=3),
                ),
                event("empty", at("UTC", 2026, 10, 11, 14), Counters()),
                event(
                    "free", at("UTC", 2026, 10, 11, 15), Counters(input_uncached=7), model="free"
                ),
            ),
        )
        by_model = {
            r.model: r
            for r in rows_of(store, "2026-10-11", "2026-10-11", now)
            if r.scope == RowScope.OVERALL
        }
        mixed = by_model["model-a"]
        assert mixed.cost_micro_usd is None  # legacy contract: any unpriced event nulls the cost
        assert mixed.priced_cost_micro_usd == 1_000_000
        assert mixed.priced_tokens == 1_000_000
        assert mixed.unpriced_tokens == 13  # the unknown event; the empty one is neither
        free = by_model["free"]
        assert free.cost_micro_usd == 0 and free.priced_tokens == 7 and free.unpriced_tokens == 0
        assert free.price_snapshot == "2026-10-01"


def test_zero_token_events_are_neither_priced_nor_unpriced_and_set_no_snapshot(
    tmp_path: Path,
) -> None:
    now = at("UTC", 2026, 10, 11, 23)
    with SqliteUsageStore(
        tmp_path / "state/usage.db", "UTC", prices=priced(), clock=lambda: now
    ) as store:
        write(store, (event("zero", at("UTC", 2026, 10, 11, 9), Counters(), model="model-a"),))
        row = next(
            r
            for r in rows_of(store, "2026-10-11", "2026-10-11", now)
            if r.scope == RowScope.OVERALL
        )
        assert row.cost_micro_usd == 0
        assert (row.priced_tokens, row.unpriced_tokens, row.priced_cost_micro_usd) == (0, 0, 0)
        assert row.price_snapshot is None


def test_unverified_events_stay_excluded_per_event(tmp_path: Path) -> None:
    now = at("UTC", 2026, 10, 11, 23)
    with SqliteUsageStore(
        tmp_path / "state/usage.db", "UTC", prices=priced(), clock=lambda: now
    ) as store:
        write(
            store,
            (
                event("ok", at("UTC", 2026, 10, 11, 9), Counters(input_uncached=5), model="free"),
                event(
                    "bad",
                    at("UTC", 2026, 10, 11, 10),
                    Counters(input_uncached=900),
                    model="free",
                    verified=False,
                ),
            ),
        )
        row = next(
            r
            for r in rows_of(store, "2026-10-11", "2026-10-11", now)
            if r.scope == RowScope.OVERALL
        )
        assert row.priced_tokens == 5 and row.counters.input_uncached == 5


def test_fall_back_hour_merges_both_occurrences_into_one_label(tmp_path: Path) -> None:
    zone = "America/New_York"
    now = at(zone, 2026, 11, 2, 12)
    with SqliteUsageStore(tmp_path / "state/usage.db", zone, clock=lambda: now) as store:
        write(
            store,
            (
                event(
                    "first-1", at(zone, 2026, 11, 1, 1, 30) - 0, Counters(input_uncached=10)
                ),  # 01:30 EDT
                event(
                    "second-1",
                    at(zone, 2026, 11, 1, 1, 30) + 3_600_000,
                    Counters(input_uncached=20),
                ),  # 01:30 EST
                event("late", at(zone, 2026, 11, 1, 7), Counters(input_uncached=5)),
            ),
        )
        hourly = rows_of(store, "2026-11-01", "2026-11-01", now, hour=True)
        overall = {r.hour: r for r in hourly if r.scope == RowScope.OVERALL}
        assert sorted(overall) == [1, 7]
        assert overall[1].counters.input_uncached == 30  # both occurrences, one label
        assert overall[7].counters.input_uncached == 5


def test_spring_forward_skipped_hour_has_no_row(tmp_path: Path) -> None:
    zone = "America/New_York"
    now = at(zone, 2026, 3, 9, 12)
    with SqliteUsageStore(tmp_path / "state/usage.db", zone, clock=lambda: now) as store:
        write(
            store,
            (
                event(
                    "before", at(zone, 2026, 3, 8, 1, 30), Counters(input_uncached=4)
                ),  # 01:30 EST
                event(
                    "after", at(zone, 2026, 3, 8, 3, 30), Counters(input_uncached=6)
                ),  # 03:30 EDT
            ),
        )
        overall = sorted(
            r.hour
            for r in rows_of(store, "2026-03-08", "2026-03-08", now, hour=True)
            if r.scope == RowScope.OVERALL
        )
        assert overall == [1, 3]  # 02:xx never existed: the client fills it with zero


def test_hourly_rows_page_by_cursor_and_match_the_unpaged_report(tmp_path: Path) -> None:
    zone = "America/New_York"
    now = at(zone, 2026, 11, 2, 12)
    with SqliteUsageStore(tmp_path / "state/usage.db", zone, clock=lambda: now) as store:
        write(
            store,
            (
                event("a", at(zone, 2026, 11, 1, 1, 30), Counters(input_uncached=1)),
                event("b", at(zone, 2026, 11, 1, 1, 30) + 3_600_000, Counters(input_uncached=2)),
                event("c", at(zone, 2026, 11, 1, 7), Counters(input_uncached=3), model="model-b"),
                event("d", at(zone, 2026, 11, 1, 9), Counters(input_uncached=4), session="s2"),
            ),
            maps=(mapping(), mapping(session="s2", space="w2")),
        )
        whole = rows_of(store, "2026-11-01", "2026-11-01", now, hour=True)
        paged: list = []
        report = store.query(
            UsageQuery(HOST, "2026-11-01", "2026-11-01", hour=True), now, _row_limit=1
        )
        paged.extend(report.rows)
        while report.next_cursor:
            report = store.query(
                UsageQuery(HOST, "2026-11-01", "2026-11-01", hour=True, cursor=report.next_cursor),
                now,
                _row_limit=1,
            )
            paged.extend(report.rows)
        assert paged == list(whole)
        daily = store.query(UsageQuery(HOST, "2026-11-01", "2026-11-01"), now, _row_limit=1)
        assert daily.next_cursor is not None
        with pytest.raises(UsageUnavailable, match="invalid_query"):
            store.query(
                UsageQuery(HOST, "2026-11-01", "2026-11-01", hour=True, cursor=daily.next_cursor),
                now,
            )


def test_hourly_needs_exactly_one_day(tmp_path: Path) -> None:
    now = at("UTC", 2026, 10, 11, 23)
    with SqliteUsageStore(tmp_path / "state/usage.db", "UTC", clock=lambda: now) as store:
        with pytest.raises(UsageUnavailable, match="invalid_query"):
            store.query(UsageQuery(HOST, "2026-10-10", "2026-10-11", hour=True), now)


def test_over_the_cap_is_an_explicit_capacity_state_without_rows(tmp_path: Path) -> None:
    now = at("UTC", 2026, 10, 11, 23)
    with SqliteUsageStore(tmp_path / "state/usage.db", "UTC", clock=lambda: now) as store:
        write(
            store,
            (
                event("a", at("UTC", 2026, 10, 11, 9), Counters(input_uncached=1), model="model-a"),
                event("b", at("UTC", 2026, 10, 11, 9), Counters(input_uncached=1), model="model-b"),
            ),
        )
        exceeded = store.report_snapshot(
            UsageQuery(HOST, "2026-10-11", "2026-10-11"), now, max_rows=1
        )
        assert exceeded.capacity == CAPACITY_EXCEEDED
        assert exceeded.rows == () and exceeded.next_cursor is None
        fits = store.report_snapshot(UsageQuery(HOST, "2026-10-11", "2026-10-11"), now, max_rows=10)
        assert fits.capacity == "ok" and len(fits.rows) >= 2


def test_mixed_snapshot_group_keeps_each_priced_part_and_marks_the_snapshot_mixed(
    tmp_path: Path,
) -> None:
    """Two priced events under different snapshots and one unpriced event share a group. The priced
    subtotal is the sum of the per-event costs, and the unpriced tokens stay separate: they are
    never repriced across snapshots."""

    class TwoSnapshots(PriceTable):
        def estimate(self, day, model, counters):  # type: ignore[no-untyped-def, override]
            if counters.total == 0:
                return 0, None
            if counters.cache_write_unknown:
                return None, None
            return (5, "2026-10-01") if counters.input_uncached == 10 else (7, "2026-10-11")

    now = at("UTC", 2026, 10, 11, 23)
    with SqliteUsageStore(
        tmp_path / "state/usage.db", "UTC", prices=TwoSnapshots(), clock=lambda: now
    ) as store:
        write(
            store,
            (
                event("early", at("UTC", 2026, 10, 11, 9), Counters(input_uncached=10)),
                event("late", at("UTC", 2026, 10, 11, 10), Counters(input_uncached=20)),
                event(
                    "unknown",
                    at("UTC", 2026, 10, 11, 11),
                    Counters(input_uncached=4, cache_write_unknown=2),
                ),
            ),
        )
        row = next(
            r
            for r in rows_of(store, "2026-10-11", "2026-10-11", now)
            if r.scope == RowScope.OVERALL
        )
        assert row.cost_micro_usd is None  # legacy: an unpriced event nulls the cost
        assert row.priced_cost_micro_usd == 12  # 5 + 7, each event priced on its own
        assert row.priced_tokens == 30 and row.unpriced_tokens == 6
        assert (
            row.price_snapshot is None
        )  # the pin still follows the legacy rule (null with a null cost)
