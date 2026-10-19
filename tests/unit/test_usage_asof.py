"""Completed reports remain usable while ongoing ingest advances the ledger."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from types import SimpleNamespace

import pytest

from relaydeck.gateway.usage import CURSOR_TTL_MS, UsageService
from relaydeck.usage.contracts import UsageQuery
from relaydeck.usage.store import SqliteUsageStore
from tests.support.usage.store import NOW, event, write

QUERY = UsageQuery("h1", "2026-10-05", "2026-10-11")
CONFIG = SimpleNamespace(sources=(), timezone="UTC", inferred_linking=True)


@pytest.mark.parametrize("commit", ["during_computation", "after_read", "after_completion"])
def test_slow_report_survives_active_ingest_and_refreshes_asof(tmp_path, monkeypatch, commit):
    async def scenario():
        clock = [NOW]
        store = SqliteUsageStore(tmp_path / "usage.db", "UTC", clock=lambda: clock[0])
        write(store, (event("initial"),))
        service = UsageService(store, CONFIG, "h1", tmp_path, lambda: clock[0])
        original = store.report_snapshot
        calls = []

        def report(*args, **kwargs):
            calls.append(len(calls) + 1)
            time.sleep(1.1)  # Each computation is longer than the actual HTTP wait.
            clock[0] += 1000
            if commit == "during_computation":
                write(store, (event(f"before-read-{len(calls)}"),))
            result = original(*args, **kwargs)
            if commit == "after_read":
                clock[0] += 1000
                write(store, (event(f"after-read-{len(calls)}"),))
            return result

        monkeypatch.setattr(store, "report_snapshot", report)
        try:
            first = await service.page(QUERY, "owner", "phone")
            assert first.status_code == 503 and first.headers["Retry-After"] == "2"
            producer = next(iter(service._inflight.values()))
            done, _ = await asyncio.wait({producer}, timeout=2)
            assert done
            snap = producer.result()
            expected_generation = 2 if commit == "during_computation" else 1
            assert snap.generation == expected_generation
            body = json.loads(snap.pages[0])
            assert body["as_of_ms"] == NOW + 1000
            assert sum(
                row["counters"]["input_uncached"]
                for row in body["rows"]
                if row["scope"] == "overall"
            ) == (20 if commit == "during_computation" else 10)
            # Ensure another real commit occurs after publication, before polling.
            clock[0] += 1000
            write(store, (event("after-completion"),))
            assert store.data_generation() > snap.generation
            began = time.monotonic()
            cached = await service.page(QUERY, "owner", "phone")
            assert cached.status_code == 200 and bytes(cached.body) == snap.pages[0]
            assert time.monotonic() - began < 0.25
            refresh = next(iter(service._inflight.values()))
            replies = await asyncio.gather(
                *(service.page(QUERY, "owner", "phone") for _ in range(100))
            )
            assert all(
                reply.status_code == 200 and bytes(reply.body) == snap.pages[0] for reply in replies
            )
            assert list(service._inflight.values()) == [refresh]
            done, _ = await asyncio.wait({refresh}, timeout=2)
            assert done
            newer = refresh.result()
            assert newer is not snap and newer.generation > snap.generation
            assert json.loads(newer.pages[0])["as_of_ms"] > body["as_of_ms"]
            assert len(calls) == 2
            assert snap.generation == expected_generation  # Never relabel the stale result.
            print(f"{commit}: busy -> completed 200; 100 cached polls; computations=2")
        finally:
            store.begin_shutdown()
            await service.close()
            store.close()

    asyncio.run(scenario())


def test_report_version_matches_its_sqlite_snapshot_during_external_commit(tmp_path):
    path = tmp_path / "usage.db"
    store = SqliteUsageStore(path, "UTC", clock=lambda: NOW)
    write(store, (event("initial"),))
    other = SqliteUsageStore(path, "UTC", clock=lambda: NOW + 1)
    entered, committed = threading.Event(), threading.Event()
    errors = []

    def writer():
        try:
            assert entered.wait(2)
            write(other, (event("concurrent"),))
        except BaseException as exc:
            errors.append(type(exc).__name__)
        finally:
            committed.set()

    def trace(sql):
        # The metadata version read has already pinned the SQLite read snapshot.
        if sql.startswith("WITH") and not entered.is_set():
            entered.set()
            assert committed.wait(2)

    thread = threading.Thread(target=writer)
    store._db.set_trace_callback(trace)
    thread.start()
    try:
        report = store.report_snapshot(QUERY, NOW)
        assert committed.is_set() and not errors
        assert report.data_generation == 1 and report.as_of_ms == NOW
        assert (
            sum(row.counters.input_uncached for row in report.rows if row.scope == "overall") == 10
        )
        assert store.data_generation() == 2
    finally:
        entered.set()
        thread.join(2)
        store._db.set_trace_callback(None)
        other.close()
        store.close()


@pytest.mark.parametrize("change", ["account", "device", "range", "hour", "space", "expiry"])
def test_completed_asof_cannot_cross_its_binding_or_expiry(tmp_path, monkeypatch, change):
    async def scenario():
        clock = [NOW]
        store = SqliteUsageStore(tmp_path / "usage.db", "UTC", clock=lambda: clock[0])
        service = UsageService(store, CONFIG, "h1", tmp_path, lambda: clock[0])
        release = threading.Event()
        original = service._report

        def blocked(*args):
            assert release.wait(2)
            return original(*args)

        try:
            warm_query = UsageQuery("h1", "2026-10-11", "2026-10-11") if change == "hour" else QUERY
            warm = await service.page(warm_query, "owner", "phone", selector="days:7")
            assert warm.status_code == 200
            monkeypatch.setattr(service, "_report", blocked)
            service.request_wait_s = 0.02
            query = warm_query
            if change == "range":
                query = UsageQuery("h1", "2026-10-06", "2026-10-12")
            elif change == "hour":
                query = UsageQuery("h1", warm_query.from_day, warm_query.to_day, hour=True)
            elif change == "space":
                query = UsageQuery("h1", QUERY.from_day, QUERY.to_day, space_id="other-space")
            elif change == "expiry":
                clock[0] += CURSOR_TTL_MS
            response = await service.page(
                query,
                "other-owner" if change == "account" else "owner",
                "other-phone" if change == "device" else "phone",
                selector="days:7",
            )
            assert response.status_code == 503 and response.headers["Retry-After"] == "2"
        finally:
            release.set()
            store.begin_shutdown()
            await service.close()
            store.close()

    asyncio.run(scenario())


def test_refresh_keeps_old_cursor_identity_and_eviction_removes_reuse(tmp_path):
    async def scenario():
        clock = [NOW]
        store = SqliteUsageStore(tmp_path / "usage.db", "UTC", clock=lambda: clock[0])
        write(store, tuple(event(f"e{i}", model=f"m{i}") for i in range(600)))
        service = UsageService(store, CONFIG, "h1", tmp_path, lambda: clock[0])
        try:
            first = await service.page(QUERY, "owner", "phone")
            old = json.loads(first.body)
            cursor = old["next_cursor"]
            old_key, _ = service._cursors[cursor]
            old_snap = service._reports[old_key]
            clock[0] += 1000
            write(store, (event("new-event", model="new-model"),))
            cached = await service.page(QUERY, "owner", "phone")
            assert bytes(cached.body) == bytes(first.body)
            await asyncio.gather(*tuple(service._inflight.values()))
            assert old_snap.expires == NOW + CURSOR_TTL_MS
            pointer = UsageQuery("h1", QUERY.from_day, QUERY.to_day, cursor=cursor)
            page = await service.page(pointer, "owner", "phone")
            assert page.status_code == 200 and json.loads(page.body)["as_of_ms"] == old["as_of_ms"]
            assert not any(row["model"] == "new-model" for row in json.loads(page.body)["rows"])
            latest = next(iter(service._reuse.values()))
            assert latest is not old_snap
            key = next(key for key, snap in service._reports.items() if snap is latest)
            service._drop(key)  # The same bounded-cache eviction path used in production.
            assert not service._reuse
            assert latest.cursors[0] not in service._cursors
            assert cursor in service._cursors
            clock[0] = old_snap.expires
            assert (await service.page(pointer, "owner", "phone")).status_code == 400
        finally:
            store.begin_shutdown()
            await service.close()
            store.close()

    asyncio.run(scenario())
