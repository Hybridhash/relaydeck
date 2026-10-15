"""Every gap flag carries its reasons; an old flag is `unknown`;
Cumulative-only files are `legacy` only at verified end of file; malformed records are `skip`
independently; `retry_blocked` marks only a current obstruction. Synthetic fixtures in temp dirs.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from relaydeck.usage.contracts import (
    MAX_CURSOR_BYTES,
    MAX_LINE_BYTES,
    Batch,
    Budget,
    Coverage,
    Cursor,
    FileCursor,
    encoded_cursor_bytes,
)
from relaydeck.usage.readers import READERS
from relaydeck.usage.readers._files import REASONS, file_key, reasons_of

MESSAGE = "jsonl-message-usage-v1"
ROLLOUT = "jsonl-cumulative-token-count-v1"
UUID = "0a1b2c3d-1111-4000-8000-000000000001"


def _run(
    format_id: str, root: Path, cursor: Cursor | None = None, budget: Budget | None = None
) -> Batch:
    return READERS[format_id].read(root, cursor or Cursor(), budget or Budget())


def _message_path(root: Path, name: str = "s.jsonl") -> Path:
    path = root / "p" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _rollout_path(root: Path) -> Path:
    path = root / "sessions" / "2026" / "10" / "11" / f"rollout-x-{UUID}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


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


# -- one fixture per reason ------------------------------------------------------------------------


def test_skip_reason_for_a_malformed_record(tmp_path: Path) -> None:
    root = tmp_path / "message"
    path = _message_path(root)
    path.write_text(json.dumps(_message_line("ok-1")) + "\n{bad\n")
    batch = _run(MESSAGE, root)
    assert batch.coverage is Coverage.PARTIAL
    assert _context(batch.next_cursor, "p/s.jsonl")["k"] == ["skip"]
    assert _context(batch.next_cursor, "p/s.jsonl")["g"] == 1


def test_oversize_reason_for_a_line_over_the_bound(tmp_path: Path) -> None:
    root = tmp_path / "message"
    path = _message_path(root)
    path.write_bytes(
        b"x" * (MAX_LINE_BYTES + 16) + b"\n" + json.dumps(_message_line("ok-2")).encode() + b"\n"
    )
    batch = _run(MESSAGE, root)
    assert _context(batch.next_cursor, "p/s.jsonl")["k"] == ["oversize"]
    assert batch.retry_blocked is True  # the oversize line was hit in this pass


def test_unreadable_reason_and_no_invented_cursor_for_a_never_opened_file(tmp_path: Path) -> None:
    root = tmp_path / "message"
    path = _message_path(root)
    path.write_text(json.dumps(_message_line("ok-3")) + "\n")
    os.chmod(path, 0)
    try:
        batch = _run(MESSAGE, root)
    finally:
        os.chmod(path, 0o600)
    assert batch.retry_blocked is True
    assert file_key("p/s.jsonl") not in batch.next_cursor.files  # never opened: no FileCursor
    assert (
        batch.next_cursor.state.get("o:") == "1"
    )  # the uncertainty is kept in the overflow marker


def test_legacy_reason_for_a_cumulative_only_file(tmp_path: Path) -> None:
    root = tmp_path / "rollout"
    path = _rollout_path(root)
    path.write_text(json.dumps(_token_count(9)) + "\n")
    batch = _run(ROLLOUT, root)
    assert batch.coverage is Coverage.PARTIAL
    rel = f"sessions/2026/10/11/rollout-x-{UUID}.jsonl"
    assert _context(batch.next_cursor, rel)["k"] == ["legacy"]


def test_unknown_reason_for_an_old_gap_flag_is_kept_not_erased(tmp_path: Path) -> None:
    root = tmp_path / "message"
    path = _message_path(root)
    path.write_text(json.dumps(_message_line("ok-4")) + "\n{bad\n")
    first = _run(MESSAGE, root)
    key = file_key("p/s.jsonl")
    old = first.next_cursor.files[key]
    legacy_context = json.dumps(
        {"g": 1}, separators=(",", ":")
    )  # a flag written before reasons existed
    cursor = Cursor(
        {
            key: FileCursor(
                old.dev,
                old.ino,
                old.offset,
                old.size,
                old.mtime_ns,
                old.head_digest,
                legacy_context,
            )
        },
        first.next_cursor.state,
        first.next_cursor.revision,
    )
    assert reasons_of(cursor.files[key]) == {"unknown"}
    with path.open("a") as handle:
        handle.write(json.dumps(_message_line("ok-5")) + "\n")
    second = _run(MESSAGE, root, cursor)
    assert second.coverage is Coverage.PARTIAL  # the historical gap still holds
    context = _context(second.next_cursor, "p/s.jsonl")
    assert context["g"] == 1 and context["k"] == ["unknown"]  # the old flag is labelled, not erased


def test_several_reasons_coexist_in_one_file(tmp_path: Path) -> None:
    root = tmp_path / "message"
    path = _message_path(root)
    path.write_bytes(
        b"{bad\n"
        + b"y" * (MAX_LINE_BYTES + 8)
        + b"\n"
        + json.dumps(_message_line("ok-6")).encode()
        + b"\n"
    )
    batch = _run(MESSAGE, root)
    assert _context(batch.next_cursor, "p/s.jsonl")["k"] == ["oversize", "skip"]


def test_reasons_are_bounded_and_the_cursor_stays_bounded_under_many_files(tmp_path: Path) -> None:
    root = tmp_path / "message"
    for index in range(300):
        _message_path(root, f"s{index:03d}.jsonl").write_text("{bad\n")
    batch = _run(MESSAGE, root, budget=Budget(max_files=10_000))
    assert encoded_cursor_bytes(batch.next_cursor) <= MAX_CURSOR_BYTES
    for file in batch.next_cursor.files.values():
        if file.context:
            assert set(json.loads(file.context).get("k", [])) <= REASONS
    assert batch.coverage is Coverage.PARTIAL


# -- legacy: classified only at verified end of file -------------------------


def test_cumulative_only_file_read_in_two_chunks_is_legacy_only_at_end_of_file(
    tmp_path: Path,
) -> None:
    root = tmp_path / "rollout"
    path = _rollout_path(root)
    path.write_text("".join(json.dumps(_token_count(n)) + "\n" for n in range(1, 9)))
    rel = f"sessions/2026/10/11/rollout-x-{UUID}.jsonl"
    first = _run(ROLLOUT, root, budget=Budget(max_bytes=300))
    assert first.coverage is Coverage.PARTIAL and first.more is True
    assert "g" not in _context(first.next_cursor, rel)  # not at EOF yet: no verdict
    assert _context(first.next_cursor, rel)["r"]["l"] == 1  # but the evidence is persisted
    second = _run(ROLLOUT, root, first.next_cursor)
    assert _context(second.next_cursor, rel)["g"] == 1
    assert _context(second.next_cursor, rel)["k"] == ["legacy"]


def test_a_mixed_file_is_never_legacy(tmp_path: Path) -> None:
    root = tmp_path / "rollout"
    path = _rollout_path(root)
    lines = [_token_count(n) for n in range(1, 6)] + [_usage_record("resp-1")]
    path.write_text("".join(json.dumps(line) + "\n" for line in lines))
    rel = f"sessions/2026/10/11/rollout-x-{UUID}.jsonl"
    first = _run(ROLLOUT, root, budget=Budget(max_bytes=200))
    second = _run(ROLLOUT, root, first.next_cursor)
    assert len(second.events) == 1  # the per-call record is counted
    assert "g" not in _context(second.next_cursor, rel)  # so the file is not legacy


def test_a_malformed_line_in_a_legacy_file_is_skip_as_well_as_legacy(tmp_path: Path) -> None:
    root = tmp_path / "rollout"
    path = _rollout_path(root)
    path.write_text(json.dumps(_token_count(4)) + "\n{bad\n")
    rel = f"sessions/2026/10/11/rollout-x-{UUID}.jsonl"
    batch = _run(ROLLOUT, root)
    assert _context(batch.next_cursor, rel)["k"] == ["legacy", "skip"]


# -- retry_blocked: a current obstruction only ---------------------------------


def test_historical_gap_alone_does_not_set_retry_blocked(tmp_path: Path) -> None:
    root = tmp_path / "message"
    path = _message_path(root)
    path.write_text("{bad\n" + json.dumps(_message_line("ok-7")) + "\n")
    first = _run(MESSAGE, root)
    assert first.coverage is Coverage.PARTIAL and first.retry_blocked is False  # a new skip only
    second = _run(MESSAGE, root, first.next_cursor)
    assert second.coverage is Coverage.PARTIAL and second.retry_blocked is False  # historical only


def test_oversize_in_one_pass_is_a_current_obstruction_and_then_not(tmp_path: Path) -> None:
    root = tmp_path / "message"
    path = _message_path(root)
    path.write_bytes(b"z" * (MAX_LINE_BYTES + 4) + b"\n")
    first = _run(MESSAGE, root)
    assert first.retry_blocked is True
    second = _run(MESSAGE, root, first.next_cursor)
    assert second.retry_blocked is False and second.coverage is Coverage.PARTIAL
