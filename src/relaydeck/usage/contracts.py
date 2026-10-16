"""Versioned, Flet-free contracts for local agent token usage.

Readers are named by log FORMAT, never by product; the owner's companion.toml maps each
configured agent kind to a format id and an allowlisted root folder.
Only numeric aggregates, opaque ids and timestamps cross the reader boundary: never
transcript text, prompts, tool arguments or filesystem paths.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from relaydeck.contracts.canonical import canonical_json

USAGE_VERSION = 1

# Approved retention and bounds; the collector enforces them, readers honour the budget.
RETENTION_DAYS = 90
MAX_SOURCES = 16
MAX_LINE_BYTES = 1024 * 1024
MAX_DOCUMENT_BYTES = 32 * 1024 * 1024  # whole-file JSON snapshots; larger => PARTIAL
MAX_CURSOR_STATE_ENTRIES = 512
MAX_CURSOR_STATE_BYTES = 128 * 1024
MAX_FILE_CONTEXT_BYTES = 64 * 1024  # per-file accounting context (FileCursor.context)
MAX_FILE_BYTES = 512 * 1024 * 1024
MAX_SCAN_BYTES_PER_PASS = 8 * 1024 * 1024
MAX_FILES_PER_PASS = 256
MAX_TRACKED_FILES = 20_000
MAX_CURSOR_BYTES = 8 * 1024 * 1024  # whole encoded cursor (files + state); see encoded_cursor_bytes
MAX_DIR_ENTRIES = 4096  # entries examined per listed directory
MAX_EXAMINED_ENTRIES = 50_000  # entries examined per enumeration pass
MAX_EVENTS_PER_BATCH = 4096
# per-event credit bound, enforced by credit readers and the store alike.
MAX_CREDITS_MILLI = 1_000_000_000
MAX_KEY_CHARS = 256
MAX_MODEL_CHARS = 128
PASS_DEADLINE_MS = 750
ACTIVE_POLL_MS = 3000
IDLE_POLL_MS = 30_000
MAX_QUERY_DAYS = RETENTION_DAYS
MAX_QUERY_ROWS = 512
CAPACITY_OK = "ok"
CAPACITY_EXCEEDED = "exceeded"
LIMIT_STALE_MS = 15 * 60 * 1000


class Basis(StrEnum):
    """How a reader derived an event's counters from the log."""

    PER_CALL = "per_call"  # one record per model call, deduplicated by event_key
    CUMULATIVE_DELTA = "cumulative_delta"  # difference of valid cumulative totals
    PER_TURN = "per_turn"  # keyed per-turn summary, replaced (not summed) on rewrite


class Coverage(StrEnum):
    COMPLETE = "complete"  # every tracked file read to its end
    PARTIAL = "partial"  # budget exhausted or a gap was skipped; more next pass
    UNVERIFIED = "unverified"  # format readable but accounting semantics not proven
    UNAVAILABLE = "unavailable"  # root missing, denied or unsafe


class RowScope(StrEnum):
    WORKSPACE = "workspace"  # a Herdr space
    OVERALL = "overall"  # all Herdr-linked usage; each session counted once
    NOT_LINKED = "not_linked"  # verified usage of sessions never linked to a Herdr pane


class LimitStatus(StrEnum):
    FRESH = "fresh"
    STALE = "stale"  # older than LIMIT_STALE_MS
    EXPIRED = "expired"  # a window's reset passed (snapshot: worst window): needs fresh evidence
    NOT_REPORTED = "not_reported"  # this format has no verified limit source


