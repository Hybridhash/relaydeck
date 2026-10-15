"""session credit reader. Synthetic session trees built here; shapes mirror
the verified structures, invented values only."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from relaydeck.usage.contracts import Basis, Budget, Counters, Coverage, Cursor
from relaydeck.usage.link_digests import cwd_digest
from relaydeck.usage.readers import READERS
from relaydeck.usage.readers.json_session_credits import FORMAT_ID, READER

T0 = 1_790_000_000_000  # synthetic epoch ms, inside the millisecond band
LINK_KEY = b"uh5-test-link-key-32-bytes-long!!"
FOLDER = "/synthetic/work/alpha"


def _loop(name: str = "agent", rand: int = 7) -> dict[str, Any]:
    return {"agent_id": {"name": name, "parent_id": "p", "rand": 1}, "rand": rand}


def _turn(
    credits: float | None = 1.5,
    *,
    loop: dict[str, Any] | None = None,
    stamp: Any = None,
    mids: list[str] | None = None,
    unit: str = "credit",
    model: str = "model-a",
) -> dict[str, Any]:
    turn: dict[str, Any] = {"model": model}
    if loop is not None:
        turn["loop_id"] = loop
    if stamp is not None:
        turn["end_timestamp"] = stamp
    if mids is not None:
        turn["message_ids"] = mids
    if credits is not None:
        turn["metering_usage"] = [{"unit": unit, "value": credits}]
    return turn


def _write_v2(
    root: Path, name: str, session: str, turns: list[dict[str, Any]], **extra: Any
) -> Path:
    target = root / "cli" / name
    target.parent.mkdir(parents=True, exist_ok=True)
    document = {
        "session_id": session,
        "session_state": {"conversation_metadata": {"user_turn_metadatas": turns}},
        **extra,
    }
    target.write_text(json.dumps(document), encoding="utf-8")
    return target


def _write_v3(
    root: Path,
    workspace: str,
    session: str,
    lines: list[dict[str, Any]],
    folders: list[str] | None = None,
) -> Path:
    home = root / workspace / f"sess_{session}"
    home.mkdir(parents=True, exist_ok=True)
    (home / "messages.jsonl").write_text(
        "".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8"
    )
    (home / "session.json").write_text(
        json.dumps({"modelId": "model-v3", "workspacePaths": folders or [FOLDER]}),
        encoding="utf-8",
    )
    return home / "messages.jsonl"


def _summary(
    usage: float, when: Any, line_id: str | None = "line-1", unit: str = "credit"
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "timestamp": when,
        "payload": {"promptTurnSummaries": [{"unit": unit, "usage": usage}]},
    }
    if line_id is not None:
        record["id"] = line_id
    return record


def _run(root: Path, cursor: Cursor | None = None, budget: Budget | None = None) -> Any:
    return READER.read(root, cursor or Cursor(), budget or Budget())


def _digest(folder: str) -> str | None:
    return cwd_digest(LINK_KEY, folder)


def test_registered_with_credits_only_flags() -> None:
    assert READERS[FORMAT_ID] is READER
    assert READER.tokens_reported is False
    assert READER.credits_reported is True


def test_loop_turns_emit_metered_events(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    _write_v2(
        root,
        "s1.json",
        "s1",
        [
            _turn(1.5, loop=_loop("a", 1), stamp=T0, mids=["m1"]),
            _turn(2.0, loop=_loop("a", 2), stamp=T0 + 1000, mids=["m2"]),
        ],
    )
    batch = _run(root)
    assert batch.coverage == Coverage.COMPLETE and batch.skipped == 0
    assert [e.session_key for e in batch.events] == ["s1", "s1"]
    assert [e.credits_milli for e in batch.events] == [1500, 2000]
    assert {e.meter for e in batch.events} == {"credits"}
    assert {e.counters for e in batch.events} == {Counters()}
    assert {e.basis for e in batch.events} == {Basis.PER_TURN}
    assert [e.occurred_ms for e in batch.events] == [T0, T0 + 1000]
    assert len({e.event_key for e in batch.events}) == 2


def test_same_loop_pair_stays_distinct(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    loop = _loop("a", 9)
    _write_v2(
        root,
        "s1.json",
        "s1",
        [
            _turn(1.0, loop=loop, stamp=T0, mids=["m1"]),
            _turn(3.0, loop=loop, stamp=T0 + 5000, mids=["m2"]),
        ],
    )
    batch = _run(root)
    assert batch.coverage == Coverage.COMPLETE
    assert [e.credits_milli for e in batch.events] == [1000, 3000]
    assert len({e.event_key for e in batch.events}) == 2


def test_retry_from_same_cursor_is_identical(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    _write_v2(root, "s1.json", "s1", [_turn(1.0, loop=_loop(), stamp=T0, mids=["m1"])])
    cursor = Cursor()
    first, second = _run(root, cursor), _run(root, cursor)
    assert [e.event_key for e in first.events] == [e.event_key for e in second.events]


def test_quiet_second_pass_emits_nothing(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    _write_v2(root, "s1.json", "s1", [_turn(1.0, loop=_loop(), stamp=T0, mids=["m1"])])
    first = _run(root)
    second = _run(root, first.next_cursor)
    assert second.events == () and second.coverage == Coverage.COMPLETE


def test_rewritten_document_reemits_with_stable_keys(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    turns = [_turn(1.0, loop=_loop("a", 1), stamp=T0, mids=["m1"])]
    _write_v2(root, "s1.json", "s1", turns)
    first = _run(root)
    turns.append(_turn(4.0, loop=_loop("a", 2), stamp=T0 + 9000, mids=["m2"]))
    _write_v2(root, "s1.json", "s1", turns)
    second = _run(root, first.next_cursor)
    # A rewrite re-reads the whole document; the old turn keeps its key, so the
    # store replaces it (no double count) and only the new turn adds credit.
    assert [e.credits_milli for e in second.events] == [1000, 4000]
    assert second.events[0].event_key == first.events[0].event_key


def test_positional_fallback_emits_without_loop_id(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    _write_v2(
        root,
        "s1.json",
        "s1",
        [
            _turn(1.0, stamp=T0, mids=["m1"]),  # missing
            _turn(2.0, stamp=T0 + 1000, mids=["m2"]) | {"loop_id": "nope"},  # invalid
            _turn(3.0, stamp=T0 + 2000, mids=["m3"]) | {"loop_id": {}},  # invalid
        ],
    )
    batch = _run(root)
    assert batch.coverage == Coverage.COMPLETE
    assert [e.credits_milli for e in batch.events] == [1000, 2000, 3000]


def test_positional_key_moves_on_rewrite_while_loop_keys_hold(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    looped = _turn(1.0, loop=_loop(), stamp=T0, mids=["m1"])
    positional = _turn(2.0, stamp=T0 + 1000, mids=["m2"])
    _write_v2(root, "s1.json", "s1", [looped, positional])
    before = {e.credits_milli: e.event_key for e in _run(root).events}
    _write_v2(root, "s1.json", "s1", [_turn(0.0, stamp=T0 - 1000), looped, positional])
    after = {e.credits_milli: e.event_key for e in _run(root).events}
    assert after[1000] == before[1000]  # the loop key is stable
    assert after[2000] != before[2000]  # the positional key moved: documented limit


def test_fork_copy_counts_once_kept_by_the_earliest_session(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    loop = _loop("shared", 3)
    old = _write_v2(root, "original.json", "s-old", [_turn(2.5, loop=loop, stamp=T0, mids=["m1"])])
    new = _write_v2(
        root, "copy.json", "s-new", [_turn(2.5, loop=loop, stamp=T0 + 7000, mids=["m9"])]
    )
    os.utime(old, ns=(T0 * 1_000_000, T0 * 1_000_000))
    os.utime(new, ns=(T0 * 1_000_000 + 5_000, T0 * 1_000_000 + 5_000))
    batch = _run(root)
    assert batch.coverage == Coverage.COMPLETE
    (event,) = batch.events
    assert (event.session_key, event.credits_milli) == ("s-old", 2500)
    assert batch.skipped == 1  # the copy's identical turn, counted


def test_fork_tie_breaks_by_session_then_path(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    loop = _loop("shared", 4)
    first = _write_v2(root, "b.json", "s-b", [_turn(1.0, loop=loop, stamp=T0)])
    second = _write_v2(root, "a.json", "s-a", [_turn(1.0, loop=loop, stamp=T0)])
    same = T0 * 1_000_000
    os.utime(first, ns=(same, same))
    os.utime(second, ns=(same, same))
    (event,) = _run(root).events
    assert event.session_key == "s-a"  # same mtime: the lower session wins


def test_fork_with_different_totals_counts_both(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    loop = _loop("shared", 5)
    old = _write_v2(root, "o.json", "s-o", [_turn(1.0, loop=loop, stamp=T0)])
    new = _write_v2(root, "n.json", "s-n", [_turn(9.0, loop=loop, stamp=T0)])
    os.utime(old, ns=(T0 * 1_000_000, T0 * 1_000_000))
    os.utime(new, ns=(T0 * 1_000_000 + 5_000, T0 * 1_000_000 + 5_000))
    batch = _run(root)
    assert sorted(e.session_key for e in batch.events) == ["s-n", "s-o"]


def test_late_fork_copy_is_vetoed_by_the_standing_claim(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    loop = _loop("shared", 6)
    old = _write_v2(root, "o.json", "s-o", [_turn(1.0, loop=loop, stamp=T0, mids=["m1"])])
    os.utime(old, ns=(T0 * 1_000_000, T0 * 1_000_000))
    first = _run(root)
    assert [e.session_key for e in first.events] == ["s-o"]
    new = _write_v2(root, "n.json", "s-n", [_turn(1.0, loop=loop, stamp=T0, mids=["m1"])])
    os.utime(new, ns=(T0 * 1_000_000 + 5_000, T0 * 1_000_000 + 5_000))
    second = _run(root, first.next_cursor)
    assert second.events == ()  # the copy's identical turn is vetoed, never emitted


def test_v3_summary_emits_and_cuts_resumed_v2_turns(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    _write_v2(
        root,
        "s1.json",
        "s1",
        [
            _turn(1.0, loop=_loop("a", 1), stamp=T0 - 1000, mids=["m1"]),
            _turn(2.0, loop=_loop("a", 2), stamp=T0, mids=["m2"]),
            _turn(3.0, loop=_loop("a", 3), stamp=T0 + 1000, mids=["m3"]),
        ],
    )
    _write_v3(root, "ws", "s1", [_summary(5.0, T0)])
    batch = _run(root)
    assert batch.coverage == Coverage.COMPLETE
    kept = sorted(e.credits_milli for e in batch.events if e.session_key == "s1")
    assert kept == [1000, 5000]  # the pre-cutoff turn plus the v3 summary itself
    assert batch.skipped == 2  # the two resumed turns, counted


def test_v3_offset_key_is_stable_without_a_line_id(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    _write_v3(root, "ws", "s1", [{"n": 1}, _summary(2.0, T0, line_id=None)])
    cursor = Cursor()
    first, second = _run(root, cursor), _run(root, cursor)
    assert [e.event_key for e in first.events] == [e.event_key for e in second.events]
    assert [e.credits_milli for e in first.events] == [2000]


def test_v3_digest_comes_from_the_first_workspace_path(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    _write_v3(root, "ws", "s1", [_summary(2.0, T0)], folders=[FOLDER, "/synthetic/other"])
    batch = READER.read(root, Cursor(), Budget(digest=_digest))
    (event,) = batch.events
    assert event.cwd_digest == cwd_digest(LINK_KEY, FOLDER)
    assert event.model == "model-v3"


def test_iso_timestamps_parse_on_both_layouts(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    stamp = "2026-10-11T10:00:00.123456Z"
    _write_v2(root, "s1.json", "s1", [_turn(1.0, loop=_loop(), stamp=stamp, mids=["m1"])])
    _write_v3(root, "ws", "s9", [_summary(2.0, stamp)])
    batch = _run(root)
    assert batch.coverage == Coverage.COMPLETE
    expected = int(datetime(2026, 10, 11, 10, 0, 0, 123456, tzinfo=UTC).timestamp() * 1000)
    assert {e.occurred_ms for e in batch.events} == {expected}


def test_foreign_units_are_skipped_counted_and_partial(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    _write_v2(
        root,
        "s1.json",
        "s1",
        [
            {
                "model": "model-a",
                "loop_id": _loop("a", 1),
                "end_timestamp": T0,
                "message_ids": ["m1"],
                "metering_usage": [
                    {"unit": "credit", "value": 1.0},
                    {"unit": "tokens", "value": 5000},
                ],
            },
            _turn(1.0, loop=_loop("a", 2), stamp=T0 + 1000, unit="tokens"),
        ],
    )
    batch = _run(root)
    assert batch.coverage == Coverage.PARTIAL  # foreign units were seen
    assert [e.credits_milli for e in batch.events] == [1000]  # credit part only
    assert batch.skipped == 2


def test_v3_foreign_summary_is_skipped_and_partial(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    _write_v3(root, "ws", "s1", [_summary(4.0, T0, unit="tokens")])
    batch = _run(root)
    assert batch.coverage == Coverage.PARTIAL
    assert batch.events == () and batch.skipped == 1


def test_meterless_turns_are_quiet_and_report_unavailable(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    _write_v2(root, "s1.json", "s1", [{"model": "model-a"}])
    batch = _run(root)
    assert batch.events == () and batch.skipped == 0
    assert batch.coverage == Coverage.UNAVAILABLE  # turns exist, credits not reported


def test_poisoned_credit_value_is_skipped_and_partial(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    _write_v2(root, "s1.json", "s1", [_turn(-1.0, loop=_loop(), stamp=T0)])
    batch = _run(root)
    assert batch.coverage == Coverage.PARTIAL
    assert batch.events == () and batch.skipped == 1


def test_missing_root_is_unavailable(tmp_path: Path) -> None:
    batch = _run(tmp_path / "no-such-dir")
    assert batch.coverage == Coverage.UNAVAILABLE and batch.events == ()


def test_truncated_claim_scan_stays_partial_but_still_reads(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    _write_v2(root, "s1.json", "s1", [_turn(1.0, loop=_loop(), stamp=T0, mids=["m1"])])
    batch = _run(root, budget=Budget(max_bytes=10))
    assert [e.credits_milli for e in batch.events] == [1000]
    assert batch.coverage == Coverage.PARTIAL  # the scan did not cover every file
