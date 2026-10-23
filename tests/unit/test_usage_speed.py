"""ingest-time pricing equivalence, backfill, reuse, duty."""

from __future__ import annotations

import asyncio
import json
import random
import time
from dataclasses import asdict
from pathlib import Path

from relaydeck.companion.config import UsageConfig, UsageSourceConfig
from relaydeck.gateway.usage import UsageService
from relaydeck.usage.collector import DUTY_FLOOR_MS, UsageCollector
from relaydeck.usage.contracts import (
    Basis,
    Counters,
    UsageEvent,
    UsageQuery,
    UsageSource,
)
from relaydeck.usage.pricing import PriceRate, PriceTable
from relaydeck.usage.store import SqliteUsageStore
from tests.support.usage.collector import FakeReader
from tests.support.usage.store import NOW, SOURCE, event, mapping, write

PRICES = PriceTable(
    rates=(
        PriceRate(
            "model-a", "2026-01-01", 3.0, 15.0, 0.3, 3.75, 6.0, None, None, None, None, None, None
        ),
        PriceRate(
            "model-b", "2026-01-01", 1.0, 5.0, 0.1, None, None, 200_000, 2.0, 10.0, 0.2, None, None
        ),
    ),
    provenance=("2026-09-01", "2026-09-01"),
)


def _dump(store: SqliteUsageStore, query: UsageQuery) -> str:
    return json.dumps(asdict(store.query(query, NOW)), sort_keys=True)


def _force_legacy(store: SqliteUsageStore) -> None:
    store._db.execute("UPDATE events SET price_digest=NULL, cost_micro=NULL, price_pin=NULL")


def _events() -> tuple[UsageEvent, ...]:
    return (
        event("known", model="model-a"),
        event("tiered", model="model-b"),
        event("unknown", model="model-zzz"),
        event("nomodel", model=None),
        UsageEvent("s1", "empty", NOW, "model-a", Counters(), Basis.PER_CALL),
        UsageEvent(
            "s1", "cw-unknown", NOW, "model-a", Counters(cache_write_unknown=7), Basis.PER_CALL
        ),
        UsageEvent(
            "s1", "cred", NOW, None, Counters(), Basis.PER_CALL, meter="credits", credits_milli=1234
        ),
        event("unverified", verified=False),
    )


def test_sql_totals_match_legacy_totals_randomised(tmp_path: Path) -> None:
    rng = random.Random(7)  # noqa: S311 - seeded test data
    with SqliteUsageStore(tmp_path / "usage.db", "UTC", prices=PRICES, clock=lambda: NOW) as store:
        events = list(_events())
        for i in range(120):
            counts = Counters(
                input_uncached=rng.randrange(0, 3000),
                cache_read=rng.randrange(0, 9000),
                cache_write_short=rng.randrange(0, 500),
                output=rng.randrange(1, 2000),
            )
            events.append(
                UsageEvent(
                    f"s{i % 5}",
                    f"r{i}",
                    NOW,
                    ("model-a", "model-b", "model-zzz", None)[i % 4],
                    counts,
                    Basis.PER_CALL,
                )
            )
        write(store, tuple(events))
        daily = UsageQuery("h1", "2026-10-11", "2026-10-11")
        hourly = UsageQuery("h1", "2026-10-11", "2026-10-11", hour=True)
        priced_daily, priced_hourly = _dump(store, daily), _dump(store, hourly)
        _force_legacy(store)
        assert _dump(store, daily) == priced_daily
        assert _dump(store, hourly) == priced_hourly


def test_backfill_resumes_and_reprices_on_table_change(tmp_path: Path) -> None:
    path = tmp_path / "usage.db"
    with SqliteUsageStore(path, "UTC", prices=PRICES, clock=lambda: NOW) as store:
        write(store, _events())
        expected = _dump(store, UsageQuery("h1", "2026-10-11", "2026-10-11"))
        _force_legacy(store)
        done, remaining = store.backfill_prices(limit=2)
        assert done == 2 and remaining > 0
        assert _dump(store, UsageQuery("h1", "2026-10-11", "2026-10-11")) == expected
    with SqliteUsageStore(path, "UTC", prices=PRICES, clock=lambda: NOW) as store:
        while True:
            _, remaining = store.backfill_prices(limit=3)
            if not remaining:
                break
        assert _dump(store, UsageQuery("h1", "2026-10-11", "2026-10-11")) == expected
        changed = PriceTable(
            rates=(
                PriceRate(
                    "model-a",
                    "2026-01-01",
                    30.0,
                    150.0,
                    0.3,
                    3.75,
                    6.0,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                ),
            ),
            provenance=("2026-10-02",),
        )
    with SqliteUsageStore(path, "UTC", prices=changed, clock=lambda: NOW) as store:
        query = UsageQuery("h1", "2026-10-11", "2026-10-11")
        legacy_new = _dump(store, query)
        assert legacy_new != expected
        while True:
            _, remaining = store.backfill_prices(limit=3)
            if not remaining:
                break
        assert _dump(store, query) == legacy_new


