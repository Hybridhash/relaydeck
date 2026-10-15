"""Format "jsonl-event-usage-v1": the usage representations in per-session event logs.

Layout under the root: `sessions/YYYY/MM/DD/<id>/session.jsonl`. Two counted
representations (schema events-4):

- `goal_usage_attribution` lines carry `payload.event.record`: `usage_id` is
  the event key (a replay replaces it), `owner.session_id` is the session,
  `quantity` holds the counters. Only records with `quantity.reported=True`
  are counted; unreported records are estimates, ignored quietly, so a later
  reported correction is the only one that sums. Cached tokens provably sit
  inside the input: total == input+output and input == cached+non-cached
  hold exactly on real usage lines, and cached <= input / reasoning <= output
  hold on every real record line, so input_uncached = input-cached with
  cache_read = cached.
- `automated_review_completed` lines carry `payload.event.usage` with
  input/output/cached_input/non_cached/reasoning counters and no identity
  keys; they are keyed by (file generation, byte offset) and attributed to
  the directory session. Other usage kinds are deliberately not counted:
  `model_completed` usage describes the same calls the records already count
  (per-file 1:1 on real logs: counting both would double-count),
  `resource_usage_sampled` carries RSS/CPU only, and
  `context_projection_checkpoint` has unknown overlap.

A counted line must have `record_type == "event"`. The documented contract's
`payload_type`/`payload_schema_version` requirement is NOT adopted: those
keys are absent from every real line (drift).

Enrichment: the same-root `session-index.db` maps session ids to model and
working folder (field allowlist: those two plus the id). A missing, corrupt
or locked index means no enrichment, never an error. An event is verified
iff the v4 contract holds for its line and a model is attached (owner join
for records; the line's model dict, else the directory session join, for
reviews). Model-less events stay stored but unverified.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from relaydeck.usage.contracts import Basis, Batch, Budget, Coverage, Cursor, FileCursor
from relaydeck.usage.readers._files import (
    EventCollector,
    Layout,
    Ledger,
    LineHandler,
    Listed,
    PassState,
    Unit,
    as_count,
    counters_of,
    file_key,
    head_digest_fd,
    key_text,
    make_event,
    model_text,
    open_confined,
    read_file_unit,
    run_units,
    session_text,
    to_batch,
)
from relaydeck.usage.readers._session_index import SessionIndex, load_session_index

FORMAT_ID = "jsonl-event-usage-v1"
SCHEMA = "events-4"  # v4: kinds, reviews, index enrichment; v3: per-file context
MIN_OCCURRED_MS = 946_684_800_000  # 2000-01-01T00:00:00Z
FUTURE_SLACK_MS = 86_400_000  # one day

# Usage-carrying kinds that are deliberately not counted (see the docstring);
# usage or record data under any other unknown kind is gap evidence, not quiet.
_IGNORED_USAGE_KINDS = frozenset(
    {"model_completed", "resource_usage_sampled", "context_projection_checkpoint"}
)

LOCK_FILENAME = ".session.lock"


def path_session_keys(rel: str) -> tuple[str | None, str | None]:
    """(session, None) from a session lock relpath; (None, None) otherwise.

    Mirrors this reader's layout exactly (`sessions/YYYY/MM/DD/<id>/`): the
    lock's session is the directory id, the same key the reader emits when the
    log's records name that session. Anything else links nothing (fail closed).
    """
    parts = rel.split("/")
    if len(parts) != 6 or parts[0] != "sessions" or parts[-1] != LOCK_FILENAME:
        return None, None
    return session_text(parts[-2]), None


def _now_ms() -> int:
    return int(time.time() * 1000)


def _occurred(value: object, now: int) -> int | None:
    """Milliseconds from `recorded_at` (integer microseconds), or None when it is not usable."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    millis = value // 1000
    if not MIN_OCCURRED_MS <= millis <= now + FUTURE_SLACK_MS:
        return None
    return millis