@dataclass(frozen=True)
class Counters:
    """Token counts in disjoint buckets; reasoning is a subset of output, never added.
    Cache writes with an unknown TTL go to `cache_write_unknown` (never priced as short)."""

    input_uncached: int = 0
    cache_read: int = 0
    cache_write_short: int = 0  # short-TTL cache creation (e.g. minutes)
    cache_write_long: int = 0  # long-TTL cache creation (e.g. an hour)
    output: int = 0
    reasoning: int = 0
    cache_write_unknown: int = 0

    def __post_init__(self) -> None:
        values = (
            self.input_uncached,
            self.cache_read,
            self.cache_write_short,
            self.cache_write_long,
            self.output,
            self.reasoning,
            self.cache_write_unknown,
        )
        if any(value < 0 for value in values) or self.reasoning > self.output:
            raise ValueError("invalid_counters")

    @property
    def total(self) -> int:
        return (
            self.input_uncached
            + self.cache_read
            + self.cache_write_short
            + self.cache_write_long
            + self.cache_write_unknown
            + self.output
        )

    def __add__(self, other: Counters) -> Counters:
        return Counters(
            self.input_uncached + other.input_uncached,
            self.cache_read + other.cache_read,
            self.cache_write_short + other.cache_write_short,
            self.cache_write_long + other.cache_write_long,
            self.output + other.output,
            self.reasoning + other.reasoning,
            self.cache_write_unknown + other.cache_write_unknown,
        )


@dataclass(frozen=True)
class UsageEvent:
    """One billable unit. `event_key` is stable across re-reads, replays and rewrites.
    Dedup namespace: (host_id, kind, source_key, session_key, event_key). A re-read with
    the same key replaces the earlier counters (subtracting its old contribution, even if
    day or model changed); absence from a rewritten file never deletes an event.
    `accounting_verified=False` (format readable, semantics unproven) keeps the event out
    of confirmed totals."""

    session_key: str = field(repr=False)  # opaque; matches a Herdr agent_session value
    event_key: str = field(repr=False)  # unique within session_key
    occurred_ms: int
    model: str | None
    counters: Counters
    basis: Basis
    parent_session_key: str | None = field(default=None, repr=False)  # forks/subagents
    accounting_verified: bool = True
    # keyed digest of the session's working folder (link_digests.cwd_digest),
    # or None when the format carries no folder or no key exists. Never a raw path.
    cwd_digest: str | None = field(default=None, repr=False)
    # the meter this event is accounted in. Token events carry counters;
    # credit events carry credits_milli with zero counters. Never converted.
    meter: str = "tokens"
    credits_milli: int = 0


@dataclass(frozen=True)
class LimitWindow:
    window_key: str  # stable per window within a quota (e.g. "primary"); opaque
    used_percent: float  # consumed share of the window, 0..100 (remaining = 100 - used)
    window_minutes: int | None = None  # never assume a fixed meaning for primary/secondary
    resets_at_ms: int | None = None
    status: LimitStatus = LimitStatus.FRESH  # per window: an expired window stays expired
    # Exact amounts behind the percent. `used_percent` is derived only when both are present and
    # limit > 0, else the window needs a native percent. used > limit clamps the percent to 100 and
    # sets `over_allowance`.
    used_amount: float | None = None
    limit_amount: float | None = None
    unit: str | None = None  # validated `[a-z_]{1,16}` (e.g. "credit")
    over_allowance: bool = False
    # The tool reported no use in this window yet: a reading of 0 %, not a failure.
    idle: bool = False


class LimitError(StrEnum):
    """Machine-readable fetch failure for a limits observation."""

    TIMEOUT = "timeout"
    AUTH_REQUIRED = "auth_required"
    UNAVAILABLE = "unavailable"
    PARSE = "parse"
    OFFLINE = "offline"


@dataclass(frozen=True)
class LimitSnapshot:
    """Latest observed allowance for one account quota; never summed across panes.
    Readers emit kind=""; the collector sets UsageSource.kind before apply and rejects a
    conflicting non-empty kind."""

    kind: str
    quota_key: str = field(repr=False)  # opaque per account/quota id from the format
    observed_ms: int
    windows: tuple[LimitWindow, ...] = ()
    status: LimitStatus = LimitStatus.FRESH  # worst of the windows
    # which provider plane produced this reading ("" for legacy ledger rows).
    source: str = ""
    # opaque account identity; None when unknown (never another account's data).
    account_ref: str | None = field(default=None, repr=False)
    # a LimitError value when this reading reports a failure, else None.
    error: str | None = None
    # sanitised plan label (<=32 chars, [A-Z0-9 ._-]) or None.
    plan_label: str | None = None


