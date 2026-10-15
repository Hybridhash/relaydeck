"""Current obstructions set the retry flag; legacy needs a verified prefix and a
verified end; per-source cadence; migrated global gap markers stay unknown.

Synthetic fixtures in temp directories only.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from relaydeck.usage.collector import UsageCollector
from relaydeck.usage.contracts import (
    IDLE_POLL_MS,
    PASS_DEADLINE_MS,
    Batch,
    Budget,
    Coverage,
    Cursor,
    FileCursor,
    UsageSource,
)
from relaydeck.usage.readers import READERS, _files
from relaydeck.usage.readers._files import GAP_PREFIX, SCHEMA_KEY, file_key, reasons_of
from relaydeck.usage.store import SqliteUsageStore
from tests.support.usage.collector import FakeReader

NOW = 1_791_700_000_000
MESSAGE = "jsonl-message-usage-v1"
ROLLOUT = "jsonl-cumulative-token-count-v1"
TURNS = "json-session-turns-v1"
UUID = "0a1b2c3d-1111-4000-8000-000000000001"
REL = f"sessions/2026/10/11/rollout-x-{UUID}.jsonl"


def _run(
    format_id: str, root: Path, cursor: Cursor | None = None, budget: Budget | None = None
) -> Batch:
    return READERS[format_id].read(root, cursor or Cursor(), budget or Budget())


def _context(cursor: Cursor, rel: str) -> dict[str, Any]:
    file = cursor.files[file_key(rel)]
    return json.loads(file.context) if file.context else {}


def _token_count(total: int) -> dict[str, Any]:
    return {
        "type": "event_msg",
        "timestamp": "2026-10-11T09:00:00Z",
        "payload": {"type": "token_count", "info": {"total_token_usage": {"input_tokens": total}}},
    }


def _usage_record(response: str) -> dict[str, Any]:
    return {
        "type": "token_usage_record",
        "timestamp": "2026-10-11T09:01:00Z",
        "payload": {"response_id": response, "usage": {"input_tokens": 10, "output_tokens": 2}},
    }


def _message_line(rid: str) -> dict[str, Any]:
    return {
        "timestamp": "2026-10-11T10:00:00Z",
        "requestId": rid,
        "message": {"id": rid, "usage": {"input_tokens": 3, "output_tokens": 1}},
    }


def _turn(index: int) -> dict[str, object]:
    return {
        "model": "model-r2",
        "input_token_count": 5,
        "output_token_count": 1,
        "end_timestamp": "2026-10-11T09:00:00Z",
        "result": {"Ok": {"id": f"turn-{index}"}},
    }


def _turn_doc(root: Path, name: str, document: object | None = None) -> Path:
    path = root / "sessions" / "cli" / f"{name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    body = document or {
        "session_id": name,
        "session_state": {"conversation_metadata": {"user_turn_metadatas": [_turn(1)]}},
    }
    path.write_text(json.dumps(body))
    return path


def _strip_flags(cursor: Cursor, rel: str, keys: tuple[str, ...]) -> Cursor:
    """The same cursor with some reader flags removed from one file's context, as an older writer
    left it."""
    key = file_key(rel)
    file = cursor.files[key]
    context = json.loads(file.context)
    reader = dict(context["r"])
    for name in keys:
        reader.pop(name, None)
    context["r"] = reader
    rewritten = FileCursor(
        file.dev,
        file.ino,
        file.offset,
        file.size,
        file.mtime_ns,
        file.head_digest,
        json.dumps(context, sort_keys=True, separators=(",", ":")),
    )
    return Cursor({**cursor.files, key: rewritten}, cursor.state, cursor.revision)


# -- every current obstruction sets retry_blocked ---


def test_document_open_failure_is_a_current_obstruction_and_malformed_json_is_not(
    tmp_path: Path,
) -> None:
    """Review an unopenable document sets retry_blocked; malformed JSON is historical skip
    evidence."""
    root = tmp_path / "turns"
    unreadable = _turn_doc(root, "unreadable")
    os.chmod(unreadable, 0)
    try:
        blocked = _run(TURNS, root)
    finally:
        os.chmod(unreadable, 0o600)
    assert blocked.retry_blocked is True

    malformed_root = tmp_path / "malformed"
    path = malformed_root / "sessions" / "cli" / "bad.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not json")
    batch = _run(TURNS, malformed_root)
    assert batch.retry_blocked is False and batch.coverage is Coverage.PARTIAL
    assert batch.next_cursor.state.get("o:") == "1"  # a never-committed document: uncertainty only


def test_whole_file_oversize_is_a_current_obstruction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A whole-file size refusal sets the obstruction flag in its
    pass."""
    monkeypatch.setattr(_files, "MAX_FILE_BYTES", 16)
    root = tmp_path / "message"
    path = root / "p" / "s.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(_message_line("big-1")) + "\n")
    batch = _run(MESSAGE, root)
    assert batch.retry_blocked is True
    assert batch.next_cursor.state.get("o:") == "1"  # refused before any FileCursor was made


