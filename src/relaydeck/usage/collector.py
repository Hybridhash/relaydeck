"""Bounded, injected format collection; no transcript-aware logging or imports."""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from relaydeck.usage.contracts import (
    ACTIVE_POLL_MS,
    IDLE_POLL_MS,
    MAX_KEY_CHARS,
    MAX_SOURCES,
    PASS_DEADLINE_MS,
    RETENTION_DAYS,
    ApplyResult,
    Batch,
    Budget,
    Coverage,
    KindCoverage,
    LimitsReader,
    MappingGap,
    PaneCwdObservation,
    Reader,
    SessionMapping,
    UsageQuery,
    UsageSource,
    UsageStore,
    UsageUnavailable,
)
from relaydeck.usage.limits import LIMITS_READERS

logger = logging.getLogger(__name__)
CATCHUP_DELAY_MS = 250
DUTY_WINDOW_MS = 10_000
DUTY_MAX = 0.20
DUTY_FLOOR_MS = 1_000
UNMAPPED_REFRESH_MS = 60_000
_COVERAGE = {
    Coverage.COMPLETE: 0,
    Coverage.PARTIAL: 1,
    Coverage.UNVERIFIED: 2,
    Coverage.UNAVAILABLE: 3,
}


class UsageCollector:
    """clock returns epoch milliseconds; sleep accepts seconds.

    Readers must cooperate with Budget deadlines. Stop interrupts the default sleep;
    an injected sleep must also return promptly. No replacement worker starts while
    an earlier worker is still alive, even if a reader violates its deadline.

    Catch-up keeps the cooperative 750 ms pass deadline and at least 250 ms between
    sequential passes: about 75% duty of one thread at the reading budget limit.
    SQLite commits/bookkeeping can extend a pass; this is not a hard CPU quota.
    Process scheduling priority is unchanged (os.nice would affect the entire process).
    """

    def __init__(
        self,
        store: UsageStore,
        readers: Mapping[str, Reader],
        sources: tuple[UsageSource, ...],
        mappings: Callable[[], tuple[SessionMapping, ...]],
        clock: Callable[[], int] | None = None,
        sleep: Callable[[float], object] | None = None,
        *,
        limits_readers: Mapping[str, LimitsReader] | None = None,
        mapping_gaps: Callable[[], tuple[MappingGap, ...]] = lambda: (),
        acknowledge_mappings: Callable[
            [str, tuple[SessionMapping, ...], tuple[MappingGap, ...]], None
        ] = lambda source, rows, gaps: None,
        digest: Callable[[str], str | None] | None = None,
        cwd_observations: Callable[[], tuple[PaneCwdObservation, ...]] = lambda: (),
        acknowledge_cwd: Callable[[str, tuple[PaneCwdObservation, ...]], None] = lambda s, o: None,
        inferred_linking: bool = True,
    ) -> None:
        if len(sources) > MAX_SOURCES or len({s.source_key for s in sources}) != len(sources):
            raise UsageUnavailable()
        for source in sources:
            if (
                not re.fullmatch(r"[A-Za-z0-9_.-]+", source.kind)
                or len(source.kind) > MAX_KEY_CHARS
            ):
                raise UsageUnavailable()
        self.store, self.readers, self.sources, self.mappings = store, readers, sources, mappings
        self.mapping_gaps, self.acknowledge_mappings = mapping_gaps, acknowledge_mappings
        self._digest = digest
        self.cwd_observations, self.acknowledge_cwd = cwd_observations, acknowledge_cwd
        self._inferred_linking = inferred_linking
        self.limits_readers = LIMITS_READERS if limits_readers is None else limits_readers
        from relaydeck.usage.limits.json_statusline_limits import JsonStatuslineLimitsReader

        self._source_limits = {
            s.source_key: JsonStatuslineLimitsReader(s.limits_cache)
            for s in sources
            if s.limits_format == JsonStatuslineLimitsReader.format_id
            and s.limits_cache is not None
        }
        self._clock = clock or (lambda: int(time.time() * 1000))
        self._stop = threading.Event()
        self._sleep = sleep or self._stop.wait
        self._lifecycle = threading.Lock()
        self._pass = threading.Lock()
        self._thread: threading.Thread | None = None
        self._failures: dict[str, int] = {}
        self._retry_at: dict[str, int] = {}
        # Each source's own next due time: normal cadence, separate from failure back-off.
        self._due: dict[str, int] = {}
        # The mapping set last applied per source: a change makes the source due at once (handoff).
        self._mapping_seen: dict[str, tuple[Any, ...]] = {}
        self._cwd_seen: dict[str, tuple[Any, ...]] = {}
        self._catchup_sources: set[str] = set()
        self._source_coverage: dict[str, KindCoverage] = {}
        self.coverage: dict[str, KindCoverage] = {}
        self.next_delay_ms = IDLE_POLL_MS
        self._last_prune: int | None = None
        self._offset = 0
        self._unmapped_counts: dict[str, int] = {}
        self._last_unmapped_refresh: int | None = None
        self._busy_window: deque[tuple[float, int]] = deque()
        self._backfill_digest: str | None = None

    @property
    def catching_up(self) -> bool:
        """Whether a successfully committed, progressing source still has history."""
        return not self._stop.is_set() and bool(self._catchup_sources)

    def start(self) -> None:
        with self._lifecycle:
            if self._thread is not None and self._thread.is_alive():
                if self._stop.is_set():
                    raise UsageUnavailable("busy")
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="usage-collector", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        with self._lifecycle:
            self._stop.set()
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2)

    def join(self) -> None:
        """Final shutdown fence, called off the event loop before closing the ledger."""
        with self._lifecycle:
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join()

    def _run(self) -> None:
        while not self._stop.is_set():
            self.run_once(self._clock())
            if not self._stop.is_set():
                self._sleep(self.next_delay_ms / 1000)

    @staticmethod
    def _root(root: Path) -> tuple[Path, tuple[int, int]]:
        if not root.is_absolute() or not root.is_dir():
            raise UsageUnavailable()
        if any(p.is_symlink() for p in (root, *root.parents)) or root.resolve(strict=True) != root:
            raise UsageUnavailable()
        info = root.stat()
        if info.st_uid != os.getuid():
            raise UsageUnavailable()
        return root, (info.st_dev, info.st_ino)

    def _coverage(self, source: UsageSource, value: Coverage, now: int) -> None:
        old = self._source_coverage.get(source.source_key)
        observed = now if value != Coverage.UNAVAILABLE else (old.observed_ms if old else None)
        self._source_coverage[source.source_key] = KindCoverage(source.kind, value, observed)
        rows = [row for row in self._source_coverage.values() if row.kind == source.kind]
        times = [row.observed_ms for row in rows if row.observed_ms is not None]
        self.coverage[source.kind] = KindCoverage(
            source.kind,
            max((row.coverage for row in rows), key=_COVERAGE.__getitem__),
            max(times) if times else None,
            self._unmapped_counts.get(source.kind, 0),
        )

    def _record_failure(self, source: UsageSource, value: Coverage) -> None:
        try:
            cursor = self.store.cursor(source.source_key)
            self.store.apply(
                source.kind,
                Batch((), cursor, value, cursor.revision),
                (),
                source_key=source.source_key,
            )
        except Exception:
            pass  # Retain in-process coverage; unavailable storage cannot acknowledge a pass.

    def _backoff(self, source_key: str, now_ms: int) -> None:
        failures = self._failures.get(source_key, 0) + 1
        self._failures[source_key] = failures
        self._retry_at[source_key] = now_ms + min(300_000, IDLE_POLL_MS * 2 ** min(failures - 1, 4))

    def _clear_backoff(self, source_key: str) -> None:
        self._failures.pop(source_key, None)
        self._retry_at.pop(source_key, None)

    def _schedule(self, source_key: str, result: ApplyResult, batch: Batch, now_ms: int) -> None:
        """Scheduling after one committed batch. Progress is a changed cursor or changed
        events, never a revision bump or a count of returned events. Historical gaps and PARTIAL
        coverage alone never back off: they are reported, and the source keeps its normal
        interval."""
        progress = result.events_changed > 0 or result.cursor_changed
        if progress:
            self._clear_backoff(source_key)
            if batch.more:
                self._catchup_sources.add(source_key)
            else:
                self._catchup_sources.discard(source_key)
            return
        self._catchup_sources.discard(source_key)
        if batch.more or batch.retry_blocked:
            # Known backlog with no progress, or an obstruction met in this pass: back off.
            self._backoff(source_key, now_ms)
        else:
            self._clear_backoff(source_key)

    def _limits_pass(self, source: UsageSource, now: int, remaining: int) -> None:
        reader = self._source_limits.get(source.source_key) or self.limits_readers.get(
            source.limits_format or ""
        )
        if reader is None or reader.format_id != source.limits_format or remaining <= 0:
            return
        key = f"{source.host_id}/{source.kind}/limits:{source.format}"
        try:
            cursor = self.store.cursor(key)
            started = time.monotonic()
            budget = Budget(
                max_bytes=64 * 1024, max_files=1, max_events=2, deadline_ms=min(50, remaining)
            )
            snapshots, following = reader.read(source.root, source.kind, cursor, budget, now)
            if (time.monotonic() - started) * 1000 > budget.deadline_ms or len(snapshots) > 1:
                return
            safe = tuple(
                replace(s, kind=source.kind) for s in snapshots if s.kind in ("", source.kind)
            )
            self.store.apply(
                source.kind,
                Batch((), following, Coverage.COMPLETE, cursor.revision, safe),
                (),
                source_key=key,
            )
        except Exception:
            pass  # Keep the previous observation; failure never refreshes its capture time.

    def _unmapped_coverage(self, now: int) -> None:
        """Use the same retained verified events as report NOT_LINKED rows (addendum C)."""
        if (
            self._last_unmapped_refresh is not None
            and 0 <= now - self._last_unmapped_refresh < UNMAPPED_REFRESH_MS
        ):
            return
        # Throttle attempts too; failed storage must not turn catch-up into repeated scans.
        self._last_unmapped_refresh = now
        try:
            zone = ZoneInfo(getattr(self.store, "timezone", "UTC"))
            last = datetime.fromtimestamp(now / 1000, zone).date()
            first = last - timedelta(days=RETENTION_DAYS - 1)
            counts: dict[str, int] = {}
            counter = getattr(self.store, "unmapped_sessions", None)
            for host in sorted({s.host_id for s in self.sources}):
                if callable(counter):
                    values = counter(
                        host,
                        first.isoformat(),
                        last.isoformat(),
                        now,
                        inferred=self._inferred_linking,
                    )
                else:
                    # Protocol-only stores keep the existing fallback, once a minute.
                    report = self.store.query(
                        UsageQuery(host, first.isoformat(), last.isoformat()), now
                    )
                    values = {row.kind: row.unmapped_sessions for row in report.coverage}
                for kind, count in values.items():
                    counts[kind] = counts.get(kind, 0) + count
            self._unmapped_counts = counts
            for kind, row in self.coverage.items():
                self.coverage[kind] = replace(row, unmapped_sessions=counts.get(kind, 0))
        except Exception:
            pass  # Unavailable storage does not invent a zero count.

    def _mapping_pending(self, source: UsageSource) -> bool:
        """True while this source has unacknowledged mapping gaps, or its mappings differ from the
        set
        last applied. Such a source is due at once, so a durable handoff closes promptly."""
        try:
            if any(g.source_key == source.source_key for g in self.mapping_gaps()):
                return True
            seen = self._mapping_seen.get(source.source_key)
            if seen is not None and seen != tuple(self.mappings()):
                return True
            cwd_seen = self._cwd_seen.get(source.source_key)
            if cwd_seen is None:
                return False
            current = tuple(o for o in self.cwd_observations() if o.source_key == source.source_key)
            return cwd_seen != current
        except Exception:
            return False  # unavailable mapping data never forces a read; the normal cadence holds

    def run_once(self, now_ms: int) -> int:
        with self._pass:
            start = self._clock()
            wall = time.monotonic()
            produced = 0
            ordered = self.sources[self._offset :] + self.sources[: self._offset]
            self._offset = (self._offset + 1) % len(self.sources) if self.sources else 0
            for source in ordered:
                if self._stop.is_set() and threading.current_thread() is self._thread:
                    break
                remaining = PASS_DEADLINE_MS - max(
                    self._clock() - start, int((time.monotonic() - wall) * 1000)
                )
                self._limits_pass(source, now_ms, remaining)
                # A source that is not due is not read at all: an idle historical source never
                # rides along on another source's fast catch-up pass.
                if self._retry_at.get(source.source_key, 0) > now_ms:
                    continue  # a failure back-off always holds
                if (
                    source.source_key not in self._catchup_sources
                    and self._due.get(source.source_key, 0) > now_ms
                    and not self._mapping_pending(source)
                ):
                    continue  # not due on its own cadence, and no mapping handoff is waiting
                remaining = PASS_DEADLINE_MS - max(
                    self._clock() - start, int((time.monotonic() - wall) * 1000)
                )
                if remaining <= 0:
                    # No reader batch replaced this source's last progressing backlog.
                    self._coverage(source, Coverage.PARTIAL, now_ms)
                    self._record_failure(source, Coverage.PARTIAL)
                    continue  # it was never read: it stays due, so the next pass reads it
                began = self._clock()
                source_wall = time.monotonic()
                count, skipped, value = 0, 0, Coverage.UNAVAILABLE
                applied = False  # a batch of this pass was committed to the store
                handoff = False  # this pass changed or left mapping gaps: due at once
                try:
                    reader = self.readers.get(source.format)
                    if reader is None or reader.format_id != source.format:
                        raise UsageUnavailable()
                    root, identity = self._root(source.root)
                    cursor = self.store.cursor(source.source_key)
                    budget = Budget(deadline_ms=remaining, digest=self._digest)
                    batch = reader.read(root, cursor, budget)
                    if self._root(root)[1] != identity:
                        raise UsageUnavailable()
                    elapsed = max(self._clock() - start, int((time.monotonic() - wall) * 1000))
                    if elapsed > PASS_DEADLINE_MS or len(batch.events) > budget.max_events:
                        self._catchup_sources.discard(source.source_key)
                        value = Coverage.PARTIAL
                        skipped = batch.skipped
                        self._record_failure(source, value)
                    else:
                        limits = tuple(
                            replace(s, kind=source.kind)
                            for s in batch.limits
                            if s.kind in ("", source.kind)
                        )
                        conflicts = len(batch.limits) - len(limits)
                        value = max(
                            batch.coverage,
                            Coverage.PARTIAL if conflicts else Coverage.COMPLETE,
                            key=_COVERAGE.__getitem__,
                        )
                        mappings = self.mappings()
                        gaps = tuple(
                            g for g in self.mapping_gaps() if g.source_key == source.source_key
                        )
                        cwd = tuple(
                            o for o in self.cwd_observations() if o.source_key == source.source_key
                        )
                        value = max(
                            value,
                            Coverage.UNVERIFIED if gaps else Coverage.COMPLETE,
                            key=_COVERAGE.__getitem__,
                        )
                        batch = replace(
                            batch,
                            limits=limits,
                            coverage=value,
                            skipped=batch.skipped + conflicts,
                            mapping_gaps=gaps,
                        )
                        result = self.store.apply(
                            source.kind,
                            batch,
                            mappings,
                            source_key=source.source_key,
                            cwd=cwd,
                        )
                        applied = True
                        # Handoff: a mapping change or an unacknowledged gap makes this source due
                        # at once,
                        # so the durable handoff is confirmed on the next pass.
                        before = self._mapping_seen.get(source.source_key)
                        handoff = bool(gaps) or (before is not None and before != tuple(mappings))
                        self._mapping_seen[source.source_key] = tuple(mappings)
                        self.acknowledge_mappings(source.source_key, mappings, gaps)
                        cwd_before = self._cwd_seen.get(source.source_key)
                        handoff = handoff or (cwd_before is not None and cwd_before != cwd)
                        self._cwd_seen[source.source_key] = cwd
                        self.acknowledge_cwd(source.source_key, cwd)
                        count, skipped = len(batch.events), batch.skipped
                        produced += count
                        self._schedule(source.source_key, result, batch, now_ms)
                except Exception:
                    # An operational failure: back off, and record one failure marker only when no
                    # batch of this pass was committed (a committed batch already holds the state).
                    self._catchup_sources.discard(source.source_key)
                    value = Coverage.UNAVAILABLE
                    self._backoff(source.source_key, now_ms)
                    if not applied:
                        self._record_failure(source, value)
                self._coverage(source, value, now_ms)
                # Normal cadence per source: 250 ms only while it is catching up; otherwise the
                # active
                # or idle interval. A back-off (retry_at) still wins when it is later.
                if source.source_key in self._catchup_sources:
                    normal = CATCHUP_DELAY_MS
                elif count:
                    normal = ACTIVE_POLL_MS
                else:
                    normal = IDLE_POLL_MS
                self._due[source.source_key] = now_ms if handoff else now_ms + normal
                logger.info(
                    "usage_pass kind=%s events=%d coverage=%s skipped=%d ms=%d",
                    source.kind,
                    count,
                    value,
                    skipped,
                    max(0, self._clock() - began, int((time.monotonic() - source_wall) * 1000)),
                )
            if self._last_prune is None or now_ms - self._last_prune >= 3600_000:
                self._last_prune = now_ms
                try:
                    self.store.prune(now_ms)
                except Exception:
                    pass
            self._unmapped_coverage(now_ms)
            # The next wake-up is the earliest source due time; a source with no due time yet
            # (stopped before its first pass) is due now.
            dues = [
                max(self._due.get(s.source_key, 0), self._retry_at.get(s.source_key, 0))
                for s in self.sources
            ]
            self.next_delay_ms = max(CATCHUP_DELAY_MS, min(dues) - now_ms) if dues else IDLE_POLL_MS
            # Duty budget: at most ~20% of wall time over 10 s.
            busy_ms = int((time.monotonic() - wall) * 1000)
            self._busy_window.append((time.monotonic(), busy_ms))
            while (
                self._busy_window
                and (time.monotonic() - self._busy_window[0][0]) * 1000 > DUTY_WINDOW_MS
            ):
                self._busy_window.popleft()
            duty = sum(b for _, b in self._busy_window) / DUTY_WINDOW_MS
            if duty > DUTY_MAX:
                self.next_delay_ms = max(self.next_delay_ms, DUTY_FLOOR_MS)
            else:
                backfill = getattr(self.store, "backfill_prices", None)
                digest_fn = getattr(self.store, "price_digest", None)
                if callable(backfill) and callable(digest_fn):
                    try:
                        if digest_fn() != self._backfill_digest:
                            _, remaining = backfill(1000)
                            if not remaining:
                                self._backfill_digest = digest_fn()
                    except Exception:
                        pass
            return produced