MAX_PLAN_LABEL_CHARS = 32
_PLAN_LABEL_KEPT = re.compile(r"[A-Z0-9 ._-]")
_UNIT_RE = re.compile(r"[a-z_]{1,16}\Z")


def sanitize_plan_label(value: object) -> str | None:
    """Uppercase, keep `[A-Z0-9 ._-]`, strip, truncate to 32 chars. None when
    nothing valid remains."""
    if not isinstance(value, str) or not value or len(value) > 256:
        return None
    kept = "".join(c for c in value.upper() if _PLAN_LABEL_KEPT.fullmatch(c)).strip()
    return kept[:MAX_PLAN_LABEL_CHARS] if kept else None


def sanitize_unit(value: object) -> str | None:
    """A validated amount unit (`[a-z_]{1,16}`), else None."""
    if not isinstance(value, str):
        return None
    return value if _UNIT_RE.fullmatch(value) else None


@dataclass(frozen=True)
class FileCursor:
    """Resume point for one file, at the end of the last COMPLETE record (a partial
    final line is re-read next pass, never stored). Identity (dev, ino) plus a hash of the
    first bytes detects rotation and same-size replacement; size < offset detects
    truncation. Whole-file JSON snapshots are re-parsed when size/mtime/digest change."""

    dev: int
    ino: int
    offset: int
    size: int
    mtime_ns: int
    head_digest: str  # hex SHA-256 of the first min(size, 4096) bytes
    # Reader-private per-file accounting context (e.g. current model, turn pins), validated
    # metadata only, <= MAX_FILE_CONTEXT_BYTES UTF-8; stored with the file entry, never content.
    context: str = field(default="", repr=False)


@dataclass(frozen=True)
class Cursor:
    """Per-source resume state, keyed by a reader-chosen opaque file key (never sent out).
    `state` holds validated accounting metadata only (e.g. last cumulative totals, current
    model), at most MAX_CURSOR_STATE_ENTRIES / MAX_CURSOR_STATE_BYTES; never content.
    `revision` increases by one on every successful apply (compare-and-swap)."""

    files: Mapping[str, FileCursor] = field(default_factory=dict, repr=False)
    state: Mapping[str, str] = field(default_factory=dict, repr=False)  # reader-private
    revision: int = 0


def encoded_state_bytes(state: Mapping[str, str]) -> int:
    """Size of Cursor.state as stored: compact sorted ASCII JSON, UTF-8. Readers and the
    store both enforce MAX_CURSOR_STATE_BYTES with this one calculation."""
    return len(canonical_json(dict(state), allow_nan=False).encode())


def encoded_cursor_bytes(cursor: Cursor) -> int:
    """Size of the whole cursor as stored (files map + state), one shared calculation.
    Readers reserve space for it before acknowledging events or offsets."""
    files = {
        key: [f.dev, f.ino, f.offset, f.size, f.mtime_ns, f.head_digest, f.context]
        for key, f in cursor.files.items()
    }
    return len(
        canonical_json(
            {"files": files, "state": dict(cursor.state)},
            allow_nan=False,
        ).encode()
    )


@dataclass(frozen=True)
class ApplyResult:
    """What a committed apply durably changed; retries and identical replays are not progress."""

    cursor_changed: bool = False  # stored files/state body differs from before
    events_changed: int = 0  # events newly inserted or with changed counters/day/model


@dataclass(frozen=True)
class Budget:
    max_bytes: int = MAX_SCAN_BYTES_PER_PASS
    max_files: int = MAX_FILES_PER_PASS
    max_events: int = MAX_EVENTS_PER_BATCH
    deadline_ms: int = PASS_DEADLINE_MS
    # A whole-file snapshot up to this size may exceed max_bytes, but only as the FIRST
    # unit of a pass (the pass then ends), so a large document always makes progress.
    max_document_bytes: int = MAX_DOCUMENT_BYTES
    # keyed working-folder digest (link_digests.cwd_digest bound to the install
    # key), or None when linking digests are unavailable. Readers stay pure: they call
    # this with a folder string and emit the digest, never opening config or the key.
    digest: Callable[[str], str | None] | None = None


