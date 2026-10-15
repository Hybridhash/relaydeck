"""Format "json-session-credits-v1": session credit consumption, never tokens.

The root is the sessions directory. Two layouts under it, read as one source
so resume dedupe sees both:

- v2 `cli/<id>.json`: one JSON document per session, parsed whole. Per turn,
  `metering_usage[]` carries credit entries; other units are skipped and
  counted, and the file is marked with gap evidence (PARTIAL), never
  converted. A turn with no credit total is not an event; a turn with no
  metering data at all is quietly not an event.
- v3 `<workspace>/sess_<id>/messages.jsonl`: streamed lines; rare
  `usage_summary` lines carry `promptTurnSummaries[]` with credit usage.

Turn identity (v2): `<session>:<loop_id>`. The `loop_id` object
(`agent_id.name` + `rand`, plus the agent's own parent/rand when set) is
digested and session-qualified; same-loop turns stay distinct by timestamp
and message list, since loop ids alone are not unique per turn (three live
pairs share one). A turn whose loop id is missing or invalid falls back to
a positional key; that fallback moves under compaction or re-indexing,
which is its documented limit. Stability was verified against recorded
shapes: 264/264 real turns carry the dict form.

Resume dedupe: when `sess_<X>` exists, V2 turns of X at or after the first V3
summary are dropped (kiroku ADR 0021). V3 files are read before V2 files every
pass and each V3 file pins its earliest summary time, so a fresh source never
double counts; a session that resumes in V3 after its V2 turns were already
read keeps those turns (absence never deletes), which is the documented
residue of this rule.

Fork dedupe: turns copied across V2 sessions with the same loop id and the
same metering totals count once, kept by the earliest session (file mtime,
then session, then path). A claim scan over the v2 layout runs before the
v2 phase every pass, so the winner does not depend on pass order; each
file also records the turns it won in its own context, and those standing
claims veto later files even when the winner is no longer the earliest
holder (an original that drops a turn still owns it via its stored events).
Only files present in the pass take part: a fork first read after its
original was deleted counts its turns anew, which is the documented
residue. When the claim scan itself is truncated, winner checks degrade
to standing claims only and the batch stays PARTIAL.

Event keys: v2 `<session, loop digest, time, messages>` or positional
fallback; v3 the line id or the byte offset. All digested; no raw text
crosses.

Coverage is UNAVAILABLE when files hold turns but no credits were ever read
("credits not reported"). Tokens are never reported by this reader.
"""

from __future__ import annotations

import json
import math
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path

from relaydeck.usage.contracts import (
    MAX_CREDITS_MILLI,
    Basis,
    Batch,
    Budget,
    Counters,
    Coverage,
    Cursor,
    FileCursor,
)
from relaydeck.usage.readers._files import (
    REASON_OVERSIZE,
    REASON_SKIP,
    REASON_UNREADABLE,
    EventCollector,
    Layout,
    Ledger,
    LineHandler,
    Listed,
    PassResult,
    PassState,
    Unit,
    context_of,
    cursor_for,
    enumerate_layout,
    file_context,
    file_key,
    head_digest_fd,
    key_text,
    make_event,
    model_text,
    ms_of,
    open_confined,
    open_root,
    read_file_unit,
    run_units,
    session_text,
    to_batch,
    validate_cursor,
)
from relaydeck.usage.readers._read import read_all

FORMAT_ID = "json-session-credits-v1"
SCHEMA = "credits-1"

MAX_CREDIT_VALUE = 1_000_000  # per-entry clamp (kiroku); larger values poison the turn
MAX_SESSION_JSON_BYTES = 65_536

V2_LAYOUT = Layout(
    descend=lambda parts: parts == ("cli",),
    accept=lambda parts: len(parts) == 2 and parts[0] == "cli" and parts[1].endswith(".json"),
)


def _descend3(parts: tuple[str, ...]) -> bool:
    if len(parts) == 1:
        return True
    return len(parts) == 2 and parts[1].startswith("sess_")