def test_document_oversize_is_a_current_obstruction(tmp_path: Path) -> None:
    """A document over the bound is an obstruction, not a
    skip."""
    root = tmp_path / "turns"
    _turn_doc(root, "large")
    batch = _run(TURNS, root, budget=Budget(max_document_bytes=8))
    assert batch.retry_blocked is True
    assert batch.coverage is Coverage.PARTIAL


def test_the_parser_capacity_refusal_is_a_current_obstruction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The parser's capacity refusal sets the flag."""
    monkeypatch.setattr(_files, "MAX_FILE_CONTEXT_BYTES", 10)
    root = tmp_path / "turns"
    _turn_doc(root, "capped")
    batch = _run(TURNS, root)
    assert batch.retry_blocked is True


# -- legacy needs a verified end and a verified prefix ---


def test_a_complete_cumulative_line_followed_by_an_unterminated_record_is_not_legacy(
    tmp_path: Path,
) -> None:
    """A tail after the last complete line blocks the legacy
    verdict.
    The record completing later decides; no cursor is reset."""
    root = tmp_path / "rollout"
    path = root / "sessions" / "2026" / "10" / "11" / f"rollout-x-{UUID}.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(_token_count(9)) + "\n" + json.dumps(_usage_record("resp-7"))[:-1])
    first = _run(ROLLOUT, root)
    assert "g" not in _context(first.next_cursor, REL)
    assert first.coverage is Coverage.PARTIAL
    path.write_text(json.dumps(_token_count(9)) + "\n" + json.dumps(_usage_record("resp-7")) + "\n")
    second = _run(ROLLOUT, root, first.next_cursor)
    assert len(second.events) == 1  # the record is counted once it is complete
    assert "g" not in _context(second.next_cursor, REL)  # a per-call record exists: never legacy


def test_an_existing_cursor_without_usage_flags_has_an_unknown_prefix_not_a_legacy_verdict(
    tmp_path: Path,
) -> None:
    """A cursor with a positive offset and no u/l flags
    could hide
    per-call usage in its prefix. The suffix is judged unknown-origin, not legacy, and the offset is
    kept."""
    root = tmp_path / "rollout"
    path = root / "sessions" / "2026" / "10" / "11" / f"rollout-x-{UUID}.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(_usage_record("resp-prefix")) + "\n")
    first = _run(ROLLOUT, root)
    assert len(first.events) == 1
    old = _strip_flags(first.next_cursor, REL, ("u", "l", "pu"))  # an older writer's cursor
    offset_before = old.files[file_key(REL)].offset
    with path.open("a") as handle:
        handle.write(json.dumps(_token_count(4)) + "\n")
    second = _run(ROLLOUT, root, old)
    assert second.next_cursor.files[file_key(REL)].offset > offset_before  # not reset to zero
    context = _context(second.next_cursor, REL)
    assert "legacy" not in context.get("k", [])
    assert context["g"] == 1 and context["k"] == ["unknown"]
    assert second.coverage is Coverage.PARTIAL


def test_a_fresh_file_with_only_cumulative_lines_is_still_legacy(tmp_path: Path) -> None:
    """The other case (no prefix at all): a new file whose whole content is cumulative is legacy."""
    root = tmp_path / "rollout"
    path = root / "sessions" / "2026" / "10" / "11" / f"rollout-x-{UUID}.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(_token_count(9)) + "\n")
    batch = _run(ROLLOUT, root)
    assert _context(batch.next_cursor, REL)["k"] == ["legacy"]


# -- each source keeps its own cadence ---


def test_a_historical_idle_source_is_not_read_while_a_backlog_catches_up(tmp_path: Path) -> None:
    """An idle source is read at its own interval, not on every
    250 ms catch-up pass of the other source. Its next read comes at the normal interval."""
    root = tmp_path / "logs"
    root.mkdir()
    now = [NOW]
    idle, backlog = FakeReader(), FakeReader()
    idle.format_id, backlog.format_id = "format-idle", "format-backlog"
    idle.coverage = Coverage.PARTIAL
    idle.state = {"gap": "1"}
    backlog.more = True
    backlog.events = (_event("e"),)
    sources = (
        UsageSource("h1", "kind-idle", idle.format_id, root),
        UsageSource("h1", "kind-backlog", backlog.format_id, root),
    )
    with SqliteUsageStore(tmp_path / "state/usage.db", "UTC", clock=lambda: now[0]) as store:
        worker = UsageCollector(
            store,
            {idle.format_id: idle, backlog.format_id: backlog},
            sources,
            lambda: (),
            lambda: now[0],
        )
        for position in range(1, 5):
            backlog.state = {"position": str(position)}
            worker.run_once(now[0])
            assert worker.catching_up and worker.next_delay_ms == 250
            now[0] += 250
        assert len(backlog.cursors) == 4  # the catching-up source is read on each fast pass
        assert len(idle.cursors) == 1  # the idle source waited for its own due time
        assert worker.coverage["kind-idle"].coverage is Coverage.PARTIAL  # honest, not complete
        # Once its own interval is due, the idle source is read again at the normal pace.
        backlog.more = False
        backlog.events = ()
        now[0] = NOW + IDLE_POLL_MS + 1
        worker.run_once(now[0])
        assert len(idle.cursors) == 2


