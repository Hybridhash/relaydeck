"""Format "json-session-turns-v1": whole-file session snapshots with per-turn usage.

Layout under the root: `sessions/cli/<id>.json`, one JSON document per session, parsed whole. Per
turn, the token buckets are `input_token_count`, `output_token_count`,
`cache_read_input_token_count` and `cache_write_input_token_count`. A turn with every bucket zero is
not an event. Metering credits (`metering_usage`) are not read: they are a different unit and are
not counted as tokens.

Turn identity, in priority order: `result.Ok.id`, then the first of `message_ids[]`, then the turn
index (only when the turn has neither id). A turn's first emitted key is pinned, per session, in the
document's file context, under each of its ids and, for index-only turns, under its index. A later
higher-priority id, or a later appearance of any pinned id, reuses the pinned key, so no second
event appears. Index pins are positional: reordering index-only turns moves them, which is the
documented limit of that fallback. Pins are never evicted.

Resume: a document that is not finished is stored with its next turn index and signature (size,
modification time and head digest). The next pass resumes at that index only while the signature
still matches. A document is complete only after every turn has been emitted. Its file cursor is
stored with offset 0 until then, so it is never treated as settled.

Capacity: when a turn's pin would not fit the file's context bound or the cursor, the document stops
before that turn, and the turns already emitted and their pins are committed. Other documents
continue. This is not backlog.

Token availability: the reader counts turns and the turns with any non-zero token bucket in each
document. When a complete pass saw turns and none had tokens, the coverage is UNAVAILABLE: the
UI shows "tokens not reported", not zero.

Budget: a document over `budget.max_document_bytes` is skipped and counted, with gap evidence
(PARTIAL). A document over `budget.max_bytes` is parsed only as the first unit of a pass, and the
pass then ends. Cache inclusion is unproven, so events are `accounting_verified=False`.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path

from relaydeck.usage.contracts import Basis, Batch, Budget, Coverage, Cursor, FileCursor
from relaydeck.usage.readers._files import (
    REASON_OVERSIZE,
    REASON_SKIP,
    REASON_UNREADABLE,
    EventCollector,
    Layout,
    Ledger,
    Listed,
    PassState,
    Unit,
    as_count,
    context_of,
    counters_of,
    cursor_for,
    file_context,
    file_key,
    head_digest_fd,
    key_text,
    make_event,
    model_text,
    ms_of,
    open_confined,
    run_units,
    session_text,
    to_batch,
)
from relaydeck.usage.readers._read import read_all

FORMAT_ID = "json-session-turns-v1"
SCHEMA = "turns-4"  # v4: as v3, and the earlier per-document entries migrate, not rewind


DOC_PREFIX = "t:"  # the earlier layout's per-document state key, migrated on first read


def _doc(raw: str | None) -> dict[str, object]:
    """A stored per-document entry from the earlier layout; anything unreadable is empty."""
    if raw is None:
        return {}
    try:
        loaded = json.loads(raw)
    except ValueError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _doc_entry(
    pins: dict[str, str], next_index: int, signature: str, seen: int, nonzero: int
) -> dict[str, object]:
    return {"c": seen, "n": next_index, "p": pins, "s": signature, "u": nonzero}


def _count(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _pins(doc: Mapping[str, object]) -> dict[str, str]:
    raw = doc.get("p")
    if not isinstance(raw, dict):
        return {}
    return {k: v for k, v in raw.items() if isinstance(k, str) and isinstance(v, str)}


def _ids(turn: dict[str, object]) -> tuple[str | None, list[str]]:
    """(primary result id, message ids in order); absent or empty values are omitted."""
    result = turn.get("result")
    ok = result.get("Ok") if isinstance(result, dict) else None
    primary = ok.get("id") if isinstance(ok, dict) else None
    messages = turn.get("message_ids")
    listed = [m for m in messages if isinstance(m, str) and m] if isinstance(messages, list) else []
    return (primary if isinstance(primary, str) and primary else None), listed


def _timestamp(turn: dict[str, object]) -> int | None:
    stamp = ms_of(turn.get("end_timestamp"))
    if stamp is not None:
        return stamp
    result = turn.get("result")
    ok = result.get("Ok") if isinstance(result, dict) else None
    meta = ok.get("meta") if isinstance(ok, dict) else None
    raw = meta.get("timestamp") if isinstance(meta, dict) else None
    if isinstance(raw, int) and not isinstance(raw, bool):
        if raw < 10**11:
            return raw * 1000  # Unix seconds (the live unit)
        if raw >= 10**12:
            return raw  # Unix milliseconds
    return None  # between the two ranges is ambiguous: not guessed at


@dataclass
class _Parsed:
    complete: bool
    next_index: int
    gap: bool
    pins: dict[str, str]
    skipped: int
    reason: str  # "done", "budget" (the event budget refused a turn) or "capacity"
    seen: int
    nonzero: int


def _parse(
    document: object,
    rel: str,
    collector: EventCollector,
    pins: dict[str, str],
    start: int,
    signature: str,
    fits: Callable[[dict[str, object]], bool],
    seen: int,
    nonzero: int,
) -> _Parsed:
    """Emit the turns from `start`. Counts and pins are kept only for emitted turns."""
    if not isinstance(document, dict):
        return _Parsed(True, 0, True, pins, 1, "done", seen, nonzero)
    fallback = rel.split("/")[-1][: -len(".json")]
    session = session_text(document.get("session_id")) or session_text(fallback)
    parent = session_text(document.get("parent_session_id"))
    session_state = document.get("session_state")
    metadata = (
        session_state.get("conversation_metadata") if isinstance(session_state, dict) else None
    )
    turns = metadata.get("user_turn_metadatas") if isinstance(metadata, dict) else None
    if session is None or not isinstance(turns, list):
        return _Parsed(True, 0, False, pins, 0, "done", seen, nonzero)
    gap = False
    skipped = 0
    pins = dict(pins)
    for index in range(start, len(turns)):
        raw = turns[index]
        if not isinstance(raw, dict):
            gap = True
            skipped += 1
            continue
        inputs = as_count(raw.get("input_token_count", 0))
        outputs = as_count(raw.get("output_token_count", 0))
        reads = as_count(raw.get("cache_read_input_token_count", 0))
        writes = as_count(raw.get("cache_write_input_token_count", 0))
        if inputs is None or outputs is None or reads is None or writes is None:
            gap = True
            skipped += 1
            continue
        has_tokens = bool(inputs or outputs or reads or writes)
        next_seen = seen + 1
        next_nonzero = nonzero + (1 if has_tokens else 0)
        if not has_tokens:
            seen, nonzero = next_seen, next_nonzero  # every bucket zero: counted, not an event
            continue
        occurred = _timestamp(raw)
        counters = counters_of(
            input_uncached=inputs, cache_read=reads, cache_write_unknown=writes, output=outputs
        )
        if occurred is None or counters is None:
            gap = True
            skipped += 1
            seen, nonzero = next_seen, next_nonzero
            continue
        primary, messages = _ids(raw)
        ids = ([primary] if primary else []) + messages
        digests = [key_text("id", value) for value in ids]
        pinned = next((pins[d] for d in digests if d in pins), None)
        if pinned is None:
            pinned = pins.get(key_text("index", index))
        if pinned is not None:
            turn_key = pinned
        elif primary is not None:
            turn_key = key_text("turn", primary)
        elif messages:
            turn_key = key_text("turn", messages[0])
        else:
            turn_key = key_text("turn-index", index, session)
        learned = dict.fromkeys(digests, turn_key)
        if not ids:
            learned[key_text("index", index)] = turn_key
        candidate = {**pins, **learned}
        entry = _doc_entry(candidate, index + 1, signature, next_seen, next_nonzero)
        if not fits(entry):
            return _Parsed(False, index, gap, pins, skipped, "capacity", seen, nonzero)
        event = make_event(
            session=session,
            event_key=turn_key,
            occurred_ms=occurred,
            model=model_text(raw.get("model")),
            counters=counters,
            basis=Basis.PER_TURN,
            parent=parent,
            verified=False,
        )
        if not collector.offer(event):
            return _Parsed(False, index, gap, pins, skipped, "budget", seen, nonzero)
        pins = candidate
        seen, nonzero = next_seen, next_nonzero
    return _Parsed(True, len(turns), gap, pins, skipped, "done", seen, nonzero)


def _document_unit(
    root_fd: int,
    item: Listed,
    prev: FileCursor | None,
    base: Cursor,
    pending: EventCollector,
    state: PassState,
    ledger: Ledger,
    budget: Budget,
) -> Unit:
    """One session document as a unit: parse from its stored turn index, or stop the pass.

    The listed size is only a hint: the opened descriptor is checked against the document and pass
    bounds before anything is allocated or read. An earlier layout's per-document entry is migrated
    from the cursor state, so turn pins (and the keys they keep stable) survive the upgrade."""
    key = file_key(item.rel)
    if state.reads > 0 and item.st.st_size > state.bytes_left:
        return Unit(key=key, resumable=True)  # first-unit rule: a large document starts a pass
    # Each failure is its own unit: an oversize document and an unreadable one are current
    # obstructions (they set the retry flag); a malformed document is historical evidence only.
    oversize = Unit(
        key=key,
        gap=True,
        partial=True,
        skipped=1,
        reasons=frozenset({REASON_OVERSIZE}),
        oversize_now=True,
    )
    unreadable = Unit(
        key=key,
        gap=True,
        partial=True,
        skipped=1,
        reasons=frozenset({REASON_UNREADABLE}),
        unreadable=True,
    )
    if item.st.st_size > budget.max_document_bytes:
        return oversize
    try:
        fd, fst = open_confined(root_fd, item.rel)
    except OSError:
        return unreadable
    try:
        if fst.st_size > budget.max_document_bytes:
            return oversize
        if state.reads > 0 and fst.st_size > state.bytes_left:
            return Unit(key=key, resumable=True)
        signature = f"{fst.st_size}:{fst.st_mtime_ns}:{head_digest_fd(fd, fst.st_size)}"
        held = context_of(prev).get("r")
        legacy_key = DOC_PREFIX + key
        migrated = not isinstance(held, dict) and legacy_key in base.state
        doc: dict[str, object] = (
            held if isinstance(held, dict) else _doc(base.state.get(legacy_key))
        )
        same = doc.get("s") == signature
        start = _count(doc.get("n")) if same else 0
        seen = _count(doc.get("c")) if same else 0
        nonzero = _count(doc.get("u")) if same else 0
        state.bytes_left -= fst.st_size
        try:
            raw = read_all(fd, fst.st_size)
        except OSError:
            return unreadable
        try:
            document = json.loads(raw)
        except ValueError:
            return Unit(
                key=key,
                gap=True,
                partial=True,
                skipped=1,
                reasons=frozenset({REASON_SKIP}),
            )
        unfinished = cursor_for(fd, fst, 0)

        def fits(entry: dict[str, object]) -> bool:
            text = file_context(reader=entry, discard=None, gap=False, pending=False)
            return ledger.would_fit({key: replace(unfinished, context=text)}, {}, set())

        parsed = _parse(
            document,
            item.rel,
            pending,
            _pins(doc),
            start,
            signature,
            fits,
            seen,
            nonzero,
        )
        done = parsed.complete
        return Unit(
            key=key,
            file=cursor_for(fd, fst, fst.st_size if done else 0),
            gap=parsed.gap,
            reader=_doc_entry(
                parsed.pins, parsed.next_index, signature, parsed.seen, parsed.nonzero
            ),
            state_del={legacy_key} if migrated else set(),
            skipped=parsed.skipped,
            partial=not done,
            resumable=parsed.reason == "budget",
            stop_pass=parsed.reason == "budget" or fst.st_size > budget.max_bytes,
            capacity_now=parsed.reason == "capacity",
        )
    except OSError:
        return unreadable
    finally:
        os.close(fd)


def _descend(parts: tuple[str, ...]) -> bool:
    return parts in {("sessions",), ("sessions", "cli")}


def _accept(parts: tuple[str, ...]) -> bool:
    return len(parts) == 3 and parts[:2] == ("sessions", "cli") and parts[2].endswith(".json")


LAYOUT = Layout(descend=_descend, accept=_accept)


def _tokens_absent(files: Mapping[str, FileCursor]) -> bool:
    """True when the stored documents saw turns and none carried any tokens."""
    seen = 0
    nonzero = 0
    for file in files.values():
        doc = context_of(file).get("r")
        if isinstance(doc, dict):
            seen += _count(doc.get("c"))
            nonzero += _count(doc.get("u"))
    return seen > 0 and nonzero == 0


class JsonSessionTurnsReader:
    format_id = FORMAT_ID
    tokens_reported = False
    lock_filename: str | None = None

    def lock_session_keys(self, rel: str) -> tuple[str | None, str | None]:
        return None, None

    def read(self, root: Path, cursor: Cursor, budget: Budget) -> Batch:
        result = run_units(
            root,
            cursor,
            budget,
            LAYOUT,
            lambda root_fd, item, prev, pending, state, ledger: _document_unit(
                root_fd, item, prev, cursor, pending, state, ledger, budget
            ),
            schema=SCHEMA,
            rewind=False,
        )
        clean = Coverage.UNVERIFIED
        if not result.unavailable and not result.partial and _tokens_absent(result.cursor.files):
            clean = Coverage.UNAVAILABLE  # the source reports no tokens: show "not reported"
        return to_batch(result, cursor, clean)


READER = JsonSessionTurnsReader()