def _accept3(parts: tuple[str, ...]) -> bool:
    return len(parts) == 3 and parts[1].startswith("sess_") and parts[2] == "messages.jsonl"


V3_LAYOUT = Layout(descend=_descend3, accept=_accept3)


def _count(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _metering_total(entries: object) -> tuple[int | None, bool]:
    """(total milli-credits, non-credit unit seen); None when the list is unusable.

    Only the exact unit "credit" counts. A missing list is quietly zero (turns
    predate metering); zero-value entries contribute nothing; negative,
    non-finite, oversized or non-numeric credit values poison the turn.
    """
    if entries is None:
        return 0, False
    if not isinstance(entries, list):
        return None, False
    total = 0
    non_credit = False
    for entry in entries:
        if not isinstance(entry, dict):
            return None, non_credit
        if entry.get("unit") != "credit":
            non_credit = True
            continue
        value = entry.get("value")
        if isinstance(value, bool) or not isinstance(value, int | float):
            return None, non_credit
        if not math.isfinite(value) or value < 0 or value > MAX_CREDIT_VALUE:
            return None, non_credit
        total += int(round(value * 1000))
    return total, non_credit


def _loop_digest(turn: Mapping[str, object]) -> str | None:
    """The turn's loop identity, or None when missing or invalid.

    Real files carry `{"agent_id": {"name", "parent_id", "rand"}, "rand"}`;
    the name and rand are required, the agent's own parent/rand join the key
    when set so subagent loops stay distinct.
    """
    lid = turn.get("loop_id")
    if not isinstance(lid, dict):
        return None
    agent = lid.get("agent_id")
    if not isinstance(agent, dict):
        return None
    name = agent.get("name")
    rand = lid.get("rand")
    if not isinstance(name, str) or not name:
        return None
    if isinstance(rand, bool) or not isinstance(rand, int):
        return None
    parent = agent.get("parent_id")
    arand = agent.get("rand")
    return key_text(
        "loop",
        name,
        rand,
        parent if isinstance(parent, str) else "",
        arand if isinstance(arand, int) and not isinstance(arand, bool) else -1,
    )


def _message_digest(turn: Mapping[str, object]) -> str | None:
    mids = turn.get("message_ids")
    if not isinstance(mids, list):
        return None
    ids = [m for m in mids if isinstance(m, str) and m]
    return key_text("mids", *ids) if ids else None


def _stamp(value: object) -> int | None:
    """Turn time: ISO strings, Unix seconds or milliseconds; the middle band is refused."""
    via_iso = ms_of(value) if isinstance(value, str) else None
    if via_iso is not None:
        return via_iso
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    if value < 10**11:
        return int(value * 1000)
    if value >= 10**12:
        return int(value)
    return None


def _turn_key(
    turn: Mapping[str, object], index: int, session: str, occurred: int | None
) -> tuple[str, str | None]:
    """(event key, loop digest or None). Loop turns key by `<session>:<loop_id>`;
    same-loop turns stay distinct by time and messages, or by the index when
    neither exists. Without a loop id the key is positional (the documented
    limit: it moves under compaction or re-indexing)."""
    digest = _loop_digest(turn)
    if digest is None:
        return key_text("v2turn-index", index, session), None
    mids = _message_digest(turn)
    parts: list[object] = [session, digest]
    if occurred is not None:
        parts.append(occurred)
    if mids is not None:
        parts.append(mids)
    if occurred is None and mids is None:
        parts.append(index)
    return key_text("v2turn", *parts), digest


def _fork_composite(loop: str, total: int) -> str:
    """One fork-claimable turn: a loop identity with its exact metering total."""
    return key_text("fork", loop, total)


def _v2_session(document: object, rel: str) -> str | None:
    """The document's session: its id, or the file stem when missing."""
    if not isinstance(document, dict):
        return None
    fallback = rel.split("/")[-1][: -len(".json")]
    return session_text(document.get("session_id")) or session_text(fallback)


def _v2_turns(document: object) -> object:
    """The document's turn list (unvalidated); None when the shape is absent."""
    if not isinstance(document, dict):
        return None
    state = document.get("session_state")
    metadata = state.get("conversation_metadata") if isinstance(state, dict) else None
    return metadata.get("user_turn_metadatas") if isinstance(metadata, dict) else None


@dataclass(frozen=True)
class _Forks:
    """Fork comparison for one pass. `winners` maps each loop turn's claim
    composite to its earliest holder (file mtime, session, rel); `scanned`
    holds the rels the claim scan read. When the scan is incomplete, winner
    checks degrade to standing claims only."""

    winners: Mapping[str, tuple[int, str, str]]
    scanned: frozenset[str]
    complete: bool


@dataclass(frozen=True)
class _ClaimScan:
    """One claim scan: the fork winners, the bytes read (charged to the v2
    phase), and every v2 rel listed, whether readable or not."""

    forks: _Forks
    bytes_used: int
    rels: frozenset[str]


def _prescan_forks(root: Path, budget: Budget) -> _ClaimScan:
    """Claim scan over the v2 layout: every loop turn's earliest holder, plus
    the bytes read (charged to the v2 phase). Files the scan cannot read
    rejoin fork comparison on the next pass."""
    root_fd = open_root(root)
    if root_fd is None:
        return _ClaimScan(_Forks({}, frozenset(), False), 0, frozenset())
    try:
        state = PassState(budget)
        listed, enumerated = enumerate_layout(root_fd, V2_LAYOUT, state)
        winners: dict[str, tuple[int, str, str]] = {}
        scanned: set[str] = set()
        complete = enumerated
        used = 0
        for item in listed:
            if state.out_of_time() or used + item.st.st_size > budget.max_bytes:
                complete = False
                break
            try:
                fd, fst = open_confined(root_fd, item.rel)
            except OSError:
                continue
            try:
                if fst.st_size > budget.max_document_bytes:
                    continue
                if used + fst.st_size > budget.max_bytes:
                    complete = False
                    break
                try:
                    raw = read_all(fd, fst.st_size)
                except OSError:
                    continue
                used += fst.st_size
                try:
                    document = json.loads(raw)
                except ValueError:
                    continue
                session = _v2_session(document, item.rel)
                if session is None:
                    continue
                turns = _v2_turns(document)
                if not isinstance(turns, list):
                    continue
                scanned.add(item.rel)
                for turn in turns:
                    if not isinstance(turn, dict):
                        continue
                    loop = _loop_digest(turn)
                    total, _ = _metering_total(turn.get("metering_usage"))
                    if loop is None or total is None or total <= 0 or total > MAX_CREDITS_MILLI:
                        continue
                    composite = _fork_composite(loop, total)
                    mine = (fst.st_mtime_ns, session, item.rel)
                    if composite not in winners or mine < winners[composite]:
                        winners[composite] = mine
            finally:
                os.close(fd)
        return _ClaimScan(
            _Forks(winners, frozenset(scanned), complete),
            used,
            frozenset(item.rel for item in listed),
        )
    finally:
        os.close(root_fd)


def _claims(files: Mapping[str, FileCursor], own: str) -> set[str]:
    """Standing fork claims (claim composites) from every other file's context."""
    found: set[str] = set()
    for key, other in files.items():
        if key == own:
            continue
        ctx = context_of(other).get("r")
        claims = ctx.get("fk") if isinstance(ctx, dict) else None
        if not isinstance(claims, dict):
            continue
        for composite, mark in claims.items():
            if isinstance(composite, str) and composite and type(mark) is int and mark >= 0:
                found.add(composite)
    return found


def _cuts(files: Mapping[str, FileCursor]) -> dict[str, int]:
    """V3 resume cutoffs (session digest -> first summary ms) from file contexts."""
    found: dict[str, int] = {}
    for file in files.values():
        ctx = context_of(file).get("r")
        if not isinstance(ctx, dict):
            continue
        css, cut = ctx.get("css"), ctx.get("cut")
        if isinstance(css, str) and css and type(cut) is int and cut >= 0:
            found[css] = cut if css not in found else min(found[css], cut)
    return found


@dataclass
class _Parsed:
    complete: bool
    next_index: int
    gap: bool
    claims: dict[str, int]
    skipped: int
    reason: str  # "done", "budget" (the event budget refused a turn) or "capacity"
    seen: int
    nonzero: int


def _doc_entry(
    claims: dict[str, int], index: int, signature: str, seen: int, nonzero: int
) -> dict[str, object]:
    return {"c": seen, "n": index, "s": signature, "u": nonzero, "fk": claims}


def _parse_v2(
    session: str | None,
    turns: object,
    collector: EventCollector,
    claims: dict[str, int],
    vetoed: set[str],
    forks: _Forks,
    mtime_ns: int,
    rel: str,
    cut: int | None,
    digest: str | None,
    parent: str | None,
    mtime_ms: int,
    signature: str,
    start: int,
    fits: Callable[[dict[str, object]], bool],
    seen: int,
    nonzero: int,
) -> _Parsed:
    """Emit credit turns from `start`. Claims grow only for emitted turns."""
    if session is None or not isinstance(turns, list):
        return _Parsed(True, 0, False, claims, 0, "done", seen, nonzero)
    mine = (mtime_ns, session, rel)
    compare = forks.complete and rel in forks.scanned
    gap = False
    skipped = 0
    claims = dict(claims)
    for index in range(start, len(turns)):
        raw = turns[index]
        if not isinstance(raw, dict):
            gap = True
            skipped += 1
            continue
        total, non_credit = _metering_total(raw.get("metering_usage"))
        if total is None or total > MAX_CREDITS_MILLI:
            gap = True
            skipped += 1
            continue
        if non_credit:
            gap = True  # foreign units are skipped and counted, never converted
            skipped += 1
        if total == 0:
            seen += 1
            continue
        real_stamp = _stamp(raw.get("end_timestamp"))
        occurred = real_stamp if real_stamp is not None else mtime_ms
        if cut is not None and occurred >= cut:
            seen += 1  # resumed in v3: the v3 summary owns this turn
            nonzero += 1
            skipped += 1
            continue
        turn_key, loop = _turn_key(raw, index, session, real_stamp)
        composite = _fork_composite(loop, total) if loop is not None else None
        if composite is not None and composite in vetoed:
            seen += 1  # a standing claim owns this identical turn
            nonzero += 1
            skipped += 1
            continue
        winner = forks.winners.get(composite) if composite is not None else None
        if compare and winner is not None and winner != mine:
            seen += 1  # an earlier session owns this identical turn
            nonzero += 1
            skipped += 1
            continue
        if composite is not None:
            candidate = {**claims, composite: 1}
            if candidate != claims:
                entry = _doc_entry(candidate, index + 1, signature, seen + 1, nonzero + 1)
                if digest is not None:
                    entry["d"] = digest
                if not fits(entry):
                    return _Parsed(False, index, gap, claims, skipped, "capacity", seen, nonzero)
            claims = candidate
        event = make_event(
            session=session,
            event_key=turn_key,
            occurred_ms=occurred,
            model=model_text(raw.get("model")),
            counters=Counters(),
            basis=Basis.PER_TURN,
            parent=parent,
            verified=True,
            digest=digest,
            meter="credits",
            credits_milli=total,
        )
        if not collector.offer(event):
            return _Parsed(False, index, gap, claims, skipped, "budget", seen, nonzero)
        seen += 1
        nonzero += 1
    return _Parsed(True, len(turns), gap, claims, skipped, "done", seen, nonzero)


def _v2_claims(doc: Mapping[str, object]) -> dict[str, int]:
    raw = doc.get("fk")
    if not isinstance(raw, dict):
        return {}
    return {k: v for k, v in raw.items() if isinstance(k, str) and type(v) is int and v >= 0}


def _document_unit(
    root_fd: int,
    item: Listed,
    prev: FileCursor | None,
    pending: EventCollector,
    state: PassState,
    ledger: Ledger,
    budget: Budget,
    forks: _Forks,
) -> Unit:
    """One v2 session document: standing fork claims and v3 cuts are consulted
    from every other file's committed context, and the claim scan names the
    earliest holder of each loop turn, so pass order cannot double count."""
    key = file_key(item.rel)
    if state.reads > 0 and item.st.st_size > state.bytes_left:
        return Unit(key=key, resumable=True)  # first-unit rule: a large document starts a pass
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
        doc = held if isinstance(held, dict) else {}
        same = doc.get("s") == signature
        start = _count(doc.get("n")) if same else 0
        seen = _count(doc.get("c")) if same else 0
        nonzero = _count(doc.get("u")) if same else 0
        pinned = doc.get("d")
        digest = pinned if isinstance(pinned, str) and pinned else None
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
        mtime_ms = fst.st_mtime_ns // 1_000_000

        def fits(entry: dict[str, object]) -> bool:
            text = file_context(reader=entry, discard=None, gap=False, pending=False)
            return ledger.would_fit({key: replace(unfinished, context=text)}, {}, set())

        session = _v2_session(document, item.rel)
        turns = _v2_turns(document)
        parent: str | None = None
        if isinstance(document, dict):
            parent = session_text(document.get("parent_session_id"))
            if digest is None and budget.digest is not None:
                folder = document.get("cwd")
                if isinstance(folder, str) and folder:
                    digest = budget.digest(folder)
        cut: int | None = None
        if session is not None:
            cut = _cuts(ledger.files).get(key_text("v3sess", session))
        parsed = _parse_v2(
            session,
            turns,
            pending,
            _v2_claims(doc),
            _claims(ledger.files, key),
            forks,
            fst.st_mtime_ns,
            item.rel,
            cut,
            digest,
            parent,
            mtime_ms,
            signature,
            start,
            fits,
            seen,
            nonzero,
        )
        done = parsed.complete
        entry = {
            "c": parsed.seen,
            "n": parsed.next_index,
            "s": signature,
            "u": parsed.nonzero,
            "fk": parsed.claims,
        }
        if digest is not None:
            entry["d"] = digest
        return Unit(
            key=key,
            file=cursor_for(fd, fst, fst.st_size if done else 0),
            gap=parsed.gap,
            reader=entry,
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


class _V3Handler(LineHandler):
    """Streams one v3 messages.jsonl: usage_summary lines become credit events."""

    def __init__(self, cursor: Cursor, digest: Callable[[str], str | None] | None = None) -> None:
        self.cursor = cursor
        self.digest_of = digest
        self.meta: dict[str, tuple[object, str | None]] = {}
        self.reader_out: dict[str, dict[str, object]] = {}
        self.skipped = 0
        self.session: str | None = None
        self.pinned_digest: str | None = None
        self.cut: int | None = None
        self.seen = 0
        self.nonzero = 0
        self.model: object = None
        self.folder: str | None = None

    def begin(self, key: str, rel: str, restarted: bool) -> None:
        loaded: dict[str, object] = {}
        if not restarted:
            prior = context_of(self.cursor.files.get(key)).get("r")
            loaded = prior if isinstance(prior, dict) else {}
        self.session = session_text(rel.split("/")[-2][len("sess_") :])
        self.pinned_digest = None
        pinned = loaded.get("d")
        if isinstance(pinned, str) and pinned:
            self.pinned_digest = pinned
        cut = loaded.get("cut")
        self.cut = cut if type(cut) is int and cut >= 0 else None
        self.seen = _count(loaded.get("c")) if not restarted else 0
        self.nonzero = _count(loaded.get("u")) if not restarted else 0
        model, folder = self.meta.get(key, (None, None))
        self.model = model
        self.folder = folder

    def end(self, key: str, rel: str) -> bool:
        out: dict[str, object] = {"c": self.seen, "u": self.nonzero}
        if self.session is not None:
            out["css"] = key_text("v3sess", self.session)
        if self.cut is not None:
            out["cut"] = self.cut
        if self.pinned_digest is not None:
            out["d"] = self.pinned_digest
        self.reader_out[key] = out
        return False  # skipped lines are gap evidence via the skipped count, never legacy

    def reader_context(self, key: str) -> dict[str, object] | None:
        return self.reader_out.get(key)

    def line(self, key: str, rel: str, offset: int, data: bytes) -> bool:
        try:
            record = json.loads(data)
        except ValueError:
            self.skipped += 1
            return True
        if not isinstance(record, dict) or self.session is None:
            return True
        payload = record.get("payload")
        summaries = payload.get("promptTurnSummaries") if isinstance(payload, dict) else None
        if not isinstance(summaries, list):
            return True  # transcript lines carry no credit data
        occurred = _stamp(record.get("timestamp"))
        total = 0
        non_credit = False
        usable = True
        for summary in summaries:
            if not isinstance(summary, dict):
                usable = False
                break
            if summary.get("unit") != "credit":
                non_credit = True
                continue
            value = summary.get("usage")
            if (
                isinstance(value, bool)
                or not isinstance(value, int | float)
                or not math.isfinite(value)
                or value < 0
                or value > MAX_CREDIT_VALUE
            ):
                usable = False
                break
            total += int(round(value * 1000))
        if not usable or occurred is None or total > MAX_CREDITS_MILLI:
            self.skipped += 1
            return True
        if non_credit:
            self.skipped += 1  # foreign units are skipped and counted, never converted
        self.cut = occurred if self.cut is None else min(self.cut, occurred)
        self.seen += 1
        if total == 0:
            return True
        if self.pinned_digest is None and self.digest_of is not None and self.folder:
            self.pinned_digest = self.digest_of(self.folder)
        line_id = record.get("id")
        if isinstance(line_id, str) and line_id:
            event_key = key_text("v3line", line_id)
        else:
            event_key = key_text("v3line-offset", offset)
        event = make_event(
            session=self.session,
            event_key=event_key,
            occurred_ms=occurred,
            model=model_text(self.model),
            counters=Counters(),
            basis=Basis.PER_TURN,
            verified=True,
            digest=self.pinned_digest,
            meter="credits",
            credits_milli=total,
        )
        if not self.collector.offer(event):
            return False
        self.nonzero += 1
        return True


def _read_session_meta(root_fd: int, rel: str) -> tuple[object, str | None, int]:
    """(model, first workspace path, bytes read) from the sibling session.json.

    Best effort: a missing, oversized or malformed file yields (None, None).
    Only the first workspace path is used, and only as a digest input."""
    parts = rel.split("/")
    meta_rel = "/".join([*parts[:-1], "session.json"])
    try:
        fd, fst = open_confined(root_fd, meta_rel)
    except OSError:
        return None, None, 0
    try:
        if fst.st_size > MAX_SESSION_JSON_BYTES:
            return None, None, 0
        try:
            raw = read_all(fd, fst.st_size)
        except OSError:
            return None, None, 0
        try:
            document = json.loads(raw)
        except ValueError:
            return None, None, 0
        if not isinstance(document, dict):
            return None, None, fst.st_size
        paths = document.get("workspacePaths")
        folder = None
        if isinstance(paths, list):
            folder = next((p for p in paths if isinstance(p, str) and p), None)
        return document.get("modelId"), folder, fst.st_size
    finally:
        os.close(fd)


def _v3_unit(
    root_fd: int,
    item: Listed,
    prev: FileCursor | None,
    base: Cursor,
    pending: EventCollector,
    state: PassState,
    handler: _V3Handler,
) -> Unit:
    model, folder, size = _read_session_meta(root_fd, item.rel)
    state.bytes_left -= size
    handler.meta[file_key(item.rel)] = (model, folder)
    return read_file_unit(root_fd, item, prev, base, pending, state, handler)


def _credits_absent(files: Mapping[str, FileCursor]) -> bool:
    """True when stored files saw credit turns and none carried any credits."""
    seen = 0
    nonzero = 0
    for file in files.values():
        doc = context_of(file).get("r")
        if isinstance(doc, dict):
            seen += _count(doc.get("c"))
            nonzero += _count(doc.get("u"))
    return seen > 0 and nonzero == 0


class JsonSessionCreditsReader:
    format_id = FORMAT_ID
    tokens_reported = False
    credits_reported = True
    lock_filename: str | None = None

    def lock_session_keys(self, rel: str) -> tuple[str | None, str | None]:
        return None, None

    def read(self, root: Path, cursor: Cursor, budget: Budget) -> Batch:
        """Two phases sharing one cursor: v3 first (resume cuts land before any
        v2 turn emits), then a v2 claim scan, then v2. Each step enforces the
        byte/deadline budget separately (bounded 3x wall); the event budget is
        split across phases and the scan's bytes are charged to the v2 phase."""
        handler = _V3Handler(cursor, budget.digest)
        v3 = run_units(
            root,
            cursor,
            budget,
            V3_LAYOUT,
            lambda root_fd, item, prev, pending, state, ledger: _v3_unit(
                root_fd, item, prev, cursor, pending, state, handler
            ),
            schema=SCHEMA,
        )
        rest = replace(budget, max_events=max(0, budget.max_events - len(v3.events)))
        scan = _prescan_forks(root, rest)
        vre = replace(rest, max_bytes=max(0, rest.max_bytes - scan.bytes_used))
        # Each phase drops files outside its own layout, so the v2 phase runs
        # on the v3 output plus the v2 entries the v3 phase dropped; the scan's
        # listing names exactly those, and nothing deleted is resurrected.
        kept = {file_key(rel) for rel in scan.rels}
        mid = replace(
            v3.cursor,
            files={
                **v3.cursor.files,
                **{k: v for k, v in cursor.files.items() if k in kept},
            },
        )
        v2 = run_units(
            root,
            mid,
            vre,
            V2_LAYOUT,
            lambda root_fd, item, prev, pending, state, ledger: _document_unit(
                root_fd, item, prev, pending, state, ledger, vre, scan.forks
            ),
            schema=SCHEMA,
        )
        events = tuple(
            sorted(
                (*v3.events, *v2.events),
                key=lambda e: (e.occurred_ms, e.session_key, e.event_key),
            )
        )
        # The v2 phase dropped the v3 entries in turn; each side's files are
        # authoritative for its own layout, and either side's markers survive.
        merged = replace(
            v2.cursor,
            files={**v3.cursor.files, **v2.cursor.files},
            state={**v3.cursor.state, **v2.cursor.state},
        )
        if not validate_cursor(merged):
            return to_batch(
                PassResult((), cursor, True, False, 0, unavailable=True, retry_blocked=True),
                cursor,
                Coverage.UNAVAILABLE,
            )
        combined = PassResult(
            events=events,
            cursor=merged,
            partial=v3.partial or v2.partial or not scan.forks.complete,
            more=v3.more or v2.more,
            skipped=v3.skipped + v2.skipped,
            unavailable=v3.unavailable or v2.unavailable,
            retry_blocked=v3.retry_blocked or v2.retry_blocked,
        )
        clean = Coverage.COMPLETE
        if (
            not combined.unavailable
            and not combined.partial
            and _credits_absent(combined.cursor.files)
        ):
            clean = Coverage.UNAVAILABLE  # turns exist but report no credits
        return to_batch(combined, cursor, clean)


READER = JsonSessionCreditsReader()