def _event(key: str) -> Any:
    from relaydeck.usage.contracts import Basis, Counters, UsageEvent

    return UsageEvent("s", key, NOW, None, Counters(1), Basis.PER_CALL)


def test_the_deadline_starved_source_is_retried_on_the_next_pass(tmp_path: Path) -> None:
    """A source the pass never reached stays due, so the next pass reads it."""
    root = tmp_path / "logs"
    root.mkdir()
    now = [NOW]
    slow, starved = FakeReader(), FakeReader()
    starved.format_id = "format-starved"
    slow.advance = lambda: now.__setitem__(0, now[0] + PASS_DEADLINE_MS + 1)
    sources = (
        UsageSource("h1", "kind-slow", slow.format_id, root),
        UsageSource("h1", "kind-starved", starved.format_id, root),
    )
    with SqliteUsageStore(tmp_path / "state/usage.db", "UTC", clock=lambda: now[0]) as store:
        worker = UsageCollector(
            store,
            {slow.format_id: slow, starved.format_id: starved},
            sources,
            lambda: (),
            lambda: now[0],
        )
        worker.run_once(now[0])
        assert len(starved.cursors) == 0
        worker.run_once(now[0])
        assert len(starved.cursors) == 1


# -- a migrated global gap marker is unknown, whatever the new unit adds -----------------------


def test_a_migrated_global_gap_marker_adds_unknown_next_to_a_new_reason(tmp_path: Path) -> None:
    """An older global g:<file> marker plus a new skip must keep
    unknown,
    not reduce to {skip}."""
    root = tmp_path / "message"
    path = root / "p" / "s.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text("{bad\n")
    key = file_key("p/s.jsonl")
    # The cursor carries the reader's schema (else the reader rewinds it), plus an older global
    # marker.
    cursor = Cursor({}, {SCHEMA_KEY: "message-4", GAP_PREFIX + key: "1"}, 0)
    with path.open("a") as handle:
        handle.write("{worse\n")
    batch = _run(MESSAGE, root, cursor)
    context = _context(batch.next_cursor, "p/s.jsonl")
    assert context["g"] == 1
    assert context["k"] == ["skip", "unknown"]
    assert GAP_PREFIX + key not in batch.next_cursor.state  # the old marker is migrated away
    assert reasons_of(batch.next_cursor.files[key]) == {"skip", "unknown"}


def test_a_mapping_handoff_makes_an_idle_source_due_at_once(tmp_path: Path) -> None:
    """Review rule: a source whose mappings changed since its last apply is due at once, so a
    durable
    handoff is confirmed promptly. An unchanged idle source keeps waiting for its own interval."""
    from relaydeck.usage.contracts import SessionMapping

    root = tmp_path / "logs"
    root.mkdir()
    now = [NOW]
    reader = FakeReader()
    reader.coverage = Coverage.PARTIAL
    reader.state = {"gap": "1"}
    source = UsageSource("h1", "kind-a", reader.format_id, root)
    maps: list[tuple[SessionMapping, ...]] = [()]
    with SqliteUsageStore(tmp_path / "state/usage.db", "UTC", clock=lambda: now[0]) as store:
        worker = UsageCollector(
            store,
            {reader.format_id: reader},
            (source,),
            lambda: maps[0],
            lambda: now[0],
        )
        worker.run_once(now[0])  # first apply records the mapping set
        now[0] += 1
        worker.run_once(now[0])
        assert len(reader.cursors) == 1  # unchanged and idle: not due yet
        maps[0] = (SessionMapping(source.source_key, "kind-a", "s1", "i1", "h1", "w1", 0, 2**62),)
        now[0] += 1
        worker.run_once(now[0])  # the mapping set changed: due at once
        assert len(reader.cursors) == 2
        now[0] += 1
        worker.run_once(now[0])  # the handoff pass made it due once more, to confirm the handoff
        assert len(reader.cursors) == 3
        now[0] += 1
        worker.run_once(now[0])  # the confirming pass changed nothing: back on its own cadence
        assert len(reader.cursors) == 3
