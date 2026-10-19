"""Poll admission and shutdown stay bounded while the real usage ledger is busy."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from types import SimpleNamespace

import pytest

from relaydeck.gateway.usage import UsageService
from relaydeck.usage.contracts import UsageQuery, UsageUnavailable
from relaydeck.usage.limits_plane.scheduler import LimitsScheduler
from relaydeck.usage.store import SqliteUsageStore

NOW = 1_700_000_000_000
QUERY = UsageQuery("mac", "2023-11-14", "2023-11-14")


def test_many_usage_polls_expire_without_releasing_the_collector_lock(tmp_path):
    async def scenario():
        store = SqliteUsageStore(tmp_path / "usage.db", "UTC", clock=lambda: NOW)
        service = UsageService(
            store,
            SimpleNamespace(sources=(), timezone="UTC", inferred_linking=True),
            "mac",
            tmp_path,
            lambda: NOW,
        )
        entered, release = threading.Event(), threading.Event()

        def collector():
            with store._lock:
                entered.set()
                release.wait(3)

        thread = threading.Thread(target=collector)
        thread.start()
        service.wait_s = 0.03
        service.request_wait_s = 0.03
        tasks = []
        try:
            while not entered.is_set():
                await asyncio.sleep(0)
            tasks = [asyncio.create_task(service.page(QUERY, "owner", str(i))) for i in range(100)]
            done, pending = await asyncio.wait(tasks, timeout=0.3)
            assert not pending, f"{len(pending)} usage polls still waiting for the collector"
            assert all(task.result().status_code == 503 for task in done)
            assert len(service._inflight) <= 16
        finally:
            release.set()
            await asyncio.gather(*tasks)
            await service.close()
            thread.join(1)
            store.close()

    asyncio.run(scenario())


def test_shutdown_interrupts_mutex_wait_and_rejects_new_work(tmp_path):
    store = SqliteUsageStore(tmp_path / "usage.db", "UTC", clock=lambda: NOW)
    entered, release = threading.Event(), threading.Event()

    def held():
        with store._lock:
            entered.set()
            release.wait(3)

    thread = threading.Thread(target=held)
    thread.start()
    errors = []

    def waiting():
        try:
            store.data_generation()
        except UsageUnavailable as exc:
            errors.append(exc.code)

    waiter = threading.Thread(target=waiting)
    try:
        assert entered.wait(1)
        waiter.start()
        store.begin_shutdown()  # Does not wait for the collector mutex.
        waiter.join(1)
        assert not waiter.is_alive() and errors == ["closed"]
        assert not release.is_set()
    finally:
        release.set()
        thread.join(1)
    with pytest.raises(UsageUnavailable, match="closed"):
        store.data_generation()
    store.close()


def test_shutdown_sql_interrupt_rolls_back_and_releases_ledger(tmp_path):
    store = SqliteUsageStore(tmp_path / "usage.db", "UTC", clock=lambda: NOW)

    def shutdown():
        store.begin_shutdown()
        return 0

    store._db.create_function("shutdown_usage", 0, shutdown)
    try:
        with pytest.raises(UsageUnavailable, match="busy"):
            with store._transaction() as db:
                db.execute("INSERT INTO meta VALUES('must_rollback', '1')")
                db.execute("SELECT shutdown_usage()").fetchone()
        assert not store._db.in_transaction
        assert (
            store._db.execute("SELECT value FROM meta WHERE key='must_rollback'").fetchone() is None
        )
        with pytest.raises(UsageUnavailable, match="closed"):
            store.data_generation()
    finally:
        store.close()
    with SqliteUsageStore(tmp_path / "usage.db", "UTC", clock=lambda: NOW) as reopened:
        assert reopened.data_generation() >= 0


def test_usage_open_notification_does_not_acquire_scheduler_or_store_lock(tmp_path):
    store = SqliteUsageStore(tmp_path / "usage.db", "UTC", clock=lambda: NOW)
    scheduler = LimitsScheduler(store, {}, {})
    # The caller holds a plain Lock; a sweep under it would deadlock with a due
    # provider, so notification must only coalesce an event, even under load.
    try:
        with scheduler._lock, store._lock:
            for _ in range(1000):
                scheduler.notify_usage_open()
        assert scheduler._open_requested.is_set() and scheduler._wake.is_set()
        scheduler.stop()
        scheduler._open_requested.clear()
        scheduler.notify_usage_open()
        assert not scheduler._open_requested.is_set()
    finally:
        scheduler.stop()
        store.close()


@pytest.mark.parametrize("contention", ["mutex", "sql_interrupt"])
def test_ledger_busy_uses_retry_response_with_default_deadlines(tmp_path, contention):
    async def scenario():
        store = SqliteUsageStore(tmp_path / "usage.db", "UTC", clock=lambda: NOW)
        service = UsageService(
            store,
            SimpleNamespace(sources=(), timezone="UTC", inferred_linking=True),
            "mac",
            tmp_path,
            lambda: NOW,
        )
        assert service.wait_s == service.request_wait_s == 1.0
        entered, release = threading.Event(), threading.Event()
        worker = None
        validate = service._validate
        failures = []

        def barrier():
            entered.set()
            assert release.wait(3)
            return 1

        def held_ledger():
            with store._lock:
                barrier()

        def interrupted_validation(*args):
            with store._transaction(write=False) as db:
                # A real running SQLite statement, interrupted while its UDF is held.
                db.execute("SELECT held_statement()").fetchone()
            return validate(*args)

        def observe_validation(*args):
            try:
                return (
                    interrupted_validation(*args)
                    if contention == "sql_interrupt"
                    else validate(*args)
                )
            except UsageUnavailable as exc:
                failures.append(exc.code)
                raise

        service._validate = observe_validation
        if contention == "mutex":
            worker = threading.Thread(target=held_ledger)
            worker.start()
            assert entered.wait(1)
        else:
            store._db.create_function("held_statement", 0, barrier)
        request = asyncio.create_task(service.page(QUERY, "owner", "phone"))
        try:
            if contention == "sql_interrupt":
                async with asyncio.timeout(1):
                    while not entered.is_set():
                        await asyncio.sleep(0)
                store._db.interrupt()
                release.set()
            response = await asyncio.wait_for(request, 2)
            # Lock contention retains its admitted validation until the lock is free;
            # only the HTTP waiter expires. Explicit SQL interruption is typed busy.
            assert failures == (["busy"] if contention == "sql_interrupt" else [])
            assert response.status_code == 503
            assert json.loads(response.body) == {"error": "usage_busy"}
            assert response.headers["Retry-After"] == "2"
            assert response.headers["Cache-Control"] == "no-store"
            release.set()
            if worker is not None:
                worker.join(1)
            service._validate = validate
            for _ in range(3):
                assert (await service.page(QUERY, "owner", "phone")).status_code == 200
            # Completed as-of pages return before their coalesced validation settles.
            await asyncio.gather(*service._inflight.values())
            assert service._waiters == 0 and not service._inflight
        finally:
            release.set()
            await asyncio.gather(request, return_exceptions=True)
            await service.close()
            if worker is not None:
                worker.join(1)
            store.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("code", ["closed", "corrupt", "unavailable"])
def test_non_busy_ledger_failure_is_not_reported_as_retryable(tmp_path, code):
    async def scenario():
        store = SqliteUsageStore(tmp_path / "usage.db", "UTC", clock=lambda: NOW)
        service = UsageService(
            store,
            SimpleNamespace(sources=(), timezone="UTC", inferred_linking=True),
            "mac",
            tmp_path,
            lambda: NOW,
        )

        def unavailable(*args):
            raise UsageUnavailable(code)

        service._validate = unavailable
        try:
            response = await service.page(QUERY, "owner", "phone")
            assert response.status_code == 503
            assert json.loads(response.body) == {"error": "usage_unavailable"}
            assert "Retry-After" not in response.headers
        finally:
            await service.close()
            store.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("paused_call", ["interrupt", "close"])
def test_shutdown_interrupt_tolerates_concurrent_close(tmp_path, paused_call):
    store = SqliteUsageStore(tmp_path / "usage.db", "UTC", clock=lambda: NOW)
    db = store._db
    entered, release = threading.Event(), threading.Event()
    errors = []

    class Interpose:
        def __getattr__(self, name):
            return getattr(db, name)

        def interrupt(self):
            if paused_call == "interrupt":
                entered.set()
                assert release.wait(3)
            return db.interrupt()

        def close(self):
            db.close()
            if paused_call == "close":
                entered.set()
                assert release.wait(3)

    store._db = Interpose()

    def first():
        try:
            store.begin_shutdown() if paused_call == "interrupt" else store.close()
        except Exception as exc:
            errors.append(type(exc).__name__)

    worker = threading.Thread(target=first)
    worker.start()
    try:
        assert entered.wait(1)
        if paused_call == "interrupt":
            store.close()
        else:
            store.begin_shutdown()
            store.begin_shutdown()  # Also safe before close() has returned.
    finally:
        release.set()
        worker.join(1)
    assert not worker.is_alive() and not errors
    assert store._closed
    store.begin_shutdown()
    store.begin_shutdown()
    store.close()


def test_shutdown_does_not_hide_an_unrelated_interrupt_programming_error(tmp_path):
    store = SqliteUsageStore(tmp_path / "usage.db", "UTC", clock=lambda: NOW)
    db = store._db

    class BrokenInterrupt:
        def __getattr__(self, name):
            return getattr(db, name)

        def interrupt(self):
            raise sqlite3.ProgrammingError("synthetic")

    store._db = BrokenInterrupt()
    try:
        with pytest.raises(sqlite3.ProgrammingError):
            store.begin_shutdown()
    finally:
        store.close()