@dataclass(frozen=True)
class Batch:
    """`expected_revision` is the revision of the cursor the reader started from; the
    store applies the batch only if it still matches, then stores next_cursor with
    revision + 1. An exact retry of an applied batch is a no-op; a stale one is rejected."""

    events: tuple[UsageEvent, ...]
    next_cursor: Cursor
    coverage: Coverage
    expected_revision: int
    limits: tuple[LimitSnapshot, ...] = ()
    skipped: int = 0  # malformed/oversized/denied units, counted but never described
    # True only for a resumable backlog (discovery, read or event budget ran out); False
    # for tails awaiting growth, permanent gaps or capacity blocks. Independent of coverage.
    more: bool = False
    # True only for an obstruction met in THIS pass (unreadable file, incomplete enumeration,
    # capacity refusal, oversize line). Historical gap flags alone leave it False. A
    # scheduling hint only: never stored and never sent on the wire.
    retry_blocked: bool = False
    # Connector-owned uncertainty, committed atomically with mappings/cursor. Readers
    # never invent these; no transcript contents or session identities are carried.
    mapping_gaps: tuple[MappingGap, ...] = ()


class Reader(Protocol):
    """A format reader. Pure with respect to its inputs: reads under `root` only,
    rejects symlink escapes, and returns content-free errors via Coverage."""

    format_id: str  # versioned, e.g. "jsonl-message-usage-v1"

    # Process-evidence linking (rank 2): the session lock leaf this format holds
    # open while running, or None when the format has no lock. Only formats with
    # a lock name are eligible for process probing.
    lock_filename: str | None

    def read(self, root: Path, cursor: Cursor, budget: Budget) -> Batch: ...

    def lock_session_keys(self, rel: str) -> tuple[str | None, str | None]:
        """(session, parent) from a lock relpath; (None, None) when not a lock.

        Must agree exactly with the session keys this reader emits for events,
        so probed locks link the same sessions the reader reports.
        """
        ...


class LimitsReader(Protocol):
    format_id: str

    def read(
        self, root: Path, kind: str, cursor: Cursor, budget: Budget, now_ms: int
    ) -> tuple[tuple[LimitSnapshot, ...], Cursor]: ...


@dataclass(frozen=True)
class UsageSource:
    """One owner-configured `[[usage.sources]]` entry."""

    host_id: str
    kind: str
    format: str
    root: Path = field(repr=False)
    limits_format: str | None = None
    limits_cache: Path | None = field(default=None, repr=False)

    @property
    def source_key(self) -> str:
        return f"{self.host_id}/{self.kind}/{self.format}"


@dataclass(frozen=True)
class SessionMapping:
    """One observation episode of a Herdr pane running `session_key` in a space.
    A->B->A moves are three episodes with distinct `interval_id`s, so moving never
    rewrites earlier days. Attribution of an event at time t: the episode whose
    [first_seen_ms, last_seen_ms] contains t; if several containing episodes name
    different spaces, the event is ambiguous and stays not_linked (no winner is
    picked, and it never falls through to inferred linking); containing episodes
    that agree on one space attribute to it; before the first episode -> the first
    episode; in a gap between episodes -> the episode that ended most recently
    before t; after the last (disconnect) -> the last episode. Sessions with no
    episode (null Herdr identity) stay unmapped: not counted, only reported in
    KindCoverage. Explicit MappingGap intervals override this fallback: no space
    is guessed. `generation` disambiguates Herdr workspace-id reuse: episodes are
    keyed (workspace_id, generation); `label`/`repo_fp` are the space's display
    label and keyed repo fingerprint as observed with the episode. `rank` is the
    evidence ladder: rank 1 (reported identity) always wins over rank 2
    (process evidence); ambiguity is judged within the winning rank only."""

    source_key: str
    kind: str
    session_key: str = field(repr=False)
    interval_id: str
    host_id: str
    workspace_id: str
    first_seen_ms: int
    last_seen_ms: int
    generation: int = 0
    label: str | None = None
    repo_fp: str | None = field(default=None, repr=False)
    rank: int = 1  # evidence ladder: 1 = reported identity, 2 = process evidence


