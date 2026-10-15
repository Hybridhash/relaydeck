from pathlib import Path

from relaydeck.usage.collector import UsageCollector
from relaydeck.usage.contracts import UsageQuery, UsageReport, UsageSource
from relaydeck.usage.store import SqliteUsageStore
from tests.support.usage.collector import FakeReader
from tests.support.usage.store import NOW, event


def test_unmapped_counts_minute_cache_never_prices_report(tmp_path: Path) -> None:
    class CountingStore(SqliteUsageStore):
        calls = 0

        def query(self, query: UsageQuery, now_ms: int) -> UsageReport:
            raise AssertionError("collector must not run priced reports")

        def unmapped_sessions(
            self, host: str, first: str, last: str, now: int, *, inferred: bool = True
        ) -> dict[str, int]:
            self.calls += 1
            return super().unmapped_sessions(host, first, last, now, inferred=inferred)

    with CountingStore(tmp_path / "state/u.db", "UTC", clock=lambda: NOW) as store:
        reader = FakeReader()
        reader.events = (event(),)
        source = UsageSource("h1", "kind-a", reader.format_id, tmp_path)
        worker = UsageCollector(
            store, {reader.format_id: reader}, (source,), lambda: (), lambda: NOW
        )
        worker.run_once(NOW)
        assert worker.coverage["kind-a"].unmapped_sessions == 1 and store.calls == 1
        reader.events = (event(session="s2"),)
        worker.run_once(NOW + 1)
        assert worker.coverage["kind-a"].unmapped_sessions == 1 and store.calls == 1
        worker.run_once(NOW + 59999)
        assert worker.coverage["kind-a"].unmapped_sessions == 1 and store.calls == 1
        worker.run_once(NOW + 60000)
        assert worker.coverage["kind-a"].unmapped_sessions == 2 and store.calls == 2


def test_count_refresh_failure_is_throttled_and_preserves_cache(tmp_path: Path) -> None:
    class FaultStore(SqliteUsageStore):
        calls = 0
        fail = False

        def unmapped_sessions(
            self, host: str, first: str, last: str, now: int, *, inferred: bool = True
        ) -> dict[str, int]:
            self.calls += 1
            if self.fail:
                raise RuntimeError("synthetic failure")
            return super().unmapped_sessions(host, first, last, now, inferred=inferred)

    with FaultStore(tmp_path / "state/u.db", "UTC", clock=lambda: NOW) as store:
        reader = FakeReader()
        reader.events = (event(),)
        source = UsageSource("h1", "kind-a", reader.format_id, tmp_path)
        worker = UsageCollector(
            store, {reader.format_id: reader}, (source,), lambda: (), lambda: NOW
        )
        worker.run_once(NOW)
        store.fail = True
        worker.run_once(NOW + 60000)
        worker.run_once(NOW + 60001)
        assert store.calls == 2 and worker.coverage["kind-a"].unmapped_sessions == 1


def test_protocol_only_store_fallback_is_once_per_minute(tmp_path: Path) -> None:
    from unittest.mock import Mock

    from relaydeck.usage.contracts import UsageStore

    with SqliteUsageStore(tmp_path / "state/u.db", "UTC", clock=lambda: NOW) as store:
        proxy = Mock(spec=UsageStore, wraps=store)
        reader = FakeReader()
        reader.events = (event(),)
        source = UsageSource("h1", "kind-a", reader.format_id, tmp_path)
        worker = UsageCollector(
            proxy, {reader.format_id: reader}, (source,), lambda: (), lambda: NOW
        )
        worker.run_once(NOW)
        worker.run_once(NOW + 59999)
        assert proxy.query.call_count == 1 and worker.coverage["kind-a"].unmapped_sessions == 1
        worker.run_once(NOW + 60000)
        assert proxy.query.call_count == 2
