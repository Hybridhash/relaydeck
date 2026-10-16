"""The four format readers, on synthetic fixtures and temporary trees.

Synthetic data only: invented ids, numbers and timestamps. Covers the owner's record shapes
(message: iterations ignored, modelUsage summaries ignored, thinking tokens, sessionId key;
rollout: token_usage_record per call, compacted replays ignored, token_count for rate limits
only, secondary null; event: record representation only; session turns: identity priority,
pinned keys), streamed duplicates, subagents, cache buckets, partial lines, oversize lines,
truncation, rotation, same-size replacement, symlink escapes, budget exhaustion and exact resume,
document budgets, idempotent rereads, and the no-path rule.
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from relaydeck.usage.contracts import (
    Basis,
    Batch,
    Budget,
    Coverage,
    Cursor,
    LimitStatus,
    encoded_cursor_bytes,
    encoded_state_bytes,
)
from relaydeck.usage.readers import READERS
from relaydeck.usage.readers._files import key_text

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "usage" / "readers"
MESSAGE = "jsonl-message-usage-v1"
ROLLOUT = "jsonl-cumulative-token-count-v1"
EVENTS = "jsonl-event-usage-v1"
TURNS = "json-session-turns-v1"
UUID = "0a1b2c3d-1111-4000-8000-000000000001"
UUID2 = "0a1b2c3d-2222-4000-8000-000000000002"


def _copy(tmp_path: Path, name: str) -> Path:
    target = tmp_path / name
    shutil.copytree(FIXTURES / name, target)
    return target


def _turns(tmp_path: Path) -> Path:
    """The session-turns tree with only the first document (the second is for budget tests)."""
    root = _copy(tmp_path, "turns")
    (root / "sessions" / "cli" / "turns-two.json").unlink()
    return root


def _run(
    format_id: str, root: Path, cursor: Cursor | None = None, budget: Budget | None = None
) -> Batch:
    return READERS[format_id].read(root, cursor or Cursor(), budget or Budget())


def _keys(batch: Batch) -> set[tuple[str, str]]:
    return {(event.session_key, event.event_key) for event in batch.events}


def _write(path: Path, records: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(record, sort_keys=True) + "\n" for record in records))


# -- format 1: per-call message usage ------------------------------------------------------


def test_message_usage_reads_record_totals_and_ignores_iterations_and_summaries(
    tmp_path: Path,
) -> None:
    root = _copy(tmp_path, "message")
    batch = _run(MESSAGE, root)
    assert len(batch.events) == 4  # streamed copy dropped; summary and iterations add nothing
    first = [e for e in batch.events if e.counters.output == 50][0]
    assert first.session_key == "sess-one"
    assert first.counters.input_uncached == 100  # the record total, not the iterations
    assert first.counters.cache_read == 20
    assert (first.counters.cache_write_short, first.counters.cache_write_long) == (10, 5)
    assert first.counters.cache_write_unknown == 15
    assert first.counters.reasoning == 12  # thinking tokens are reasoning, inside output
    assert first.basis is Basis.PER_CALL and first.accounting_verified is True
    unsplit = [e for e in batch.events if e.counters.output == 5][0]
    assert unsplit.counters.cache_write_unknown == 7  # an unsplit creation is never priced as short
    sub = [e for e in batch.events if e.session_key == "agent-one"][0]
    assert sub.parent_session_key == "sess-one" and sub.counters.input_uncached == 4
    other = [e for e in batch.events if e.session_key == "sess-two-id"]
    assert len(other) == 1  # the record's sessionId wins over the file name
    assert batch.coverage is Coverage.PARTIAL  # a skipped record is a gap, never COMPLETE


def test_message_usage_partial_tail_waits_for_its_newline_then_completes(tmp_path: Path) -> None:
    root = tmp_path / "message"
    record_a = {
        "timestamp": "2026-10-11T10:00:00Z",
        "requestId": "a",
        "message": {"id": "a", "usage": {"input_tokens": 1, "output_tokens": 1}},
    }
    record_b = {
        "timestamp": "2026-10-11T10:00:01Z",
        "requestId": "b",
        "message": {"id": "b", "usage": {"input_tokens": 2, "output_tokens": 2}},
    }
    path = root / "p" / "s.jsonl"
    path.parent.mkdir(parents=True)
    line_a = json.dumps(record_a) + "\n"
    line_b = json.dumps(record_b)
    path.write_text(line_a + line_b)
    first = _run(MESSAGE, root)
    assert len(first.events) == 1
    (file_cursor,) = first.next_cursor.files.values()
    assert file_cursor.offset == len(line_a.encode())
    path.write_text(line_a + line_b + "\n")
    second = _run(MESSAGE, root, first.next_cursor)
    assert len(second.events) == 1 and second.events[0].counters.input_uncached == 2
    assert len(_keys(first) | _keys(second)) == 2


def test_message_usage_skips_oversize_lines_and_reads_the_next_record(tmp_path: Path) -> None:
    from relaydeck.usage.contracts import MAX_LINE_BYTES

    root = tmp_path / "message"
    path = root / "p" / "s.jsonl"
    path.parent.mkdir(parents=True)
    big = b"x" * (MAX_LINE_BYTES + 10)
    good = json.dumps(
        {
            "timestamp": "2026-10-11T10:00:00Z",
            "requestId": "g",
            "message": {"id": "g", "usage": {"input_tokens": 3, "output_tokens": 1}},
        }
    ).encode()
    path.write_bytes(big + b"\n" + good + b"\n")
    batch = _run(MESSAGE, root)
    assert len(batch.events) == 1 and batch.events[0].counters.input_uncached == 3
    # an oversize skip is permanent gap evidence: PARTIAL, never COMPLETE
    assert batch.skipped >= 1 and batch.coverage is Coverage.PARTIAL


def test_truncation_rotation_and_same_size_replacement_restart_the_file(tmp_path: Path) -> None:
    root = tmp_path / "message"
    path = root / "p" / "s.jsonl"

    def rec(rid: str) -> str:
        return json.dumps(
            {
                "timestamp": "2026-10-11T10:00:00Z",
                "requestId": rid,
                "message": {"id": rid, "usage": {"input_tokens": 5, "output_tokens": 1}},
            }
        )

    path.parent.mkdir(parents=True)
    path.write_text(rec("aa") + "\n" + rec("bb") + "\n")
    first = _run(MESSAGE, root)
    assert len(first.events) == 2
    path.write_text(rec("aa") + "\n")  # truncation
    truncated = _run(MESSAGE, root, first.next_cursor)
    assert _keys(truncated) <= _keys(first) and len(truncated.events) == 1
    path.write_text(rec("zz") + "\n")  # same size as the stored head, different bytes
    replaced = _run(MESSAGE, root, truncated.next_cursor)
    assert any(e.counters.input_uncached == 5 for e in replaced.events)
    tmp_new = root / "p" / "new.tmp"
    _write(
        tmp_new,
        [
            {
                "timestamp": "2026-10-11T10:00:00Z",
                "requestId": "rot",
                "message": {"id": "rot", "usage": {"input_tokens": 9, "output_tokens": 1}},
            }
        ],
    )
    os.replace(tmp_new, path)  # rotation
    rotated = _run(MESSAGE, root, replaced.next_cursor)
    assert any(e.counters.input_uncached == 9 for e in rotated.events)


def test_a_symlink_that_escapes_the_root_is_ignored(tmp_path: Path) -> None:
    root = tmp_path / "message"
    (root / "p").mkdir(parents=True)
    outside = tmp_path / "outside.jsonl"
    _write(
        outside,
        [
            {
                "timestamp": "2026-10-11T10:00:00Z",
                "requestId": "o",
                "message": {"id": "o", "usage": {"input_tokens": 1}},
            }
        ],
    )
    os.symlink(outside, root / "p" / "link.jsonl")
    batch = _run(MESSAGE, root)
    assert batch.events == () and batch.skipped >= 1


def test_budget_exhaustion_is_partial_and_resumes_exactly(tmp_path: Path) -> None:
    root = tmp_path / "message"
    for index in range(3):
        _write(
            root / f"p{index}" / "s.jsonl",
            [
                {
                    "timestamp": "2026-10-11T10:00:00Z",
                    "requestId": f"r{index}",
                    "message": {"id": f"m{index}", "usage": {"input_tokens": index + 1}},
                }
            ],
        )
    cursor, seen, statuses = Cursor(), set(), []
    for _ in range(5):
        batch = _run(MESSAGE, root, cursor, Budget(max_files=1))
        seen |= _keys(batch)
        statuses.append(batch.coverage)
        cursor = batch.next_cursor
        if batch.coverage is Coverage.COMPLETE:
            break
    assert len(seen) == 3 and statuses[0] is Coverage.PARTIAL and statuses[-1] is Coverage.COMPLETE


def test_reread_is_idempotent_and_keeps_the_same_event_keys(tmp_path: Path) -> None:
    root = _copy(tmp_path, "message")
    first = _run(MESSAGE, root)
    assert _keys(first) == _keys(_run(MESSAGE, root))
    after = _run(MESSAGE, root, first.next_cursor)
    # the fixture's malformed tail is a persisted gap, so a reread stays PARTIAL (never COMPLETE)
    assert after.events == () and after.coverage is Coverage.PARTIAL


# -- format 2: per-call rollout records and rate limits -------------------------------------


def test_rollout_counts_token_usage_records_and_ignores_compacted_replays(tmp_path: Path) -> None:
    root = tmp_path / "rollout"
    src = FIXTURES / "rollout" / "sessions" / "2026" / "10" / "11"
    dst = root / "sessions" / "2026" / "10" / "11"
    dst.mkdir(parents=True)
    shutil.copy(src / f"rollout-2026-10-11T00-00-00-{UUID}.jsonl", dst)
    batch = _run(ROLLOUT, root)
    by_response = {e.event_key: e for e in batch.events}
    assert len(batch.events) == 2  # the compacted replay of resp-1 is not counted again
    first = by_response[key_text("response", "resp-1")]
    assert (first.counters.input_uncached, first.counters.cache_read) == (50, 40)
    assert (
        first.counters.cache_write_unknown,
        first.counters.output,
        first.counters.reasoning,
    ) == (10, 30, 10)
    assert first.model == "model-a" and first.session_key == UUID
    second = by_response[key_text("response", "resp-2")]
    assert (second.counters.input_uncached, second.counters.output, second.counters.reasoning) == (
        50,
        8,
        2,
    )
    assert second.model == "model-b" and second.basis is Basis.PER_CALL
    assert batch.coverage is Coverage.COMPLETE


def test_rollout_rate_limits_latest_per_quota_with_secondary_null(tmp_path: Path) -> None:
    root = _copy(tmp_path, "rollout")
    (
        root / "sessions" / "2026" / "10" / "11" / f"rollout-2026-10-11T00-00-00-{UUID2}.jsonl"
    ).unlink()
    batch = _run(ROLLOUT, root)
    (limit,) = batch.limits
    assert limit.quota_key == "lim-one" and limit.kind == ""
    assert [w.window_key for w in limit.windows] == ["primary"]  # secondary is null now
    primary = limit.windows[0]
    assert primary.used_percent == 25.0 and primary.window_minutes == 300
    assert primary.resets_at_ms == 1800000600 * 1000 and primary.status is LimitStatus.FRESH
    assert batch.coverage is Coverage.COMPLETE


def test_rollout_records_without_response_id_or_with_negative_input_are_partial(
    tmp_path: Path,
) -> None:
    root = _copy(tmp_path, "rollout")
    batch = _run(ROLLOUT, root)
    assert len(batch.events) == 2  # the main file only
    assert batch.skipped == 2  # a record with no response id, and a negative uncached result
    assert batch.coverage is Coverage.PARTIAL


def test_rollout_same_response_key_is_stable_across_rereads(tmp_path: Path) -> None:
    root = _copy(tmp_path, "rollout")
    assert _keys(_run(ROLLOUT, root)) == _keys(_run(ROLLOUT, root))


# -- format 3: record representation only ---------------------------------------------------


def test_event_usage_never_emits_from_the_legacy_usage_object(tmp_path: Path) -> None:
    root = tmp_path / "events"
    _write(
        root / "sessions" / "2026" / "10" / "11" / "0009" / "session.jsonl",
        [
            {
                "timestamp": "2026-10-11T11:00:00Z",
                "payload": {
                    "event": {
                        "kind": "model_completed",
                        "usage": {"input_tokens": 9, "output_tokens": 9},
                    }
                },
            }
        ],
    )
    batch = _run(EVENTS, root)
    assert batch.events == ()


# -- format 4: whole-file session turns and pinned keys -------------------------------------


def _event_usage_record(
    usage_id: str,
    session: str = "evt-sess",
    *,
    reported: object = True,
    recorded_at: object = None,
    unit: str = "tokens",
) -> dict[str, object]:
    stamp = recorded_at if recorded_at is not None else micro_of("2026-10-11T11:00:00Z")
    quantity = {
        "input_tokens": 80,
        "output_tokens": 20,
        "cached_tokens": 30,
        "reasoning_tokens": 5,
        "unit": unit,
        "reported": reported,
    }
    return {
        "causation_id": "c",
        "durability": "durable",
        "id": f"row-{usage_id}",
        "payload": {
            "event": {
                "kind": "goal_usage_attribution",
                "model": "model-c",
                "record": {
                    "owner": {"session_id": session},
                    "quantity": quantity,
                    "usage_id": usage_id,
                },
            }
        },
        "payload_type": "usage",
        "record_type": "event",
        "recorded_at": stamp,
        "schema_version": 1,
    }


def micro_of(iso: str) -> int:
    from relaydeck.usage.readers._files import ms_of

    return int(ms_of(iso) or 0) * 1000


def test_event_usage_reads_reported_records_at_recorded_at_and_ignores_estimates(
    tmp_path: Path,
) -> None:
    root = _copy(tmp_path, "events")
    batch = _run(EVENTS, root)
    (event,) = batch.events  # estimates, bytes and legacy objects are out; the replay is one key
    assert event.session_key == "evt-sess" and event.event_key == key_text("usage-id", "u-1")
    assert event.occurred_ms == micro_of("2026-10-11T11:00:02Z") // 1000  # the replay's stamp
    assert event.counters.input_uncached == 50  # cached tokens are inside the input
    assert (event.counters.cache_read, event.counters.output, event.counters.reasoning) == (
        30,
        20,
        5,
    )
    assert event.accounting_verified is False
    assert batch.coverage is Coverage.UNVERIFIED  # estimates are not gaps


def test_event_recorded_at_must_be_an_integer_within_range_or_it_is_a_gap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import relaydeck.usage.readers.jsonl_event_usage as events

    monkeypatch.setattr(events, "_now_ms", lambda: micro_of("2026-10-11T12:00:00Z") // 1000)
    root = tmp_path / "events"
    _write(
        root / "sessions" / "2026" / "10" / "11" / "0005" / "session.jsonl",
        [
            _event_usage_record("ok-1"),
            _event_usage_record("bool-1", recorded_at=True),
            _event_usage_record("float-1", recorded_at=1.5e15),
            _event_usage_record("early-1", recorded_at=micro_of("1999-12-31T23:59:59Z")),
            _event_usage_record("future-1", recorded_at=micro_of("2026-10-14T12:00:00Z")),
        ],
    )
    batch = _run(EVENTS, root)
    assert [e.event_key for e in batch.events] == [key_text("usage-id", "ok-1")]
    assert batch.coverage is Coverage.PARTIAL and batch.skipped == 4


def test_event_schema_change_restarts_an_old_cursor_and_the_keys_stay_stable(
    tmp_path: Path,
) -> None:
    root = tmp_path / "events"
    _write(
        root / "sessions" / "2026" / "10" / "11" / "0006" / "session.jsonl",
        [_event_usage_record("v-1")],
    )
    first = _run(EVENTS, root)
    old = Cursor(
        files=first.next_cursor.files,
        state={k: v for k, v in first.next_cursor.state.items() if k != "v:"},
    )
    replay = _run(EVENTS, root, old)
    assert [e.event_key for e in replay.events] == [e.event_key for e in first.events]
    assert replay.next_cursor.state.get("v:") == "events-4"


def test_event_record_whose_unit_is_not_tokens_is_ignored(tmp_path: Path) -> None:
    root = tmp_path / "events"
    _write(
        root / "sessions" / "2026" / "10" / "11" / "0004" / "session.jsonl",
        [_event_usage_record("bytes-1", unit="bytes")],
    )
    assert _run(EVENTS, root).events == ()


def test_session_turns_key_by_identity_priority_and_skip_zero_turns(tmp_path: Path) -> None:
    root = _turns(tmp_path)
    batch = _run(TURNS, root)
    keys = {e.event_key for e in batch.events}
    assert len(batch.events) == 3  # the all-zero turn is not an event
    assert key_text("turn", "turn-id-1") in keys
    assert key_text("turn", "msg-b") in keys
    assert key_text("turn-index", 3, "turns-one") in keys
    first = [e for e in batch.events if e.event_key == key_text("turn", "turn-id-1")][0]
    assert first.counters.cache_read == 100 and first.counters.cache_write_unknown == 20
    assert first.parent_session_key == "turns-root" and first.accounting_verified is False
    assert batch.coverage is Coverage.UNVERIFIED


def _rewrite_turns(root: Path, mutate) -> None:  # type: ignore[no-untyped-def]
    path = root / "sessions" / "cli" / "turns-one.json"
    document = json.loads(path.read_text())
    mutate(document["session_state"]["conversation_metadata"]["user_turn_metadatas"])
    path.write_text(json.dumps(document, sort_keys=True))


def test_fallback_to_primary_keeps_the_pinned_key_and_adds_no_event(tmp_path: Path) -> None:
    root = _turns(tmp_path)
    first = _run(TURNS, root)
    before = _keys(first)

    def gain_primary(turns: list) -> None:  # type: ignore[type-arg]
        turns[1]["result"] = {"Ok": {"id": "turn-id-b"}}

    _rewrite_turns(root, gain_primary)
    second = _run(TURNS, root, first.next_cursor)
    assert _keys(second) == before and len(second.events) == 3


def test_reordered_turns_keep_the_keys_of_turns_with_ids(tmp_path: Path) -> None:
    root = _turns(tmp_path)
    first = _run(TURNS, root)

    def swap(turns: list) -> None:  # type: ignore[type-arg]
        turns[0], turns[1] = turns[1], turns[0]

    _rewrite_turns(root, swap)
    second = _run(TURNS, root, first.next_cursor)
    assert _keys(second) == _keys(first)


def test_a_restart_with_the_stored_state_keeps_the_keys(tmp_path: Path) -> None:
    root = _turns(tmp_path)
    first = _run(TURNS, root)
    stored = Cursor(
        files=dict(first.next_cursor.files),
        state=dict(first.next_cursor.state),
        revision=first.next_cursor.revision,
    )

    def gain_primary(turns: list) -> None:  # type: ignore[type-arg]
        turns[1]["result"] = {"Ok": {"id": "turn-id-b"}}

    _rewrite_turns(root, gain_primary)
    restarted = _run(TURNS, root, stored)
    assert _keys(restarted) == _keys(first)


def test_a_rewrite_that_corrects_a_timestamp_keeps_the_key_and_replaces(tmp_path: Path) -> None:
    root = _turns(tmp_path)
    before = {e.event_key: e for e in _run(TURNS, root).events}[key_text("turn", "turn-id-1")]

    def correct(turns: list) -> None:  # type: ignore[type-arg]
        turns[0]["end_timestamp"] = "2026-10-12T00:30:00Z"

    _rewrite_turns(root, correct)
    after = {e.event_key: e for e in _run(TURNS, root).events}[key_text("turn", "turn-id-1")]
    assert after.event_key == before.event_key and after.occurred_ms != before.occurred_ms


def test_an_oversize_document_is_skipped_and_partial(tmp_path: Path) -> None:
    root = _turns(tmp_path)
    batch = _run(TURNS, root, budget=Budget(max_document_bytes=10))
    assert batch.events == () and batch.coverage is Coverage.PARTIAL and batch.skipped >= 1


def test_a_large_document_is_the_first_unit_then_the_pass_ends_partial(tmp_path: Path) -> None:
    root = _copy(tmp_path, "turns")
    # The reader visits newest first; checkout order must not decide which document that is.
    os.utime(root / "sessions" / "cli" / "turns-two.json", (1_000_000_000, 1_000_000_000))
    tiny = Budget(max_bytes=10)
    first = _run(TURNS, root, budget=tiny)
    assert first.coverage is Coverage.PARTIAL and len(first.events) == 3
    second = _run(TURNS, root, first.next_cursor, tiny)
    assert len(second.events) == 1 and second.coverage is Coverage.UNVERIFIED  # nothing remains
    third = _run(TURNS, root, second.next_cursor, tiny)
    assert third.events == () and third.coverage is Coverage.UNVERIFIED


# -- shared guarantees ----------------------------------------------------------------------


@pytest.mark.parametrize("format_id", [MESSAGE, ROLLOUT, EVENTS, TURNS])
def test_a_missing_root_is_unavailable_and_nothing_escapes(format_id: str, tmp_path: Path) -> None:
    batch = _run(format_id, tmp_path / "missing-root")
    assert batch.coverage is Coverage.UNAVAILABLE and batch.events == ()
    assert str(tmp_path) not in repr(batch)


def test_batch_repr_carries_no_fixture_path_or_identity(tmp_path: Path) -> None:
    root = _copy(tmp_path, "message")
    batch = _run(MESSAGE, root)
    text = repr(batch) + repr(batch.events) + repr(batch.next_cursor)
    assert str(tmp_path) not in text and "sess-one" not in text and "msg-1" not in text


# Live family and timestamp shapes ---------------------------------------------


@pytest.mark.parametrize(
    "stamp,expected",
    [
        (1789000000, 1789000000000),  # 10 digits: Unix seconds, the live unit
        (1789000000000, 1789000000000),  # 13 digits: Unix milliseconds
        (10**11 + 5, None),  # between the two ranges: skipped, not guessed
    ],
)
def test_turn_timestamp_fallback_reads_seconds_and_milliseconds_and_skips_the_rest(
    tmp_path: Path, stamp: int, expected: int | None
) -> None:
    root = tmp_path / "turns"
    _write_turn_document(
        root,
        {
            "result": {"Ok": {"id": "stamp-turn", "meta": {"timestamp": stamp}}},
            "model": "model-s",
            "input_token_count": 3,
            "output_token_count": 1,
        },
    )
    batch = _run(TURNS, root)
    if expected is None:
        assert batch.events == () and batch.skipped == 1 and batch.coverage is Coverage.PARTIAL
    else:
        (event,) = batch.events
        assert event.occurred_ms == expected


def _write_turn_document(root: Path, turn: dict[str, object]) -> None:
    path = root / "sessions" / "cli" / "stamp.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "session_id": "stamp",
                "session_state": {"conversation_metadata": {"user_turn_metadatas": [turn]}},
            }
        )
    )


# Review reader must-fixes and newest-first scanning ----------------------------


def _rollout_file(root: Path, uuid: str, records: list[dict[str, object]]) -> Path:
    path = root / "sessions" / "2026" / "10" / "11" / f"rollout-x-{uuid}.jsonl"
    _write(path, records)
    return path


def _usage_record(response: str, stamp: str = "2026-10-11T09:00:00Z") -> dict[str, object]:
    return {
        "type": "token_usage_record",
        "timestamp": stamp,
        "payload": {
            "response_id": response,
            "usage": {"input_tokens": 10, "cached_input_tokens": 0, "output_tokens": 2},
        },
    }


def test_refused_event_does_not_advance_the_offset_and_the_next_pass_emits_it(
    tmp_path: Path,
) -> None:
    root = tmp_path / "rollout"
    _rollout_file(
        root, UUID, [_usage_record("resp-a"), _usage_record("resp-b", "2026-10-11T09:01:00Z")]
    )
    first = _run(ROLLOUT, root, budget=Budget(max_events=1))
    assert len(first.events) == 1 and first.coverage is Coverage.PARTIAL
    second = _run(ROLLOUT, root, first.next_cursor)
    assert len(second.events) == 1 and not (_keys(first) & _keys(second))
    assert second.coverage is Coverage.COMPLETE


def test_refused_turn_is_not_lost_and_the_document_resumes_at_its_index(tmp_path: Path) -> None:
    root = _turns(tmp_path)
    everything = _keys(_run(TURNS, root))
    first = _run(TURNS, root, budget=Budget(max_events=1))
    assert first.coverage is Coverage.PARTIAL and len(first.events) == 1
    seen = _keys(first)
    cursor = first.next_cursor
    for _ in range(6):
        step = _run(TURNS, root, cursor, Budget(max_events=1))
        seen |= _keys(step)
        cursor = step.next_cursor
        if step.coverage is not Coverage.PARTIAL:
            break
    assert seen == everything  # every turn once, none lost
    assert step.coverage is Coverage.UNVERIFIED  # complete only after the last turn


def test_index_only_identity_keeps_its_key_when_a_primary_id_appears(tmp_path: Path) -> None:
    root = tmp_path / "turns"
    _write_turn_document(
        root,
        {
            "model": "model-i",
            "input_token_count": 5,
            "output_token_count": 1,
            "end_timestamp": "2026-10-11T09:00:00Z",
        },
    )
    first = _run(TURNS, root)
    before = _keys(first)
    _write_turn_document(
        root,
        {
            "result": {"Ok": {"id": "later-primary"}},
            "model": "model-i",
            "input_token_count": 5,
            "output_token_count": 1,
            "end_timestamp": "2026-10-11T09:00:00Z",
        },
    )
    second = _run(TURNS, root, first.next_cursor)
    assert second.events == () or _keys(second) == before  # no second event
    assert _keys(first) == before


def test_pins_are_never_evicted_and_a_bound_stops_the_session_with_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import relaydeck.usage.readers._files as files_module

    root = _turns(tmp_path)
    monkeypatch.setattr(files_module, "MAX_FILE_CONTEXT_BYTES", 200)
    batch = _run(TURNS, root)
    assert batch.coverage is Coverage.PARTIAL  # pins would exceed the bound: stopped, not evicted
    for file in batch.next_cursor.files.values():
        assert len(file.context.encode()) <= 200


def test_ledger_sizes_match_the_shared_cursor_encoding(tmp_path: Path) -> None:
    from relaydeck.usage.readers._files import Ledger

    root = _copy(tmp_path, "rollout")
    batch = _run(ROLLOUT, root)
    ledger = Ledger(batch.next_cursor.files, batch.next_cursor.state)
    assert ledger.encoded_bytes() == encoded_cursor_bytes(batch.next_cursor)


def test_encoded_state_bytes_matches_the_store_encoding(tmp_path: Path) -> None:
    import json as json_module

    state = {"f:k1": '{"sid":"é","model":"m"}', "t:k2": '{"n":1}'}
    store = json_module.dumps(dict(state), sort_keys=True, separators=(",", ":"), allow_nan=False)
    assert encoded_state_bytes(state) == len(store.encode())
    assert encoded_state_bytes({"é": "x"}) == len(b'{"\\u00e9":"x"}')  # escaping counts


def test_discovery_selects_only_json_documents(tmp_path: Path) -> None:
    root = _turns(tmp_path)
    (root / "sessions" / "cli" / "notes.jsonl").write_text(json.dumps({"session_id": "x"}))
    (root / "sessions" / "cli" / "old.history").write_text("{}")
    batch = _run(TURNS, root)
    assert all(e.session_key != "x" for e in batch.events)


def test_discovery_is_newest_first_so_a_small_budget_reaches_today_first(tmp_path: Path) -> None:
    root = tmp_path / "message"
    old = root / "old" / "s.jsonl"
    new = root / "new" / "s.jsonl"
    for path, rid in ((old, "old"), (new, "new")):
        _write(
            path,
            [
                {
                    "timestamp": "2026-10-11T10:00:00Z",
                    "requestId": rid,
                    "message": {"id": rid, "usage": {"input_tokens": 1}},
                }
            ],
        )
    os.utime(old, (1_000_000_000, 1_000_000_000))
    os.utime(new, (2_000_000_000, 2_000_000_000))
    batch = _run(MESSAGE, root, budget=Budget(max_files=1))
    assert [e.session_key for e in batch.events] == ["s"]
    assert batch.events[0].occurred_ms == ms_of_stamp("2026-10-11T10:00:00Z")


def ms_of_stamp(value: str) -> int:
    from datetime import datetime

    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)


def test_a_terminated_line_longer_than_the_budget_finishes_across_passes(tmp_path: Path) -> None:
    from relaydeck.usage.contracts import MAX_LINE_BYTES

    root = tmp_path / "message"
    path = root / "p" / "s.jsonl"
    path.parent.mkdir(parents=True)
    big = b"x" * (MAX_LINE_BYTES + 200_000)
    good = json.dumps(
        {
            "timestamp": "2026-10-11T10:00:00Z",
            "requestId": "after",
            "message": {"id": "after", "usage": {"input_tokens": 7}},
        }
    ).encode()
    path.write_bytes(big + b"\n" + good + b"\n")
    cursor, events, passes = Cursor(), [], 0
    tiny = Budget(max_bytes=300_000)
    while passes < 20:
        batch = _run(MESSAGE, root, cursor, tiny)
        events.extend(batch.events)
        cursor = batch.next_cursor
        passes += 1
        if batch.coverage is Coverage.COMPLETE:
            break
    assert passes > 1 and any(e.counters.input_uncached == 7 for e in events)  # no starvation


def test_a_permission_failure_is_sanitized_counted_and_partial(tmp_path: Path) -> None:
    root = tmp_path / "message"
    path = root / "p" / "s.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "timestamp": "2026-10-11T10:00:00Z",
                "requestId": "z",
                "message": {"id": "z", "usage": {"input_tokens": 1}},
            }
        )
        + "\n"
    )
    os.chmod(path, 0)
    try:
        batch = _run(MESSAGE, root)  # must not raise, and must not name the path
        assert str(tmp_path) not in repr(batch)
        assert batch.coverage is Coverage.PARTIAL and batch.skipped >= 1
    finally:
        os.chmod(path, 0o600)


def test_a_symlinked_directory_inside_the_root_is_ignored(tmp_path: Path) -> None:
    root = tmp_path / "message"
    outside = tmp_path / "outside-dir"
    outside.mkdir()
    _write(
        outside / "s.jsonl",
        [
            {
                "timestamp": "2026-10-11T10:00:00Z",
                "requestId": "o",
                "message": {"id": "o", "usage": {"input_tokens": 1}},
            }
        ],
    )
    root.mkdir()
    os.symlink(outside, root / "linked")
    batch = _run(MESSAGE, root)
    assert batch.events == () and batch.skipped >= 1


def test_a_legacy_cumulative_only_file_is_partial_not_complete(tmp_path: Path) -> None:
    root = tmp_path / "rollout"
    _rollout_file(
        root,
        UUID,
        [
            {
                "type": "event_msg",
                "timestamp": "2026-10-11T09:00:00Z",
                "payload": {
                    "type": "token_count",
                    "info": {"total_token_usage": {"input_tokens": 9}},
                },
            }
        ],
    )
    first = _run(ROLLOUT, root)
    assert first.events == () and first.coverage is Coverage.PARTIAL
    second = _run(ROLLOUT, root, first.next_cursor)
    assert second.coverage is Coverage.PARTIAL  # the gap evidence persists


def test_an_incomplete_tail_makes_the_batch_partial(tmp_path: Path) -> None:
    root = tmp_path / "message"
    path = root / "p" / "s.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "timestamp": "2026-10-11T10:00:00Z",
                "requestId": "t",
                "message": {"id": "t", "usage": {"input_tokens": 1}},
            }
        )
        + "\n"
        + '{"partial"'
    )
    batch = _run(MESSAGE, root)
    assert len(batch.events) == 1 and batch.coverage is Coverage.PARTIAL


def _write_turn_document(root: Path, turn: dict[str, object]) -> None:
    path = root / "sessions" / "cli" / "one-turn.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "session_id": "one-turn",
                "session_state": {"conversation_metadata": {"user_turn_metadatas": [turn]}},
            }
        )
    )


# Review probes (discovery, capacity, gap evidence, identity, Batch.more) ---------


def _message_line(rid: str, tokens: int, stamp: str = "2026-10-11T10:00:00Z") -> dict[str, object]:
    return {
        "timestamp": stamp,
        "requestId": rid,
        "message": {"id": rid, "usage": {"input_tokens": tokens, "output_tokens": 1}},
    }


def _day_file(root: Path, day: str, n: int, records: list[dict[str, object]]) -> Path:
    path = (
        root
        / "sessions"
        / "2026"
        / "10"
        / day
        / f"rollout-x-0a1b2c3d-{n:04d}-4000-8000-000000000001.jsonl"
    )
    _write(path, records)
    return path


def _turn(index: int, ident: str | None = None, tokens: int = 5) -> dict[str, object]:
    turn: dict[str, object] = {
        "model": "model-r2",
        "input_token_count": tokens,
        "output_token_count": 1,
        "end_timestamp": "2026-10-11T09:00:00Z",
    }
    if ident is not None:
        turn["result"] = {"Ok": {"id": ident}}
    return turn


def _turn_doc(root: Path, name: str, turns: list[object], mtime: int | None = None) -> Path:
    path = root / "sessions" / "cli" / f"{name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "session_id": name,
                "session_state": {"conversation_metadata": {"user_turn_metadatas": turns}},
            }
        )
    )
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def _gap_files(cursor: Cursor) -> set[str]:
    """File keys whose permanent gap evidence is recorded in their own context."""
    return {
        key for key, file in cursor.files.items() if json.loads(file.context or "{}").get("g") == 1
    }


def _rollout_day(root: Path, day: str, n: int, stamp: str) -> None:
    _day_file(root, day, n, [_usage_record(f"r{n}", stamp)])


def test_enumeration_lists_only_the_layout_and_ignores_sibling_trees(tmp_path: Path) -> None:
    root = tmp_path / "rollout"
    for n, day in enumerate(["09", "10", "11", "11", "12"]):
        _rollout_day(root, day, n, f"2026-10-{day}T09:00:00Z")
    plugins = root / "plugins"
    for n in range(3000):  # unrelated sibling content: never listed, never read
        path = plugins / f"pkg-{n // 100}" / f"file-{n}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}\n")
    batch = _run(ROLLOUT, root)
    assert len({e.event_key for e in batch.events}) == 5
    assert batch.coverage is Coverage.COMPLETE and batch.more is False


def test_enumeration_caps_are_incomplete_preserve_cursors_and_clear_when_complete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import relaydeck.usage.readers._files as files

    root = tmp_path / "message"
    for n in range(4):
        _write(root / "p" / f"s{n}.jsonl", [_message_line(f"e{n}", 1)])
    first = _run(MESSAGE, root)
    assert first.coverage is Coverage.COMPLETE
    monkeypatch.setattr(files, "MAX_DIR_ENTRIES", 2)  # the project directory now overflows its cap
    capped = _run(MESSAGE, root, first.next_cursor)
    assert capped.coverage is Coverage.PARTIAL and capped.more is False
    assert "e:" in capped.next_cursor.state  # the enumeration gap is marked
    assert set(capped.next_cursor.files) == set(first.next_cursor.files)  # nothing deleted
    monkeypatch.setattr(files, "MAX_DIR_ENTRIES", 4096)
    restored = _run(MESSAGE, root, capped.next_cursor)
    assert restored.coverage is Coverage.COMPLETE and "e:" not in restored.next_cursor.state


def test_absent_checkpoints_are_deleted_only_after_a_complete_enumeration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import relaydeck.usage.readers._files as files

    root = tmp_path / "message"
    for name in ("keep", "gone", "extra"):
        _write(root / "p" / f"{name}.jsonl", [_message_line(f"{name}-1", 1)])
    first = _run(MESSAGE, root)
    assert len(first.next_cursor.files) == 3
    (root / "p" / "gone.jsonl").unlink()
    monkeypatch.setattr(files, "MAX_DIR_ENTRIES", 1)  # two entries remain, the cap is one
    unknown = _run(MESSAGE, root, first.next_cursor)
    assert len(unknown.next_cursor.files) == 3  # absence unknown: every checkpoint is kept
    assert unknown.coverage is Coverage.PARTIAL and "e:" in unknown.next_cursor.state
    monkeypatch.setattr(files, "MAX_DIR_ENTRIES", 4096)
    known = _run(MESSAGE, root, unknown.next_cursor)
    assert len(known.next_cursor.files) == 2  # a complete enumeration established the absence


def test_a_slow_enumeration_reads_what_it_found_and_never_reports_more(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import relaydeck.usage.readers._files as files

    root = tmp_path / "rollout"
    for n in range(100):
        _rollout_day(root, "11", n, "2026-10-11T09:00:00Z")
    clock = [0.0]

    def tick() -> float:
        clock[0] += 0.001  # every clock read costs 1 ms of "time"
        return clock[0]

    monkeypatch.setattr(files.time, "monotonic", tick)
    batch = _run(ROLLOUT, root, Cursor(), Budget(deadline_ms=100))
    assert len(batch.events) > 0  # the enumeration was cut short; what it found was read
    assert batch.coverage is Coverage.PARTIAL and batch.more is False


def test_a_capacity_block_stops_only_its_document_and_keeps_its_pins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import relaydeck.usage.readers._files as files

    root = _turns(tmp_path)  # the first document is removed by the helper below
    for path in (root / "sessions" / "cli").glob("*.json"):
        path.unlink()
    big = [_turn(i, f"big-{i}") for i in range(4)]
    _turn_doc(root, "older-big", big, mtime=1_700_000_000)  # older: processed after the newer one
    _turn_doc(root, "newer-small", [_turn(0, "small-0")], mtime=1_800_000_000)
    monkeypatch.setattr(files, "MAX_FILE_CONTEXT_BYTES", 340)
    first = _run(TURNS, root)
    ids = {event.session_key for event in first.events}
    assert len(ids) == 2  # the newer document was emitted despite the blocked one
    assert first.coverage is Coverage.PARTIAL and first.more is False  # a block is not backlog
    blocked = [e for e in first.events if e.session_key == "older-big"]
    assert 0 < len(blocked) < 4  # its committed turns are kept; the rest wait for room
    monkeypatch.setattr(files, "MAX_FILE_CONTEXT_BYTES", 4096)
    second = _run(TURNS, root, first.next_cursor)
    assert len({e.event_key for e in first.events + second.events}) == 5
    assert second.coverage is Coverage.UNVERIFIED and second.more is False


def test_unvisited_files_keep_their_cursors_and_gap_evidence(tmp_path: Path) -> None:
    root = tmp_path / "message"
    _write(root / "p" / "a.jsonl", [_message_line("a1", 2)])
    path_b = root / "p" / "b.jsonl"
    path_b.parent.mkdir(parents=True, exist_ok=True)
    path_b.write_text(json.dumps(_message_line("b1", 4)) + "\n{bad\n")
    first = _run(MESSAGE, root)
    assert _gap_files(first.next_cursor)
    with path_b.open("a") as handle:
        handle.write(json.dumps(_message_line("b2", 1)) + "\n")  # b changed: it needs a read
    second = _run(MESSAGE, root, first.next_cursor, Budget(max_files=0))  # discovered, not read
    assert second.events == () and second.more is True
    assert set(second.next_cursor.files) == set(first.next_cursor.files)
    assert _gap_files(second.next_cursor) == _gap_files(first.next_cursor)


def test_a_historical_gap_survives_a_clean_suffix(tmp_path: Path) -> None:
    root = tmp_path / "message"
    path = root / "p" / "s.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(_message_line("h1", 2)) + "\n{bad\n" + json.dumps(_message_line("h2", 3)) + "\n"
    )
    first = _run(MESSAGE, root)
    assert first.coverage is Coverage.PARTIAL
    with path.open("a") as handle:
        handle.write(json.dumps(_message_line("h3", 4)) + "\n")
    second = _run(MESSAGE, root, first.next_cursor)
    assert len(second.events) == 1  # the clean suffix is read
    assert second.coverage is Coverage.PARTIAL  # but the historical gap stays
    assert _gap_files(second.next_cursor)


def test_an_unchanged_turn_document_keeps_its_gap(tmp_path: Path) -> None:
    root = tmp_path / "turns"
    _turn_doc(root, "gappy", ["junk", _turn(1, "ok-1")])
    first = _run(TURNS, root)
    assert first.coverage is Coverage.PARTIAL
    second = _run(TURNS, root, first.next_cursor)
    assert second.events == () and second.coverage is Coverage.PARTIAL
    assert _gap_files(second.next_cursor)


def test_oversize_skips_are_gap_evidence_and_gap_overflow_keeps_a_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import dataclasses

    import relaydeck.usage.readers._files as files
    from relaydeck.usage.contracts import encoded_cursor_bytes

    root = tmp_path / "message"
    for n in range(5):
        path = root / "p" / f"s{n}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(_message_line(f"m{n}", 1)) + "\n{bad\n")
    gapped = _run(MESSAGE, root)
    assert _gap_files(gapped.next_cursor) == set(gapped.next_cursor.files)  # gaps in context
    # A cursor bound that leaves no room for the gap flags: gaps are summarised, nothing blocked.
    ungapped = Cursor(
        files={k: dataclasses.replace(f, context="") for k, f in gapped.next_cursor.files.items()},
        state=gapped.next_cursor.state,
    )
    # The bound leaves room for the reserved global markers ("e:", "o:") and nothing for flags.
    monkeypatch.setattr(files, "MAX_CURSOR_BYTES", encoded_cursor_bytes(ungapped) + 30)
    tight = _run(MESSAGE, root)
    assert tight.coverage is Coverage.PARTIAL
    assert "o:" in tight.next_cursor.state  # gap evidence that did not fit is summarised
    assert len(tight.next_cursor.files) == 5  # no file was blocked or lost by the overflow


def test_a_partial_tail_is_partial_but_not_gap_evidence_and_resolves_on_growth(
    tmp_path: Path,
) -> None:
    root = tmp_path / "message"
    path = root / "p" / "s.jsonl"
    path.parent.mkdir(parents=True)
    head = json.dumps(_message_line("t1", 2)) + "\n"
    tail = json.dumps(_message_line("t2", 3))
    path.write_text(head + tail[:20])
    first = _run(MESSAGE, root)
    assert first.coverage is Coverage.PARTIAL and first.more is False
    assert not any(k.startswith("g:") for k in first.next_cursor.state)
    path.write_text(head + tail + "\n")
    second = _run(MESSAGE, root, first.next_cursor)
    assert len(second.events) == 1 and second.coverage is Coverage.COMPLETE


def test_a_same_size_same_mtime_replacement_with_a_new_inode_is_reread(tmp_path: Path) -> None:
    root = tmp_path / "message"
    path = root / "p" / "s.jsonl"
    path.parent.mkdir(parents=True)
    _write(path, [_message_line("aaa", 3)])
    os.utime(path, (1_700_000_000, 1_700_000_000))
    first = _run(MESSAGE, root)
    replacement = path.with_name("s.new")
    _write(replacement, [_message_line("bbb", 3)])
    os.utime(replacement, (1_700_000_000, 1_700_000_000))
    os.replace(replacement, path)
    second = _run(MESSAGE, root, first.next_cursor)
    assert {e.event_key for e in second.events} == {key_text("message", "bbb", "bbb")}


def test_batch_more_is_backlog_only(tmp_path: Path) -> None:
    root = tmp_path / "message"
    _write(root / "p" / "a.jsonl", [_message_line("x1", 1)])
    _write(root / "p" / "b.jsonl", [_message_line("x2", 1)])
    backlog = _run(MESSAGE, root, Cursor(), Budget(max_files=1))
    assert backlog.more is True  # a file was left for the next pass
    done = _run(MESSAGE, root, backlog.next_cursor)
    assert done.more is False

    events = tmp_path / "events-root"
    _write(events / "p" / "e.jsonl", [_message_line("y1", 1), _message_line("y2", 1)])
    refused = _run(MESSAGE, events, Cursor(), Budget(max_events=1))
    assert refused.more is True  # the event budget refused a line: resumable

    legacy = tmp_path / "legacy"
    _rollout_file(
        legacy,
        UUID,
        [
            {
                "type": "event_msg",
                "timestamp": "2026-10-11T09:00:00Z",
                "payload": {
                    "type": "token_count",
                    "info": {"total_token_usage": {"input_tokens": 9}},
                },
            }
        ],
    )
    gap_only = _run(ROLLOUT, legacy)
    assert gap_only.coverage is Coverage.PARTIAL and gap_only.more is False


# Blocking instead of eviction, token availability, no oscillation -------------


def test_a_file_whose_context_cannot_fit_is_blocked_and_keeps_its_offset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import relaydeck.usage.readers._files as files
    from relaydeck.usage.contracts import encoded_cursor_bytes

    root = tmp_path / "rollout"
    _rollout_day(root, "10", 1, "2026-10-10T09:00:00Z")
    first = _run(ROLLOUT, root)
    assert len(first.events) == 1
    _rollout_day(root, "11", 2, "2026-10-11T09:00:00Z")  # a new file needs new context
    _rollout_day(root, "11", 3, "2026-10-11T09:00:00Z")
    monkeypatch.setattr(files, "MAX_CURSOR_BYTES", encoded_cursor_bytes(first.next_cursor) + 40)
    blocked = _run(ROLLOUT, root, first.next_cursor)
    assert blocked.events == () and blocked.coverage is Coverage.PARTIAL
    assert set(blocked.next_cursor.files) == set(first.next_cursor.files)  # no offset advanced
    assert blocked.more is False  # a block is not backlog
    monkeypatch.setattr(files, "MAX_CURSOR_BYTES", 8 * 1024 * 1024)
    freed = _run(ROLLOUT, root, blocked.next_cursor)
    assert len(freed.events) == 2 and freed.coverage is Coverage.COMPLETE


def test_session_turns_with_no_tokens_report_tokens_not_reported(tmp_path: Path) -> None:
    root = tmp_path / "turns"
    zero = {
        "model": "model-credit",
        "input_token_count": 0,
        "output_token_count": 0,
        "cache_read_input_token_count": 0,
        "cache_write_input_token_count": 0,
        "end_timestamp": "2026-10-11T09:00:00Z",
        "metering_usage": [{"value": 1.5, "unit": "credit", "unitPlural": "credits"}],
    }
    _turn_doc(root, "credits-only", [zero, dict(zero)])
    batch = _run(TURNS, root)
    assert batch.events == () and batch.coverage is Coverage.UNAVAILABLE
    _turn_doc(root, "with-tokens", [_turn(0, "t-1", tokens=7)])
    mixed = _run(TURNS, root)
    assert len(mixed.events) == 1 and mixed.coverage is Coverage.UNVERIFIED


def test_a_pass_per_file_budget_finishes_without_oscillating(tmp_path: Path) -> None:
    root = tmp_path / "message"
    for n in range(20):
        _write(root / "p" / f"s{n:02d}.jsonl", [_message_line(f"o{n}", 1)])
    cursor, flags, keys = Cursor(), [], set()
    for _ in range(40):
        batch = _run(MESSAGE, root, cursor, Budget(max_files=1))
        flags.append(batch.more)
        keys |= _keys(batch)
        cursor = batch.next_cursor
        if not batch.more:
            break
    assert len(keys) == 20
    assert flags == [True] * 19 + [False]  # one file per pass, no repeats, no oscillation


# Per-file context lives in FileCursor.context; the cursor state is global only -----


def test_a_file_context_over_its_bound_blocks_that_file_and_keeps_its_offset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import relaydeck.usage.readers._files as files

    root = tmp_path / "rollout"
    _rollout_day(root, "10", 1, "2026-10-10T09:00:00Z")  # a short model: a small context
    first = _run(ROLLOUT, root)
    long_model = "m" * 120
    _day_file(
        root,
        "11",
        2,
        [
            {"type": "turn_context", "payload": {"model": long_model}},
            _usage_record("r2", "2026-10-11T09:00:00Z"),
        ],
    )
    monkeypatch.setattr(files, "MAX_FILE_CONTEXT_BYTES", 100)  # only the long context is too big
    blocked = _run(ROLLOUT, root, first.next_cursor)
    assert blocked.events == () and blocked.coverage is Coverage.PARTIAL
    assert set(blocked.next_cursor.files) == set(first.next_cursor.files)  # the new file is out
    assert blocked.more is False


def test_cursor_state_holds_only_global_keys_and_per_file_context_is_on_the_file(
    tmp_path: Path,
) -> None:
    root = _copy(tmp_path, "rollout")
    batch = _run(ROLLOUT, root)
    assert set(batch.next_cursor.state) <= {"v:", "e:", "o:"}
    assert all(f.context for f in batch.next_cursor.files.values())
    turns = _run(TURNS, _copy(tmp_path / "t", "turns"))
    assert set(turns.next_cursor.state) <= {"v:", "e:", "o:"}
    assert all(f.context for f in turns.next_cursor.files.values())


def test_a_legacy_cursor_with_per_file_state_entries_restarts_from_zero(tmp_path: Path) -> None:
    root = tmp_path / "message"
    _write(root / "p" / "s.jsonl", [_message_line("legacy-1", 4)])
    first = _run(MESSAGE, root)
    legacy = Cursor(
        files=first.next_cursor.files,
        state={"f:old": '{"sid":"x"}', "x:old": "{}"},  # the earlier layout, without "v:"
    )
    replay = _run(MESSAGE, root, legacy)
    assert [e.event_key for e in replay.events] == [e.event_key for e in first.events]
    assert replay.next_cursor.state.get("v:") == "message-4"


# Review must-fixes (denied/oversized files, reservation, migration, ordering) ------

STORE_SOURCE = "h1/kind-a/format-a"
STORE_NOW = int(datetime(2026, 10, 11, tzinfo=UTC).timestamp() * 1000)


def _store(tmp_path: Path):  # type: ignore[no-untyped-def]
    from relaydeck.usage.store import SqliteUsageStore

    return SqliteUsageStore(tmp_path / "usage.db", "UTC", clock=lambda: STORE_NOW)


def test_a_denied_new_file_records_global_gap_without_an_invented_checkpoint(
    tmp_path: Path,
) -> None:
    from relaydeck.usage.contracts import Coverage as _Coverage
    from relaydeck.usage.readers._files import validate_cursor

    root = tmp_path / "message"
    _write(root / "p" / "good-a.jsonl", [_message_line("ga", 1)])
    _write(root / "p" / "good-b.jsonl", [_message_line("gb", 1)])
    denied = root / "p" / "denied.jsonl"
    _write(denied, [_message_line("dn", 1)])
    os.chmod(denied, 0)
    try:
        batch = _run(MESSAGE, root)
    finally:
        os.chmod(denied, 0o600)
    assert len(batch.next_cursor.files) == 2  # only the readable files have checkpoints
    assert "o:" in batch.next_cursor.state  # the gap is recorded globally
    assert batch.coverage is Coverage.PARTIAL and batch.skipped >= 1
    assert validate_cursor(batch.next_cursor) and _coverage_is(_Coverage.PARTIAL, batch)


def _coverage_is(expected: object, batch: Batch) -> bool:
    return batch.coverage is expected


def test_an_oversized_new_file_is_gap_evidence_without_a_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import relaydeck.usage.readers._files as files
    from relaydeck.usage.readers._files import validate_cursor

    root = tmp_path / "message"
    _write(root / "p" / "small.jsonl", [_message_line("sm", 1)])
    _write(root / "p" / "huge.jsonl", [_message_line("hg", 1)] * 3)
    one_record = len(json.dumps(_message_line("sm", 1))) + 1
    monkeypatch.setattr(files, "MAX_FILE_BYTES", one_record + 10)  # the three-record file is over
    batch = _run(MESSAGE, root)
    assert len(batch.next_cursor.files) == 1 and "o:" in batch.next_cursor.state
    assert batch.coverage is Coverage.PARTIAL and validate_cursor(batch.next_cursor)


def test_a_denied_file_with_a_prior_checkpoint_keeps_its_offset_and_context(tmp_path: Path) -> None:
    root = tmp_path / "message"
    path = root / "p" / "kept.jsonl"
    _write(path, [_message_line("kp", 2)])
    first = _run(MESSAGE, root)
    key = next(iter(first.next_cursor.files))
    before = first.next_cursor.files[key]
    with path.open("a") as handle:
        handle.write(json.dumps(_message_line("kq", 3)) + "\n")  # changed, so it is not settled
    os.chmod(path, 0)
    try:
        denied = _run(MESSAGE, root, first.next_cursor)
    finally:
        os.chmod(path, 0o600)
    kept = denied.next_cursor.files[key]
    assert kept.offset == before.offset and kept.size == before.size  # last intact offset
    assert json.loads(kept.context or "{}").get("g") == 1  # the gap is recorded on that file
    assert denied.coverage is Coverage.PARTIAL


def test_the_real_store_accepts_a_pass_in_which_one_file_fails(tmp_path: Path) -> None:
    root = tmp_path / "message"
    for name in ("a", "b"):
        _write(root / "p" / f"{name}.jsonl", [_message_line(f"{name}-1", 1)])
    denied = root / "p" / "c.jsonl"
    _write(denied, [_message_line("c-1", 1)])
    os.chmod(denied, 0)
    try:
        batch = _run(MESSAGE, root)
    finally:
        os.chmod(denied, 0o600)
    with _store(tmp_path) as store:
        store.apply("kind-a", batch, (), source_key=STORE_SOURCE)  # must not raise
        stored = store.cursor(STORE_SOURCE)
    assert set(stored.files) == set(batch.next_cursor.files) and len(stored.files) == 2


def test_the_file_count_cap_is_enforced_by_the_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import relaydeck.usage.readers._files as files
    from relaydeck.usage.readers._files import validate_cursor

    root = tmp_path / "message"
    for n in range(4):
        _write(root / "p" / f"f{n}.jsonl", [_message_line(f"cap-{n}", 1)])
    monkeypatch.setattr(files, "MAX_TRACKED_FILES", 2)
    batch = _run(MESSAGE, root)
    assert len(batch.next_cursor.files) == 2 and batch.coverage is Coverage.PARTIAL
    assert validate_cursor(batch.next_cursor)


def test_the_store_accepts_every_cursor_the_readers_produce_across_sizes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import relaydeck.usage.readers._files as files
    from relaydeck.usage.readers._files import validate_cursor

    cases = [
        (MESSAGE, _copy(tmp_path / "m", "message")),
        (ROLLOUT, _copy(tmp_path / "r", "rollout")),
        (EVENTS, _copy(tmp_path / "e", "events")),
        (TURNS, _copy(tmp_path / "t", "turns")),
    ]
    bounds = [400, 900, 2000, 8 * 1024 * 1024]
    for bound in bounds:
        monkeypatch.setattr(files, "MAX_CURSOR_BYTES", bound)
        for format_id, root in cases:
            batch = _run(format_id, root, Cursor(), Budget(max_files=2))
            assert batch.coverage is not Coverage.UNAVAILABLE  # a valid cursor, never a failed one
            assert validate_cursor(batch.next_cursor)
            with _store(tmp_path / f"s{bound}-{format_id}") as store:
                store.apply(
                    "kind-a",
                    Batch((), batch.next_cursor, batch.coverage, 0),
                    (),
                    source_key=STORE_SOURCE,
                )


def test_an_index_only_turn_keeps_one_key_after_the_schema_migration(tmp_path: Path) -> None:
    root = tmp_path / "turns"
    _turn_doc(root, "migrating", [_turn(0)])  # no ids: keyed by its index
    first = _run(TURNS, root)
    (first_event,) = first.events
    legacy = Cursor(
        files={k: replace(f, context="") for k, f in first.next_cursor.files.items()},
        state={
            "t:" + k: json.dumps(json.loads(f.context)["r"], sort_keys=True, separators=(",", ":"))
            for k, f in first.next_cursor.files.items()
        },
    )
    _turn_doc(root, "migrating", [_turn(0, "later-primary")])  # the same turn gains a primary id
    migrated = _run(TURNS, root, legacy)
    assert {e.event_key for e in migrated.events} == {first_event.event_key}
    assert not any(k.startswith("t:") for k in migrated.next_cursor.state)


def test_a_head_change_seen_before_the_budget_runs_out_is_known_unread_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import relaydeck.usage.readers._files as files

    root = tmp_path / "message"
    path = root / "p" / "h.jsonl"
    _write(path, [_message_line("hd", 1)])
    first = _run(MESSAGE, root)
    original = path.stat()
    with path.open("r+b") as handle:  # in place: same inode and size, the head changes
        handle.write(json.dumps(_message_line("he", 1)).encode() + b"\n")
    os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns))
    clock = [0.0]
    real_settled = files.settled
    calls = []

    def settled_then_late(prev, listed, root_fd):  # type: ignore[no-untyped-def]
        calls.append(1)
        real_settled(prev, listed, root_fd)  # the head check really runs
        clock[0] = 10.0  # the deadline passes during the check
        return False

    monkeypatch.setattr(files.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(files, "settled", settled_then_late)
    batch = _run(MESSAGE, root, first.next_cursor, Budget(deadline_ms=1000))
    assert calls and batch.events == ()
    assert batch.more is True and batch.coverage is Coverage.PARTIAL


def test_a_document_that_grows_after_listing_is_bounded_by_the_opened_size(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import types

    import relaydeck.usage.readers.json_session_turns as turns

    root = _copy(tmp_path, "turns")
    real_open = turns.open_confined

    def grown(root_fd: int, rel: str):  # type: ignore[no-untyped-def]
        fd, st = real_open(root_fd, rel)
        return fd, types.SimpleNamespace(
            st_size=10**6,
            st_mtime_ns=st.st_mtime_ns,
            st_dev=st.st_dev,
            st_ino=st.st_ino,
        )

    monkeypatch.setattr(turns, "open_confined", grown)
    batch = _run(TURNS, root, budget=Budget(max_document_bytes=1000))
    assert batch.events == () and batch.coverage is Coverage.PARTIAL and batch.skipped >= 1


def test_a_legacy_gap_entry_migrates_into_the_document_context(tmp_path: Path) -> None:
    root = tmp_path / "turns"
    _turn_doc(root, "legacy-gap", [_turn(0, "lg-1")])
    first = _run(TURNS, root)
    assert first.coverage is not Coverage.PARTIAL
    legacy = Cursor(
        files={k: replace(f, context="") for k, f in first.next_cursor.files.items()},
        state={"g:" + k: "1" for k in first.next_cursor.files},  # the earlier gap layout
    )
    migrated = _run(TURNS, root, legacy)
    assert migrated.coverage is Coverage.PARTIAL  # the historical gap is kept
    assert _gap_files(migrated.next_cursor) == set(migrated.next_cursor.files)
    assert not any(k.startswith("g:") for k in migrated.next_cursor.state)