@dataclass(frozen=True)
class MappingGap:
    """Bounded mapping backpressure interval. None ends only after a clean snapshot.

    Attribution during this interval is unknown, even if a nearby mapping exists.
    The collector acknowledges its exact version only after a committed apply.
    """

    source_key: str
    gap_id: str
    first_ms: int
    last_ms: int | None = None


@dataclass(frozen=True)
class PaneCwdObservation:
    """One episode of a pane observed with one working-folder digest in a space.

    The connector's pane-side evidence for inferred linking: the raw folder is
    digested in memory and never leaves the connector. Attribution joins
    `digest` against the session's `UsageEvent.cwd_digest` with kind equality
    and time overlap; several candidate spaces mean not_linked. No pane id is
    stored: `interval_id` correlates acknowledgement only."""

    source_key: str
    kind: str
    interval_id: str
    host_id: str
    digest: str = field(repr=False)
    workspace_id: str = ""
    generation: int = 0
    first_seen_ms: int = 0
    last_seen_ms: int = 0


_SPACE_GENERATION = re.compile(r"(.+)~g([0-9]+)")


def parse_space_id(space_id: str) -> tuple[str, int | None]:
    """Split a wire space id into (workspace_id, generation): `w2` -> (w2, None)
    meaning the current generation, `w2~g0` -> (w2, 0). Anything else is returned
    unchanged with None (matched literally, so old rows keep working)."""
    match = _SPACE_GENERATION.fullmatch(space_id)
    if match is None:
        return space_id, None
    return match.group(1), int(match.group(2))


def space_wire_id(workspace_id: str, generation: int, current: int) -> str:
    """Wire id for one (workspace, generation): the bare id for the current
    generation, else `w2~g0`. Opaque to clients, who send it back as space_id."""
    if generation == current:
        return workspace_id
    return f"{workspace_id}~g{generation}"


@dataclass(frozen=True)
class PriceRate:
    """USD per million tokens, effective from a date. A missing rate for any bucket an
    event uses (incl. any cache_write_unknown) => that event is tokens-only. Rates must be
    finite and nonnegative. Credits reported by a log are never reinterpreted as USD."""

    model: str
    effective_from: str  # ISO date
    input: float
    output: float
    cache_read: float | None = None
    cache_write_short: float | None = None
    cache_write_long: float | None = None
    # Catalog 'above_Nk_tokens': N*1000 prompt tokens, strictly greater than the threshold.
    # Prompt size includes uncached input, cache reads and all writes, never output/reasoning.
    # Above rates never fall back to base rates: a used bucket without a rate is unpriced.
    tier_threshold_tokens: int | None = None
    input_above_threshold: float | None = None
    output_above_threshold: float | None = None
    cache_read_above_threshold: float | None = None
    cache_write_short_above_threshold: float | None = None
    cache_write_long_above_threshold: float | None = None


@dataclass(frozen=True)
class UsageQuery:
    host_id: str
    from_day: str  # ISO date in the configured timezone, inclusive
    to_day: str  # inclusive; to_day - from_day < MAX_QUERY_DAYS
    space_id: str | None = None
    cursor: str | None = None
    hour: bool = False  # hourly buckets: only for a one-day range