class _Handler(LineHandler):
    def __init__(
        self,
        index: SessionIndex | None,
        digest: Callable[[str], str | None] | None,
    ) -> None:
        self.index = index
        self.digest_of = digest
        self.identity: dict[str, tuple[int, int, str]] = {}
        self.skipped = 0

    def line(self, key: str, rel: str, offset: int, data: bytes) -> bool:
        try:
            record = json.loads(data)
        except ValueError:
            self.skipped += 1
            return True
        payload = record.get("payload") if isinstance(record, dict) else None
        event = payload.get("event") if isinstance(payload, dict) else None
        if not isinstance(event, dict):
            return True
        kind = event.get("kind")
        resource = event.get("record")
        usage = event.get("usage")
        if kind == "goal_usage_attribution":
            if not isinstance(resource, dict):
                return True  # a goal line without the counted representation
            return self._record(record, resource)
        if kind == "automated_review_completed":
            if not isinstance(usage, dict):
                return True
            return self._review(key, rel, offset, record, event, usage)
        if kind in _IGNORED_USAGE_KINDS:
            if isinstance(resource, dict):
                self.skipped += 1  # the counted shape under a foreign kind: drift
            return True  # the kind's own usage is deliberately not counted
        if isinstance(resource, dict) or isinstance(usage, dict):
            self.skipped += 1  # usage data under an unknown kind: drift evidence
        return True

    def _digest_for(self, session: str) -> str | None:
        if self.index is None or self.digest_of is None:
            return None
        folder = self.index.workspace(session)
        return self.digest_of(folder) if folder else None

    def _record(
        self,
        record: object,
        resource: dict[str, object],
    ) -> bool:
        if not isinstance(record, dict) or record.get("record_type") != "event":
            self.skipped += 1
            return True
        quantity = resource.get("quantity")
        if not isinstance(quantity, dict) or quantity.get("unit") != "tokens":
            return True
        if quantity.get("reported") is not True:
            return True  # an estimate: ignored, and not a gap
        usage_id = resource.get("usage_id")
        owner = resource.get("owner")
        session = session_text(owner.get("session_id")) if isinstance(owner, dict) else None
        occurred = _occurred(record.get("recorded_at"), _now_ms())
        values = {
            name: as_count(quantity.get(name, 0))
            for name in ("input_tokens", "output_tokens", "cached_tokens", "reasoning_tokens")
        }
        if (
            not isinstance(usage_id, str)
            or not usage_id
            or session is None
            or occurred is None
            or any(value is None for value in values.values())
        ):
            self.skipped += 1
            return True
        inputs = values["input_tokens"] or 0
        cached = values["cached_tokens"] or 0
        counters = counters_of(
            input_uncached=max(0, inputs - cached),
            cache_read=cached,
            output=values["output_tokens"] or 0,
            reasoning=values["reasoning_tokens"] or 0,
        )
        if counters is None:
            self.skipped += 1
            return True
        model = None
        if self.index is not None:
            model = model_text(self.index.model(session))
        return self.collector.offer(
            make_event(
                session=session,
                event_key=key_text("usage-id", usage_id),
                occurred_ms=occurred,
                model=model,
                counters=counters,
                basis=Basis.PER_CALL,
                verified=model is not None,
                digest=self._digest_for(session),
            )
        )

    def _review(
        self,
        key: str,
        rel: str,
        offset: int,
        record: object,
        event: dict[str, object],
        usage: dict[str, object],
    ) -> bool:
        if not isinstance(record, dict) or record.get("record_type") != "event":
            self.skipped += 1
            return True
        parts = rel.split("/")
        session = session_text(parts[-2]) if len(parts) == 6 and parts[0] == "sessions" else None
        identity = self.identity.get(key)
        occurred = _occurred(record.get("recorded_at"), _now_ms())
        total_in = as_count(usage.get("input_tokens", 0))
        out = as_count(usage.get("output_tokens", 0))
        cached = as_count(usage.get("cached_input_tokens", 0))
        non_cached_raw = usage.get("non_cached_input_tokens")
        if non_cached_raw is None:
            non_cached = None if total_in is None or cached is None else max(0, total_in - cached)
        else:
            non_cached = as_count(non_cached_raw)
        reasoning = as_count(usage.get("reasoning_tokens", 0))
        if (
            session is None
            or identity is None
            or occurred is None
            or total_in is None
            or out is None
            or cached is None
            or non_cached is None
            or reasoning is None
        ):
            self.skipped += 1
            return True
        counters = counters_of(
            input_uncached=non_cached,
            cache_read=cached,
            output=out,
            reasoning=reasoning,
        )
        if counters is None:
            self.skipped += 1
            return True
        model_value = event.get("model")
        if isinstance(model_value, dict):
            model_value = model_value.get("model_id")
        model = model_text(model_value)
        if model is None and self.index is not None:
            model = model_text(self.index.model(session))
        dev, ino, head = identity
        return self.collector.offer(
            make_event(
                session=session,
                event_key=key_text("review-usage", dev, ino, head, offset),
                occurred_ms=occurred,
                model=model,
                counters=counters,
                basis=Basis.PER_CALL,
                verified=model is not None,
                digest=self._digest_for(session),
            )
        )


def _descend(parts: tuple[str, ...]) -> bool:
    return bool(parts) and parts[0] == "sessions" and len(parts) <= 5


def _accept(parts: tuple[str, ...]) -> bool:
    return len(parts) == 6 and parts[0] == "sessions" and parts[5] == "session.jsonl"


LAYOUT = Layout(descend=_descend, accept=_accept)


def _event_unit(
    root_fd: int,
    item: Listed,
    prev: FileCursor | None,
    base: Cursor,
    pending: EventCollector,
    state: PassState,
    ledger: Ledger,
    handler: _Handler,
) -> Unit:
    """One session log: capture the file generation (dev, ino, head) so review
    offset keys re-key exactly when the restart rules restart, then delegate."""
    try:
        fd, fst = open_confined(root_fd, item.rel)
    except OSError:
        pass
    else:
        try:
            handler.identity[file_key(item.rel)] = (
                fst.st_dev,
                fst.st_ino,
                head_digest_fd(fd, fst.st_size),
            )
        finally:
            os.close(fd)
    return read_file_unit(root_fd, item, prev, base, pending, state, handler)


class JsonlEventUsageReader:
    format_id = FORMAT_ID
    tokens_reported = True
    lock_filename: str | None = LOCK_FILENAME

    def lock_session_keys(self, rel: str) -> tuple[str | None, str | None]:
        return path_session_keys(rel)

    def read(self, root: Path, cursor: Cursor, budget: Budget) -> Batch:
        index, used = load_session_index(root)
        rest = replace(budget, max_bytes=max(0, budget.max_bytes - used))
        handler = _Handler(index, budget.digest)
        result = run_units(
            root,
            cursor,
            rest,
            LAYOUT,
            lambda root_fd, item, prev, pending, state, ledger: _event_unit(
                root_fd, item, prev, cursor, pending, state, ledger, handler
            ),
            schema=SCHEMA,
        )
        clean = (
            Coverage.COMPLETE
            if all(event.accounting_verified for event in result.events)
            else Coverage.UNVERIFIED
        )
        return to_batch(result, cursor, clean)


READER = JsonlEventUsageReader()
