"""Historical gaps never back off; progress clears failures; a current
obstruction or a zero-progress backlog backs off; an unavailable pass applies exactly once.

Synthetic readers only.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from relaydeck.usage.collector import CATCHUP_DELAY_MS, UsageCollector
from relaydeck.usage.contracts import (
    ACTIVE_POLL_MS,
    IDLE_POLL_MS,
    Basis,
    Batch,
    Budget,
    Counters,
    Coverage,
    Cursor,
    UsageEvent,
    UsageSource,
)
from relaydeck.usage.store import SqliteUsageStore

NOW = 1_791_700_000_000


class Reader:
    """A synthetic reader whose batch flags the test sets directly."""

    format_id = "format-a"

    def __init__(self) -> None:
        self.calls = 0
        self.error = False
        self.coverage = Coverage.COMPLETE
        self.more = False
        self.retry_blocked = False
        self.events: tuple[UsageEvent, ...] = ()
        self.state: dict[str, str] | None = None

    def read(self, root: Path, cursor: Cursor, budget: Budget) -> Batch:
        self.calls += 1
        if self.error:
            raise ValueError("synthetic failure")
        state = cursor.state if self.state is None else self.state
        return Batch(
            self.events,
            Cursor(cursor.files, state, cursor.revision),
            self.coverage,
            cursor.revision,
            more=self.more,
            retry_blocked=self.retry_blocked,
        )


def _worker(
    tmp_path: Path,
    reader: Reader,
    now: list[int],
    store: SqliteUsageStore,
    sources: tuple[UsageSource, ...] | None = None,
    readers: dict[str, Any] | None = None,
) -> UsageCollector:
    root = tmp_path / "logs"
    root.mkdir(exist_ok=True)
    chosen = sources or (UsageSource("h1", "kind-a", reader.format_id, root),)
    return UsageCollector(
        store,
        readers or {reader.format_id: reader},
        chosen,
        lambda: (),
        lambda: now[0],
        lambda s: None,
    )


def _event(key: str) -> UsageEvent:
    return UsageEvent("s", key, NOW, None, Counters(1), Basis.PER_CALL)


def test_historical_partial_without_progress_is_polled_at_the_normal_interval(
    tmp_path: Path,
) -> None:
    """Old logic: PARTIAL without `more` set a back-off (30 s, 60 s, ...). Now: ordinary polling."""
    now = [NOW]
    reader = Reader()
    reader.coverage = Coverage.PARTIAL
    reader.state = {"gap": "1"}
    with SqliteUsageStore(tmp_path / "state/usage.db", "UTC", clock=lambda: now[0]) as store:
        worker = _worker(tmp_path, reader, now, store)
        for _ in range(3):
            worker.run_once(now[0])
            assert worker.next_delay_ms == IDLE_POLL_MS
            assert not worker.catching_up
            now[0] += IDLE_POLL_MS
        assert reader.calls == 3  # read on every normal pass, never skipped
        assert worker.coverage["kind-a"].coverage == Coverage.PARTIAL  # honest, not complete


def test_progress_with_a_historical_gap_clears_the_failures(tmp_path: Path) -> None:
    """Old logic: a PARTIAL progressing batch kept the back-off (it was never cleared).
    Now: progress clears it, so the next pass uses the normal interval."""
    now = [NOW]
    reader = Reader()
    with SqliteUsageStore(tmp_path / "state/usage.db", "UTC", clock=lambda: now[0]) as store:
        worker = _worker(tmp_path, reader, now, store)
        reader.error = True
        worker.run_once(now[0])
        now[0] += IDLE_POLL_MS
        worker.run_once(now[0])
        assert worker.next_delay_ms == 60_000  # two failures: backing off
        now[0] += 60_000
        reader.error = False
        reader.coverage = Coverage.PARTIAL
        reader.events = (_event("e1"),)
        reader.state = {"gap": "1"}
        worker.run_once(now[0])
        assert worker.next_delay_ms == ACTIVE_POLL_MS  # produced events, no back-off left


def test_progress_with_more_catches_up_at_the_fast_interval(tmp_path: Path) -> None:
    now = [NOW]
    reader = Reader()
    reader.more = True
    reader.events = (_event("e1"),)
    reader.state = {"position": "1"}
    with SqliteUsageStore(tmp_path / "state/usage.db", "UTC", clock=lambda: now[0]) as store:
        worker = _worker(tmp_path, reader, now, store)
        worker.run_once(now[0])
        assert worker.catching_up and worker.next_delay_ms == CATCHUP_DELAY_MS == 250


def test_zero_progress_with_more_backs_off(tmp_path: Path) -> None:
    now = [NOW]
    reader = Reader()
    reader.more = True
    with SqliteUsageStore(tmp_path / "state/usage.db", "UTC", clock=lambda: now[0]) as store:
        worker = _worker(tmp_path, reader, now, store)
        worker.run_once(now[0])
        assert worker.next_delay_ms == 30_000 and not worker.catching_up


def test_zero_progress_with_retry_blocked_backs_off_then_recovers(tmp_path: Path) -> None:
    """A current obstruction in this pass (retry_blocked) with no progress backs off; the back-off
    grows: 30 s, then 60 s."""
    now = [NOW]
    reader = Reader()
    reader.coverage = Coverage.PARTIAL
    reader.retry_blocked = True
    with SqliteUsageStore(tmp_path / "state/usage.db", "UTC", clock=lambda: now[0]) as store:
        worker = _worker(tmp_path, reader, now, store)
        worker.run_once(now[0])
        assert worker.next_delay_ms == 30_000
        now[0] += 30_000
        worker.run_once(now[0])
        assert worker.next_delay_ms == 60_000
        reader.retry_blocked = False
        now[0] += 60_000
        worker.run_once(now[0])
        assert worker.next_delay_ms == IDLE_POLL_MS  # obstruction gone: back to normal


def test_a_backed_off_source_does_not_delay_another_source_catching_up(tmp_path: Path) -> None:
    now = [NOW]
    blocked, catching = Reader(), Reader()
    blocked.format_id, catching.format_id = "format-a", "format-b"
    blocked.retry_blocked = True
    blocked.coverage = Coverage.PARTIAL
    catching.more = True
    catching.events = (_event("e1"),)
    catching.state = {"position": "1"}
    root = tmp_path / "logs"
    root.mkdir()
    sources = (
        UsageSource("h1", "kind-a", blocked.format_id, root),
        UsageSource("h1", "kind-b", catching.format_id, root),
    )
    with SqliteUsageStore(tmp_path / "state/usage.db", "UTC", clock=lambda: now[0]) as store:
        worker = UsageCollector(
            store,
            {blocked.format_id: blocked, catching.format_id: catching},
            sources,
            lambda: (),
            lambda: now[0],
            lambda s: None,
        )
        worker.run_once(now[0])
        assert worker.catching_up and worker.next_delay_ms == CATCHUP_DELAY_MS
        now[0] += CATCHUP_DELAY_MS
        worker.run_once(now[0])
        assert blocked.calls == 1  # skipped while backing off
        assert catching.calls == 2  # the other source kept its fast pace


def test_an_unavailable_pass_applies_exactly_once(tmp_path: Path) -> None:
    """Old logic: a committed UNAVAILABLE batch was raised as an error and recorded a second failure
    marker (revision +2). Now: one committed batch, revision +1."""
    now = [NOW]
    reader = Reader()
    reader.coverage = Coverage.UNAVAILABLE
    reader.retry_blocked = True
    with SqliteUsageStore(tmp_path / "state/usage.db", "UTC", clock=lambda: now[0]) as store:
        worker = _worker(tmp_path, reader, now, store)
        before = store.cursor("h1/kind-a/format-a").revision
        worker.run_once(now[0])
        assert store.cursor("h1/kind-a/format-a").revision == before + 1
        assert worker.coverage["kind-a"].coverage == Coverage.UNAVAILABLE


def test_an_unavailable_checkpoint_is_saved_and_applied_once(tmp_path: Path) -> None:
    """A deliberate UNAVAILABLE checkpoint (tokenless data) must still be stored, exactly once."""
    now = [NOW]
    reader = Reader()
    reader.coverage = Coverage.UNAVAILABLE  # no retry_blocked: a reader's own checkpoint
    reader.state = {"checkpoint": "doc-1"}
    with SqliteUsageStore(tmp_path / "state/usage.db", "UTC", clock=lambda: now[0]) as store:
        worker = _worker(tmp_path, reader, now, store)
        before = store.cursor("h1/kind-a/format-a").revision
        worker.run_once(now[0])
        saved = store.cursor("h1/kind-a/format-a")
        assert saved.state.get("checkpoint") == "doc-1"
        assert saved.revision == before + 1
        assert worker.next_delay_ms == IDLE_POLL_MS  # no obstruction: an ordinary interval


def test_a_failure_before_any_apply_records_exactly_one_marker(tmp_path: Path) -> None:
    now = [NOW]
    reader = Reader()
    reader.error = True
    with SqliteUsageStore(tmp_path / "state/usage.db", "UTC", clock=lambda: now[0]) as store:
        worker = _worker(tmp_path, reader, now, store)
        before = store.cursor("h1/kind-a/format-a").revision
        worker.run_once(now[0])
        assert store.cursor("h1/kind-a/format-a").revision == before + 1
        assert worker.next_delay_ms == 30_000


def test_a_real_historical_gap_reader_is_not_backed_off_by_the_collector(tmp_path: Path) -> None:
    """Real reader and collector: a malformed record leaves a permanent gap (PARTIAL) with no new
    bytes. The source keeps polling at the normal interval. Old logic: back-off 30 s, then 60 s."""
    from relaydeck.usage.readers import READERS

    root = tmp_path / "logs"
    path = root / "p" / "s.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text("{not json\n")
    now = [NOW]
    reader = READERS["jsonl-message-usage-v1"]
    source = UsageSource("h1", "kind-a", reader.format_id, root)
    with SqliteUsageStore(tmp_path / "state/usage.db", "UTC", clock=lambda: now[0]) as store:
        worker = UsageCollector(
            store, {reader.format_id: reader}, (source,), lambda: (), lambda: now[0], lambda s: None
        )
        for _ in range(3):
            worker.run_once(now[0])
            assert worker.next_delay_ms == IDLE_POLL_MS
            now[0] += IDLE_POLL_MS
        assert worker.coverage["kind-a"].coverage == Coverage.PARTIAL