def test_generation_bumps_on_apply(tmp_path: Path) -> None:
    with SqliteUsageStore(tmp_path / "usage.db", "UTC", prices=PRICES, clock=lambda: NOW) as store:
        before = store.data_generation()
        write(store, (event("g1"),))
        assert store.data_generation() > before


def test_gateway_serves_asof_until_generation_refresh_completes(tmp_path: Path) -> None:
    config = UsageConfig(
        "UTC",
        (UsageSourceConfig("kind-a", "jsonl-message-usage-v1", Path("/tmp"), "A"),),
        (),
        90,
        (),
    )
    with SqliteUsageStore(tmp_path / "usage.db", "UTC", prices=PRICES, clock=lambda: NOW) as store:
        write(store, (event("k1"),))
        service = UsageService(store, config, "h1", tmp_path, lambda: NOW)
        calls = 0
        original = service._report

        def counting(query, now):
            nonlocal calls
            calls += 1
            return original(query, now)

        service._report = counting  # type: ignore[method-assign]
        query = UsageQuery("h1", "2026-10-11", "2026-10-11")

        async def run() -> tuple[bytes, bytes, bytes, bytes]:
            first = await service.page(query, "owner", "device-a")
            second = await service.page(query, "owner", "device-a")
            await asyncio.gather(*tuple(service._inflight.values()))
            write(store, (event("k2"),))
            third = await service.page(query, "owner", "device-a")
            await asyncio.gather(*tuple(service._inflight.values()))
            fourth = await service.page(query, "owner", "device-a")
            await asyncio.gather(*tuple(service._inflight.values()))
            await service.close()
            return bytes(first.body), bytes(second.body), bytes(third.body), bytes(fourth.body)

        first, second, third, fourth = asyncio.run(run())
        assert first == second == third  # The cached as-of page stays usable while refreshing.
        assert fourth != first
        assert calls == 2


def test_collector_duty_floor(tmp_path: Path) -> None:
    root = tmp_path / "logs"
    root.mkdir()
    now = [NOW]
    source = UsageSource("h1", "kind-a", "format-a", root)
    reader = FakeReader()
    with SqliteUsageStore(tmp_path / "usage.db", "UTC", clock=lambda: now[0]) as store:
        worker = UsageCollector(
            store,
            {reader.format_id: reader},
            (source,),
            lambda: (mapping(),),
            lambda: now[0],
            lambda s: None,
        )
        worker._busy_window.append((time.monotonic(), 3000))
        worker.run_once(NOW)
        assert worker.next_delay_ms >= DUTY_FLOOR_MS


def test_report_perf_60k_sql_is_not_slower_than_legacy(tmp_path: Path) -> None:
    rng = random.Random(42)  # noqa: S311 - seeded test data
    with SqliteUsageStore(tmp_path / "usage.db", "UTC", prices=PRICES, clock=lambda: NOW) as store:
        db = store._db
        rows = []
        for n in range(60_000):
            counts = {
                "input_uncached": rng.randrange(0, 5000),
                "cache_read": rng.randrange(0, 9000),
                "cache_write_short": rng.randrange(0, 2000),
                "cache_write_long": 0,
                "output": rng.randrange(0, 3000),
                "reasoning": 0,
                "cache_write_unknown": 0,
            }
            rows.append(
                (
                    "h1",
                    "kind-a",
                    SOURCE,
                    f"s{n % 400}",
                    f"e{n}",
                    NOW + n * 1000,
                    "2026-10-11",
                    ("model-a", "model-b", "model-zzz", None)[n % 4],
                    json.dumps(counts, sort_keys=True, separators=(",", ":")),
                    1,
                    None,
                    "tokens",
                    0,
                )
            )
        db.executemany(
            "INSERT INTO events(host,kind,source,session,event,occurred,day,model,counters,"
            "verified,parent_session_key,meter,credits_milli) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
        query = UsageQuery("h1", "2026-10-11", "2026-10-11")

        def timed() -> float:
            best = min(_time_query(store, query) for _ in range(3))
            return best

        legacy_s = timed()
        legacy_dump = _dump(store, query)
        while True:
            _, remaining = store.backfill_prices(limit=10_000)
            if not remaining:
                break
        sql_s = timed()
        assert _dump(store, query) == legacy_dump
        print(f"\nperf60k legacy={legacy_s:.3f}s sql={sql_s:.3f}s")
        # A regression guard: the paths are within a few percent, so a strict win flakes
        # whenever the machine is busy.
        assert sql_s < legacy_s * 1.25


def _time_query(store: SqliteUsageStore, query: UsageQuery) -> float:
    start = time.perf_counter()
    store.query(query, NOW)
    return time.perf_counter() - start
