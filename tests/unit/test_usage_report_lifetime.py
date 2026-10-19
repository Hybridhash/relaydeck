"""HTTP deadlines must not abort admitted report work or collector commits."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from types import SimpleNamespace

import pytest

from relaydeck.gateway.usage import UsageService
from relaydeck.usage.collector import UsageCollector
from relaydeck.usage.contracts import UsageQuery, UsageSource
from relaydeck.usage.store import SqliteUsageStore
from tests.support.usage.collector import FakeReader
from tests.support.usage.store import NOW, event, mapping


def test_three_second_report_survives_busy_waiters_and_populates_cache(tmp_path, monkeypatch):
    async def scenario():
        store = SqliteUsageStore(tmp_path / "usage.db", "UTC", clock=lambda: NOW)
        service = UsageService(
            store,
            SimpleNamespace(sources=(), timezone="UTC", inferred_linking=True),
            "h1",
            tmp_path,
            lambda: NOW,
        )
        original = store.report_snapshot
        entered = threading.Event()
        calls = []

        def slow_statement():
            entered.set()
            time.sleep(3)
            return 1

        store._db.create_function("slow_report", 0, slow_statement)

        def slow_report(*args, **kwargs):
            calls.append(args)
            with store._transaction(write=False) as db:
                db.execute("SELECT slow_report()").fetchone()
                # Exercise the progress handler after three seconds of admitted SQL.
                db.execute(
                    "WITH RECURSIVE n(x) AS (VALUES(0) UNION ALL SELECT x+1 FROM n WHERE x<2000) "
                    "SELECT SUM(x) FROM n"
                ).fetchone()
            return original(*args, **kwargs)

        monkeypatch.setattr(store, "report_snapshot", slow_report)
        query = UsageQuery("h1", "2026-10-05", "2026-10-11")
        try:
            began = time.monotonic()
            first = await service.page(query, "owner", "phone")
            assert entered.is_set()
            assert time.monotonic() - began < 1.5
            assert first.status_code == 503
            assert json.loads(first.body) == {"error": "usage_busy"}
            assert first.headers["Retry-After"] == "2"
            assert len(service._inflight) == 1 and len(service._workers) == 1
            producer = next(iter(service._inflight.values()))
            assert not producer.done()
            replies = await asyncio.gather(
                *(service.page(query, "owner", "phone") for _ in range(100))
            )
            assert all(reply.status_code == 503 for reply in replies)
            assert len(calls) == 1 and service._waiters == 0
            assert list(service._inflight.values()) == [producer]
            done, _ = await asyncio.wait({producer}, timeout=4)
            assert done and producer.result() in service._reuse.values()
            assert 3 <= time.monotonic() - began < 4.5
            cached_at = time.monotonic()
            cached = await service.page(query, "owner", "phone")
            assert cached.status_code == 200
            assert time.monotonic() - cached_at < 0.25
            await asyncio.gather(*tuple(service._inflight.values()))  # Single-flight validation.
            assert len(calls) == 1 and not service._inflight and not service._workers
            print(f"report: elapsed={time.monotonic() - began:.3f}s cached=200 computations=1")
        finally:
            store.begin_shutdown()
            await service.close()
            store.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("delay", ["batch", "lock_admission"])
def test_three_second_collector_work_commits_without_timeout(tmp_path, monkeypatch, delay):
    async def scenario():
        root = tmp_path / "logs"
        root.mkdir()
        store = SqliteUsageStore(tmp_path / "usage.db", "UTC", clock=lambda: NOW)
        reader = FakeReader()
        reader.events = tuple(event(f"e{index}", session="s1") for index in range(100))
        source = UsageSource("h1", "kind-a", "format-a", root)
        collector = UsageCollector(
            store,
            {reader.format_id: reader},
            (source,),
            lambda: (mapping(),),
            lambda: NOW,
            lambda _: None,
        )
        entered = threading.Event()
        original = store._apply_events

        def delayed_apply(*args):
            if not entered.is_set():
                entered.set()
                time.sleep(3)
            return original(*args)

        def held_report():
            with store._transaction(write=False):
                entered.set()
                time.sleep(3)

        holder = None
        running = None

        def waiting_apply(*args, **kwargs):
            nonlocal holder
            holder = threading.Thread(target=held_report)
            holder.start()
            assert entered.wait(1)
            return original_apply(*args, **kwargs)

        original_apply = store._apply
        try:
            if delay == "batch":
                monkeypatch.setattr(store, "_apply_events", delayed_apply)
            else:
                # Hold the connection after the reader's budget check, before ingest
                # admission, so this tests commit admission rather than reader limits.
                monkeypatch.setattr(store, "_apply", waiting_apply)
            began = time.monotonic()
            running = asyncio.create_task(asyncio.to_thread(collector.run_once, NOW))
            count = await asyncio.wait_for(asyncio.shield(running), 5)
            assert entered.is_set() and count == 100
            assert store.cursor(source.source_key).revision == 1
            assert store._db.execute("SELECT COUNT(*) FROM events").fetchone() == (100,)
            assert time.monotonic() - began >= 2.9
            print(f"collector {delay}: elapsed={time.monotonic() - began:.3f}s committed=100")
        finally:
            store.begin_shutdown()
            if holder is not None:
                await asyncio.to_thread(holder.join, 4)
            if running is not None:
                await asyncio.gather(running, return_exceptions=True)
            collector.stop()
            store.close()

    asyncio.run(scenario())