@dataclass(frozen=True)
class UsageRow:
    day: str
    workspace_id: str | None  # set only when scope is WORKSPACE
    kind: str
    model: str | None
    counters: Counters
    sessions: int
    cost_micro_usd: int | None  # None when any contributing model has no price
    scope: RowScope = RowScope.WORKSPACE
    # Price snapshot date used for cost_micro_usd; "mixed" if the row spans snapshots.
    price_snapshot: str | None = None
    # Per-event aggregates. An event is "empty" when every counter is 0 (in no
    # field), "priced" when its cost is known (a free model prices to 0), else "unpriced".
    priced_cost_micro_usd: int = 0  # sum of the costs of priced events
    priced_tokens: int = 0  # tokens of priced events, including events that cost 0
    unpriced_tokens: int = 0  # tokens of nonzero events with no price
    hour: int | None = None  # host-local wall-clock hour 0-23, for hourly rows only
    # link evidence behind the row's sessions. A row mixes sessions, so counts,
    # not one label: exact-linked sessions, and inferred-linked sessions (0 on
    # NOT_LINKED rows; a session with both kinds of events counts in both).
    linked_sessions: int = 0
    inferred_sessions: int = 0
    # rows group by meter. Credit rows carry credits_milli with zero
    # counters and are never priced (cost stays None).
    meter: str = "tokens"
    credits_milli: int = 0


@dataclass(frozen=True)
class KindCoverage:
    kind: str
    coverage: Coverage
    observed_ms: int | None = None  # last successful pass
    unmapped_sessions: int = 0  # seen in logs, never linked to a Herdr pane: not counted
    # , disjoint with unmapped_sessions: exact-linked and inferred-linked sessions.
    linked_sessions: int = 0
    inferred_sessions: int = 0


@dataclass(frozen=True)
class UsageReport:
    host_id: str
    generated_ms: int
    timezone: str
    rows: tuple[UsageRow, ...]
    limits: tuple[LimitSnapshot, ...]
    coverage: tuple[KindCoverage, ...]
    price_table_date: str | None = None
    next_cursor: str | None = None
    schema_version: int = USAGE_VERSION
    # "ok", or "exceeded" when the report is over the row cap: then rows are empty, never partial.
    capacity: str = "ok"  # CAPACITY_OK
    # (workspace_id, latest day with attributed usage in this report), read BEFORE any space filter:
    # one row per space, so a space filter cannot erase another space's recency.
    workspace_days: tuple[tuple[str, str], ...] = ()
    # (wire space id, historical label) for spaces in this report whose label was
    # stored with an episode; the gateway prefers live inventory labels, then these.
    workspace_labels: tuple[tuple[str, str], ...] = ()


class UsageUnavailable(RuntimeError):
    """Fixed safe categories only; never include a path, session id or exception text."""

    def __init__(self, code: str = "unavailable") -> None:
        allowed = {
            "unavailable",
            "disabled",
            "busy",
            "corrupt",
            "invalid_query",
            "closed",
            "stale_batch",
        }
        self.code = code if code in allowed else "unavailable"
        super().__init__(self.code)


class UsageStore(Protocol):
    def apply(
        self,
        kind: str,
        batch: Batch,
        mappings: tuple[SessionMapping, ...],
        *,
        source_key: str,
        cwd: tuple[PaneCwdObservation, ...] = (),
    ) -> ApplyResult:
        """In one transaction: compare-and-swap the source cursor revision against
        batch.expected_revision, upsert events (replacing a keyed event's previous
        contribution), limits (latest observed per quota/window), mappings, session
        working-folder digests (from batch events) and pane cwd observations, then
        store next_cursor. Exact retry of an applied batch: no-op. Stale batch: raise
        UsageUnavailable("stale_batch") and change nothing. Returns what durably changed
        (an exact retry returns ApplyResult())."""
        ...

    def cursor(self, source_key: str) -> Cursor: ...
    def query(self, query: UsageQuery, now_ms: int) -> UsageReport: ...
    def prune(self, now_ms: int) -> int:
        """Bounded per call: delete events, day aggregates, mappings, limits, cursors of
        removed sources and dedup state older than RETENTION_DAYS. Events dated before the
        retention horizon are never re-inserted by a later rescan."""
        ...
