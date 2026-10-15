"""Shared store-builder test helpers (moved out of test modules)."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from relaydeck.usage.contracts import (
    Basis,
    Batch,
    Counters,
    Coverage,
    LimitSnapshot,
    SessionMapping,
    UsageEvent,
    UsageQuery,
    UsageReport,
)
from relaydeck.usage.store import SqliteUsageStore

SOURCE = "h1/kind-a/format-a"
NOW = int(datetime(2026, 10, 11, tzinfo=ZoneInfo("UTC")).timestamp() * 1000)


def event(
    key: str = "e1",
    *,
    t: int = NOW,
    session: str = "s1",
    model: str = "model-a",
    verified: bool = True,
) -> UsageEvent:
    return UsageEvent(
        session, key, t, model, Counters(10, output=2), Basis.PER_CALL, accounting_verified=verified
    )


def mapping(
    space: str = "w1",
    interval: str = "i1",
    first: int = NOW - 1000,
    last: int = NOW + 1000,
    session: str = "s1",
) -> SessionMapping:
    return SessionMapping(SOURCE, "kind-a", session, interval, "h1", space, first, last)


def write(
    store: SqliteUsageStore,
    events: tuple[UsageEvent, ...],
    maps: tuple[SessionMapping, ...] = (mapping(),),
    limits: tuple[LimitSnapshot, ...] = (),
) -> Batch:
    cursor = store.cursor(SOURCE)
    batch = Batch(events, cursor, Coverage.COMPLETE, cursor.revision, limits)
    store.apply("kind-a", batch, maps, source_key=SOURCE)
    return batch


def report(
    store: SqliteUsageStore, first: str = "2026-10-11", last: str = "2026-10-11", **kw: str
) -> UsageReport:
    return store.query(UsageQuery("h1", first, last, **kw), NOW)
