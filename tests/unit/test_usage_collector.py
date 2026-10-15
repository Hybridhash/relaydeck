from pathlib import Path

import pytest

from relaydeck.usage.collector import UsageCollector
from relaydeck.usage.contracts import (
    ACTIVE_POLL_MS,
    IDLE_POLL_MS,
    PASS_DEADLINE_MS,
    Basis,
    Budget,
    Counters,
    Coverage,
    Cursor,
    FileCursor,
    LimitSnapshot,
    LimitWindow,
    SessionMapping,
    UsageEvent,
    UsageQuery,
    UsageSource,
)
from relaydeck.usage.store import SqliteUsageStore
from tests.support.usage.collector import FakeReader
from tests.support.usage.store import NOW


def test_active_idle_restart_budget_and_content_free_logging(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    root = tmp_path / "logs"
    root.mkdir()
    now = [NOW]
    source = UsageSource("h1", "kind-a", "format-a", root)
    reader = FakeReader()
    reader.events = (
        UsageEvent("session-marker", "e", NOW, "model-a", Counters(1), Basis.PER_CALL),
    )

    def maps() -> tuple[SessionMapping, ...]:
        return (
            SessionMapping(
                source.source_key, "kind-a", "session-marker", "i", "h1", "w1", NOW, NOW
            ),
        )

    caplog.set_level("INFO", logger="relaydeck.usage.collector")
    path = tmp_path / "state/usage.db"
    with SqliteUsageStore(path, "UTC", clock=lambda: now[0]) as store:
        worker = UsageCollector(
            store, {reader.format_id: reader}, (source,), maps, lambda: now[0], lambda s: None
        )
        assert worker.run_once(NOW) == 1 and worker.next_delay_ms == ACTIVE_POLL_MS
        assert reader.budgets[0].deadline_ms <= PASS_DEADLINE_MS
        reader.events = ()
        now[0] += ACTIVE_POLL_MS
        assert worker.run_once(now[0]) == 0 and worker.next_delay_ms == IDLE_POLL_MS
    with SqliteUsageStore(path, "UTC", clock=lambda: now[0]) as store:
        resumed = UsageCollector(
            store, {reader.format_id: reader}, (source,), maps, lambda: now[0], lambda s: None
        )
        resumed.run_once(now[0])
        assert reader.cursors[-1].revision == 2
    for record in caplog.records:
        line = record.getMessage()
        assert line.startswith("usage_pass kind=kind-a events=")
        assert (
            str(root) not in line
            and "session-marker" not in line
            and "private-marker" not in line
            and "model-a" not in line
        )


@pytest.mark.parametrize("unsafe", ["symlink", "missing", "relative", "format"])
def test_unsafe_root_or_unknown_format_never_calls_reader(tmp_path: Path, unsafe: str) -> None:
    root = tmp_path / "logs"
    root.mkdir()
    configured = root
    if unsafe == "symlink":
        configured = tmp_path / "escape"
        configured.symlink_to(root, target_is_directory=True)
    elif unsafe == "missing":
        configured = tmp_path / "missing"
    elif unsafe == "relative":
        configured = Path("relative")
    reader = FakeReader()
    source = UsageSource(
        "h1", "kind-a", "missing" if unsafe == "format" else "format-a", configured
    )
    with SqliteUsageStore(tmp_path / "state/usage.db", "UTC", clock=lambda: NOW) as store:
        worker = UsageCollector(
            store, {reader.format_id: reader}, (source,), lambda: (), lambda: NOW, lambda s: None
        )
        assert worker.run_once(NOW) == 0
        assert not reader.cursors
        assert worker.coverage["kind-a"].coverage == Coverage.UNAVAILABLE
        assert (
            store.query(UsageQuery("h1", "2026-10-11", "2026-10-11"), NOW).coverage[0].coverage
            == Coverage.UNAVAILABLE
        )


def test_error_backoff_deadline_and_limits_kind_assignment(tmp_path: Path) -> None:
    root = tmp_path / "logs"
    root.mkdir()
    now = [NOW]
    reader = FakeReader()
    reader.error = True
    source = UsageSource("h1", "kind-a", "format-a", root)
    with SqliteUsageStore(tmp_path / "state/usage.db", "UTC", clock=lambda: now[0]) as store:
        worker = UsageCollector(
            store, {reader.format_id: reader}, (source,), lambda: (), lambda: now[0], lambda s: None
        )
        delays = []
        for _ in range(3):
            worker.run_once(now[0])
            delays.append(worker.next_delay_ms)
            now[0] += worker.next_delay_ms
        assert delays == [30000, 60000, 120000]
        reader.error = False
        reader.limits = (
            LimitSnapshot("", "q", now[0], (LimitWindow("x", 50),)),
            LimitSnapshot("kind-b", "other", now[0], (LimitWindow("x", 20),)),
        )
        worker.run_once(now[0])
        limits = store.query(UsageQuery("h1", "2026-10-11", "2026-10-11"), now[0]).limits
        assert len(limits) == 1 and limits[0].kind == "kind-a"
        assert worker.coverage["kind-a"].coverage == Coverage.PARTIAL
        before = store.cursor(source.source_key)
        reader.advance = lambda: now.__setitem__(0, now[0] + PASS_DEADLINE_MS + 1)
        reader.events = (UsageEvent("s", "late", now[0], None, Counters(1), Basis.PER_CALL),)
        assert worker.run_once(now[0]) == 0
        assert worker.coverage["kind-a"].coverage == Coverage.PARTIAL
        assert store.cursor(source.source_key).files == before.files


def test_daemon_single_start_stop_and_hourly_prune(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading

    reader = FakeReader()
    root = tmp_path / "logs"
    root.mkdir()
    source = UsageSource("h1", "kind-a", "format-a", root)
    with SqliteUsageStore(tmp_path / "state/usage.db", "UTC", clock=lambda: NOW) as store:
        prunes: list[int] = []
        original = store.prune

        def prune(now: int) -> int:
            prunes.append(now)
            return original(now)

        monkeypatch.setattr(store, "prune", prune)
        reached = threading.Event()
        worker = UsageCollector(
            store,
            {reader.format_id: reader},
            (source,),
            lambda: (),
            lambda: NOW,
            lambda s: reached.set(),
        )
        worker.start()
        thread = worker._thread
        worker.start()
        assert thread is worker._thread and thread is not None and thread.daemon
        assert reached.wait(2)
        worker.stop()
        assert not thread.is_alive()
        worker.run_once(NOW)
        assert prunes == [NOW]


def test_foreign_root_and_source_bound(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    from relaydeck.usage.contracts import MAX_SOURCES, UsageUnavailable

    root = tmp_path / "logs"
    root.mkdir()
    reader = FakeReader()
    source = UsageSource("h1", "kind-a", "format-a", root)
    with SqliteUsageStore(tmp_path / "state/usage.db", "UTC", clock=lambda: NOW) as store:
        worker = UsageCollector(
            store, {reader.format_id: reader}, (source,), lambda: (), lambda: NOW
        )
        uid = os.getuid()
        monkeypatch.setattr(os, "getuid", lambda: uid + 1)
        worker.run_once(NOW)
        assert not reader.cursors and worker.coverage["kind-a"].coverage == Coverage.UNAVAILABLE
        too_many = tuple(
            UsageSource("h1", "kind-a", f"format-{i}", root) for i in range(MAX_SOURCES + 1)
        )
        with pytest.raises(UsageUnavailable):
            UsageCollector(store, {}, too_many, lambda: ())


def test_slow_first_source_cannot_starve_other_sources(tmp_path: Path) -> None:
    root = tmp_path / "logs"
    root.mkdir()
    now = [NOW]
    slow, other = FakeReader(), FakeReader()
    other.format_id = "format-b"
    slow.advance = lambda: now.__setitem__(0, now[0] + PASS_DEADLINE_MS + 1)
    sources = (
        UsageSource("h1", "kind-a", slow.format_id, root),
        UsageSource("h1", "kind-b", other.format_id, root),
    )
    with SqliteUsageStore(tmp_path / "state/usage.db", "UTC", clock=lambda: now[0]) as store:
        worker = UsageCollector(
            store, {r.format_id: r for r in (slow, other)}, sources, lambda: (), lambda: now[0]
        )
        worker.run_once(now[0])
        assert not other.cursors
        worker.run_once(now[0])
        assert len(other.cursors) == 1


@pytest.mark.parametrize("progress", ["events", "state", "files"])
def test_catchup_requires_committed_progress_and_preserves_budget(
    tmp_path: Path, progress: str
) -> None:
    from relaydeck.usage.collector import CATCHUP_DELAY_MS

    root = tmp_path / "logs"
    root.mkdir()
    now = [NOW]
    reader = FakeReader()
    reader.more = True
    source = UsageSource("h1", "kind-a", reader.format_id, root)
    with SqliteUsageStore(tmp_path / "state/usage.db", "UTC", clock=lambda: now[0]) as store:
        worker = UsageCollector(
            store, {reader.format_id: reader}, (source,), lambda: (), lambda: now[0]
        )
        assert not worker.catching_up
        for position in (1, 2):
            if progress == "events":
                reader.events = (
                    UsageEvent("s", str(position), NOW, None, Counters(1), Basis.PER_CALL),
                )
            elif progress == "state":
                reader.state = {"position": str(position)}
            else:
                reader.files = {"file": FileCursor(1, 2, position, 2, NOW, "0" * 64)}
            worker.run_once(now[0])
            assert worker.next_delay_ms == CATCHUP_DELAY_MS == 250
            assert worker.catching_up
            assert store.cursor(source.source_key).revision == position
            now[0] += CATCHUP_DELAY_MS
        for budget in reader.budgets:
            expected = Budget()
            assert (
                budget.max_bytes,
                budget.max_files,
                budget.max_events,
                budget.max_document_bytes,
            ) == (
                expected.max_bytes,
                expected.max_files,
                expected.max_events,
                expected.max_document_bytes,
            )
            assert 0 < budget.deadline_ms <= PASS_DEADLINE_MS


@pytest.mark.parametrize("progress", [False, True])
def test_non_backlog_never_catches_up(tmp_path: Path, progress: bool) -> None:
    root = tmp_path / "logs"
    root.mkdir()
    reader = FakeReader()
    reader.state = {"gap": "1"}
    if progress:
        reader.events = (UsageEvent("s", "e", NOW, None, Counters(1), Basis.PER_CALL),)
    source = UsageSource("h1", "kind-a", reader.format_id, root)
    with SqliteUsageStore(tmp_path / "state/usage.db", "UTC", clock=lambda: NOW) as store:
        worker = UsageCollector(
            store, {reader.format_id: reader}, (source,), lambda: (), lambda: NOW
        )
        worker.run_once(NOW)
        assert not worker.catching_up
        assert worker.next_delay_ms == (ACTIVE_POLL_MS if progress else IDLE_POLL_MS)


def test_backlog_without_checkpoint_progress_backs_off_then_recovers(tmp_path: Path) -> None:
    from relaydeck.usage.collector import CATCHUP_DELAY_MS

    root = tmp_path / "logs"
    root.mkdir()
    now = [NOW]
    reader = FakeReader()
    reader.more = True
    source = UsageSource("h1", "kind-a", reader.format_id, root)
    with SqliteUsageStore(tmp_path / "state/usage.db", "UTC", clock=lambda: now[0]) as store:
        worker = UsageCollector(
            store, {reader.format_id: reader}, (source,), lambda: (), lambda: now[0]
        )
        for attempt, delay in enumerate((30000, 60000, 120000, 240000, 300000, 300000), 1):
            worker.run_once(now[0])
            assert (
                store.cursor(source.source_key).revision == attempt
            )  # Revision alone isn't progress.
            assert not worker.catching_up
            assert worker.next_delay_ms == delay
            now[0] += 250
            worker.run_once(now[0])
            assert len(reader.cursors) == attempt
            now[0] += delay - 250
        reader.state = {"position": "1"}
        worker.run_once(now[0])
        assert worker.catching_up and worker.next_delay_ms == CATCHUP_DELAY_MS
        now[0] += CATCHUP_DELAY_MS
        worker.run_once(now[0])
        assert not worker.catching_up and worker.next_delay_ms == IDLE_POLL_MS
        assert worker.coverage["kind-a"].coverage == Coverage.COMPLETE


def test_catchup_source_rotation_preserves_other_source_and_backoff(tmp_path: Path) -> None:
    from relaydeck.usage.collector import CATCHUP_DELAY_MS

    root = tmp_path / "logs"
    root.mkdir()
    now = [NOW]
    backlog, stalled = FakeReader(), FakeReader()
    stalled.format_id = "format-b"
    backlog.more = stalled.more = True
    order: list[str] = []
    backlog.advance = lambda: order.append("a")
    stalled.advance = lambda: order.append("b")
    sources = (
        UsageSource("h1", "kind-a", backlog.format_id, root),
        UsageSource("h1", "kind-b", stalled.format_id, root),
    )
    with SqliteUsageStore(tmp_path / "state/usage.db", "UTC", clock=lambda: now[0]) as store:
        worker = UsageCollector(
            store, {r.format_id: r for r in (backlog, stalled)}, sources, lambda: (), lambda: now[0]
        )
        for position in (1, 2, 3):
            backlog.state = {"position": str(position)}
            worker.run_once(now[0])
            assert worker.catching_up and worker.next_delay_ms == CATCHUP_DELAY_MS
            now[0] += IDLE_POLL_MS
        assert order == ["a", "b", "b", "a", "a"]  # Third stalled attempt is still backed off.
        assert len(backlog.cursors) == 3 and len(stalled.cursors) == 2
        backlog.more = False
        worker.run_once(now[0])
        assert not worker.catching_up


def test_catchup_pending_history_survives_an_unvisited_pass(tmp_path: Path) -> None:
    from relaydeck.usage.collector import CATCHUP_DELAY_MS

    root = tmp_path / "logs"
    root.mkdir()
    now = [NOW]
    backlog, slow = FakeReader(), FakeReader()
    slow.format_id = "format-b"
    backlog.more = True
    backlog.state = {"position": "1"}
    slow.advance = lambda: now.__setitem__(0, now[0] + PASS_DEADLINE_MS + 1)
    sources = (
        UsageSource("h1", "kind-a", backlog.format_id, root),
        UsageSource("h1", "kind-b", slow.format_id, root),
    )
    with SqliteUsageStore(tmp_path / "state/usage.db", "UTC", clock=lambda: now[0]) as store:
        worker = UsageCollector(
            store, {r.format_id: r for r in (backlog, slow)}, sources, lambda: (), lambda: now[0]
        )
        worker.run_once(now[0])
        assert worker.catching_up and worker.next_delay_ms == CATCHUP_DELAY_MS
        now[0] += CATCHUP_DELAY_MS
        # The idle slow source is due on its own cadence, so the pass that visits it is at that
        # time.
        now[0] += IDLE_POLL_MS - CATCHUP_DELAY_MS
        worker.run_once(now[0])  # Slow reader goes first; backlog gets no new reader batch.
        assert len(backlog.cursors) == 1
        assert worker.catching_up and worker.next_delay_ms == CATCHUP_DELAY_MS
        now[0] += CATCHUP_DELAY_MS
        backlog.state = {"position": "2"}
        worker.run_once(now[0])
        assert len(backlog.cursors) == 2
        assert worker.catching_up and worker.next_delay_ms == CATCHUP_DELAY_MS


def test_rejected_catchup_batch_does_not_schedule_fast_pass(tmp_path: Path) -> None:
    root = tmp_path / "logs"
    root.mkdir()
    now = [NOW]
    reader = FakeReader()
    reader.more = True
    reader.state = {"position": "1"}
    source = UsageSource("h1", "kind-a", reader.format_id, root)
    with SqliteUsageStore(tmp_path / "state/usage.db", "UTC", clock=lambda: now[0]) as store:
        worker = UsageCollector(
            store, {reader.format_id: reader}, (source,), lambda: (), lambda: now[0]
        )
        worker.run_once(now[0])
        assert worker.catching_up
        reader.advance = lambda: now.__setitem__(0, now[0] + PASS_DEADLINE_MS + 1)
        reader.state = {"position": "2"}
        worker.run_once(now[0])
        assert not worker.catching_up and worker.next_delay_ms == IDLE_POLL_MS
        assert store.cursor(source.source_key).state == {"position": "1"}
        # The rejected batch left this idle source on its own cadence: it is read again when due.
        now[0] += IDLE_POLL_MS
        reader.advance = lambda: None
        reader.error = True
        worker.run_once(now[0])
        assert not worker.catching_up and worker.next_delay_ms == IDLE_POLL_MS


def test_stop_interrupts_catchup_gap(tmp_path: Path) -> None:
    import threading
    import time

    from relaydeck.usage.collector import CATCHUP_DELAY_MS

    root = tmp_path / "logs"
    root.mkdir()
    reader = FakeReader()
    reader.more = True
    reader.state = {"position": "1"}
    source = UsageSource("h1", "kind-a", reader.format_id, root)
    with SqliteUsageStore(tmp_path / "state/usage.db", "UTC", clock=lambda: NOW) as store:
        worker = UsageCollector(
            store, {reader.format_id: reader}, (source,), lambda: (), lambda: NOW
        )
        entered_gap = threading.Event()
        sleep = worker._sleep
        delays: list[float] = []

        def sleeping(seconds: float) -> object:
            delays.append(seconds)
            entered_gap.set()
            return sleep(seconds)

        worker._sleep = sleeping
        worker.start()
        worker.start()
        try:
            assert entered_gap.wait(2)
            assert delays == [CATCHUP_DELAY_MS / 1000]
            assert worker.catching_up
        finally:
            began = time.monotonic()
            worker.stop()
        assert time.monotonic() - began < 1
        assert worker._thread is not None and not worker._thread.is_alive()
        assert len(reader.cursors) == 1 and not worker.catching_up


def test_catchup_passes_remain_sequential(tmp_path: Path) -> None:
    import threading

    root = tmp_path / "logs"
    root.mkdir()
    reader = FakeReader()
    reader.more = True
    reader.events = (UsageEvent("s", "e", NOW, None, Counters(1), Basis.PER_CALL),)
    source = UsageSource("h1", "kind-a", reader.format_id, root)
    entered = threading.Event()
    release = threading.Event()
    second_entered = threading.Event()

    def reading() -> None:
        reader.events = (
            UsageEvent("s", str(len(reader.cursors)), NOW, None, Counters(1), Basis.PER_CALL),
        )
        if len(reader.cursors) == 1:
            entered.set()
            assert release.wait(2)
        else:
            second_entered.set()

    reader.advance = reading
    with SqliteUsageStore(tmp_path / "state/usage.db", "UTC", clock=lambda: NOW) as store:
        worker = UsageCollector(
            store, {reader.format_id: reader}, (source,), lambda: (), lambda: NOW
        )
        failures: list[Exception] = []

        def run() -> None:
            try:
                worker.run_once(NOW)
            except Exception as exc:
                failures.append(exc)

        first, second = threading.Thread(target=run), threading.Thread(target=run)
        first.start()
        try:
            assert entered.wait(2)
            second.start()
            assert not second_entered.wait(0.05)
        finally:
            release.set()
            first.join(2)
            if second.ident is not None:
                second.join(2)
        assert not first.is_alive() and not second.is_alive() and not failures
        assert second_entered.is_set() and worker.catching_up
        assert [cursor.revision for cursor in reader.cursors] == [0, 1]


def test_replayed_events_with_more_back_off_instead_of_catching_up(tmp_path: Path) -> None:
    root = tmp_path / "logs"
    root.mkdir()
    now = [NOW]
    reader = FakeReader()
    reader.more = True
    reader.events = (UsageEvent("s", "e", NOW, None, Counters(1), Basis.PER_CALL),)
    source = UsageSource("h1", "kind-a", reader.format_id, root)
    with SqliteUsageStore(tmp_path / "state/usage.db", "UTC", clock=lambda: now[0]) as store:
        worker = UsageCollector(
            store, {reader.format_id: reader}, (source,), lambda: (), lambda: now[0]
        )
        worker.run_once(now[0])
        assert worker.catching_up and worker.next_delay_ms == 250
        now[0] += 250
        worker.run_once(now[0])
        assert not worker.catching_up and worker.next_delay_ms == IDLE_POLL_MS
        now[0] += IDLE_POLL_MS
        worker.run_once(now[0])
        assert not worker.catching_up and worker.next_delay_ms == IDLE_POLL_MS * 2


def test_collector_trusts_apply_result_without_a_second_cursor_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from relaydeck.usage.contracts import ApplyResult

    root = tmp_path / "logs"
    root.mkdir()
    reader = FakeReader()
    reader.more = True
    reader.state = {"position": "1"}
    reader.events = (UsageEvent("s", "e", NOW, None, Counters(1), Basis.PER_CALL),)
    source = UsageSource("h1", "kind-a", reader.format_id, root)
    with SqliteUsageStore(tmp_path / "state/usage.db", "UTC", clock=lambda: NOW) as store:
        cursor_reads: list[str] = []
        original = store.cursor

        def cursor(key: str) -> Cursor:
            cursor_reads.append(key)
            return original(key)

        monkeypatch.setattr(store, "cursor", cursor)
        monkeypatch.setattr(store, "apply", lambda *args, **kwargs: ApplyResult())
        worker = UsageCollector(
            store, {reader.format_id: reader}, (source,), lambda: (), lambda: NOW
        )
        worker.run_once(NOW)
        assert not worker.catching_up and worker.next_delay_ms == IDLE_POLL_MS
        assert cursor_reads == [source.source_key]


def test_enumeration_gap_is_backed_off_beside_a_catchup_source(tmp_path: Path) -> None:
    root = tmp_path / "logs"
    root.mkdir()
    now = [NOW]
    backlog, gap = FakeReader(), FakeReader()
    backlog.more = True
    gap.format_id = "format-b"
    gap.coverage = Coverage.PARTIAL
    gap.retry_blocked = True  # a real incomplete enumeration is an obstruction (_files.py)
    sources = (
        UsageSource("h1", "kind-a", backlog.format_id, root),
        UsageSource("h1", "kind-b", gap.format_id, root),
    )
    with SqliteUsageStore(tmp_path / "state/usage.db", "UTC", clock=lambda: now[0]) as store:
        worker = UsageCollector(
            store, {r.format_id: r for r in (backlog, gap)}, sources, lambda: (), lambda: now[0]
        )
        for position in range(1, 5):
            backlog.state = {"position": str(position)}
            worker.run_once(now[0])
            assert worker.catching_up and worker.next_delay_ms == 250
            now[0] += 250
        assert len(gap.cursors) == 1
        now[0] = NOW + IDLE_POLL_MS
        backlog.state = {"position": "5"}
        worker.run_once(now[0])
        assert len(gap.cursors) == 2
        now[0] += IDLE_POLL_MS
        backlog.more = False
        gap.coverage = Coverage.COMPLETE
        worker.run_once(now[0])
        assert len(gap.cursors) == 2  # Second gap's exponential delay is still active.
        now[0] += IDLE_POLL_MS
        worker.run_once(now[0])
        assert len(gap.cursors) == 3 and not worker.catching_up
