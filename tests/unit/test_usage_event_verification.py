"""event-usage verification against the documented log contract, with
same-root session-index enrichment. Synthetic logs and index built here."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from relaydeck.usage.contracts import Batch, Budget, Coverage, Cursor, UsageQuery
from relaydeck.usage.readers._files import key_text, ms_of
from relaydeck.usage.readers.jsonl_event_usage import READER
from relaydeck.usage.store import SqliteUsageStore

_MISSING = object()
TS = int(ms_of("2026-10-11T11:00:00Z") or 0) * 1000
FOLDER = "/synthetic/work/alpha"


def _record_line(
    usage_id: str,
    session: str = "sess-a",
    *,
    kind: Any = "goal_usage_attribution",
    rtype: Any = "event",
    reported: Any = True,
    unit: str = "tokens",
    counts: tuple[int, int, int, int] = (80, 20, 30, 5),
    stamp: Any = None,
) -> dict[str, Any]:
    inputs, output, cached, reasoning = counts
    event: dict[str, Any] = {
        "record": {
            "owner": {"session_id": session},
            "quantity": {
                "input_tokens": inputs,
                "output_tokens": output,
                "cached_tokens": cached,
                "reasoning_tokens": reasoning,
                "unit": unit,
                "reported": reported,
            },
            "usage_id": usage_id,
        }
    }
    if kind is not _MISSING:
        event["kind"] = kind
    line: dict[str, Any] = {
        "payload": {"event": event},
        "recorded_at": TS if stamp is None else stamp,
    }
    if rtype is not _MISSING:
        line["record_type"] = rtype
    return line


def _review_line(
    *,
    counts: tuple[int, int, int, int, int] = (100, 10, 70, 30, 2),
    model: Any = None,
    stamp: Any = None,
) -> dict[str, Any]:
    inputs, output, cached, non_cached, reasoning = counts
    event: dict[str, Any] = {
        "kind": "automated_review_completed",
        "usage": {
            "input_tokens": inputs,
            "output_tokens": output,
            "cached_input_tokens": cached,
            "non_cached_input_tokens": non_cached,
            "reasoning_tokens": reasoning,
        },
    }
    if model is None:
        model = {"model_id": "model-r"}
    if model is not _MISSING:
        event["model"] = model
    return {
        "payload": {"event": event},
        "record_type": "event",
        "recorded_at": TS if stamp is None else stamp,
    }


def _usage_line(kind: str, usage: dict[str, Any]) -> dict[str, Any]:
    return {
        "payload": {"event": {"kind": kind, "usage": usage}},
        "record_type": "event",
        "recorded_at": TS,
    }


def _write_log(root: Path, sid: str, lines: list[dict[str, Any]]) -> Path:
    target = root / "sessions" / "2026" / "10" / "11" / sid / "session.jsonl"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
    return target


def _make_index(root: Path, rows: list[tuple[Any, Any, Any]]) -> Path:
    path = root / "session-index.db"
    db = sqlite3.connect(path)
    db.execute(
        "CREATE TABLE sessions(session_id TEXT PRIMARY KEY, model_id TEXT,"
        " workspace_root TEXT, title TEXT NOT NULL DEFAULT '')"
    )
    db.executemany("INSERT INTO sessions(session_id, model_id, workspace_root) VALUES(?,?,?)", rows)
    db.commit()
    db.close()
    return path


def _run(root: Path, cursor: Cursor | None = None, digest: Any = None) -> Any:
    return READER.read(root, cursor or Cursor(), Budget(digest=digest))


def _digest_recorder(seen: list[str]) -> Any:
    return lambda folder: seen.append(folder) or "dg"


def test_index_hit_marks_verified_with_model_and_digest(tmp_path: Path) -> None:
    root = tmp_path / "data"
    _write_log(root, "sess-a", [_record_line("u-1")])
    _make_index(root, [("sess-a", "model-a", FOLDER)])
    seen: list[str] = []
    batch = _run(root, digest=_digest_recorder(seen))
    (event,) = batch.events
    assert event.model == "model-a" and event.accounting_verified is True
    assert event.cwd_digest == "dg" and seen == [FOLDER]
    assert event.event_key == key_text("usage-id", "u-1")
    assert (event.counters.input_uncached, event.counters.cache_read) == (50, 30)
    assert batch.coverage == Coverage.COMPLETE


def test_unmatched_owner_stays_stored_but_unverified(tmp_path: Path) -> None:
    root = tmp_path / "data"
    _write_log(root, "sess-a", [_record_line("u-1", session="ghost")])
    _make_index(root, [("sess-a", "model-a", FOLDER)])
    batch = _run(root)
    (event,) = batch.events  # stored, exactly as before
    assert event.model is None and event.accounting_verified is False
    assert event.cwd_digest is None
    assert batch.coverage == Coverage.UNVERIFIED


def test_missing_index_keeps_status_quo_unverified(tmp_path: Path) -> None:
    root = tmp_path / "data"
    _write_log(root, "sess-a", [_record_line("u-1")])
    batch = _run(root)
    (event,) = batch.events
    assert event.model is None and event.accounting_verified is False
    assert batch.coverage == Coverage.UNVERIFIED


def test_corrupt_index_is_no_enrichment_never_an_error(tmp_path: Path) -> None:
    root = tmp_path / "data"
    _write_log(root, "sess-a", [_record_line("u-1")])
    (root / "session-index.db").write_bytes(b"not a database")
    batch = _run(root)
    (event,) = batch.events
    assert event.accounting_verified is False


def test_locked_index_is_no_enrichment_never_an_error(tmp_path: Path) -> None:
    root = tmp_path / "data"
    _write_log(root, "sess-a", [_record_line("u-1")])
    path = _make_index(root, [("sess-a", "model-a", FOLDER)])
    held = sqlite3.connect(path)
    held.execute("BEGIN EXCLUSIVE")
    try:
        batch = _run(root)
    finally:
        held.execute("ROLLBACK")
        held.close()
    (event,) = batch.events
    assert event.accounting_verified is False


def test_wrong_schema_index_is_no_enrichment(tmp_path: Path) -> None:
    root = tmp_path / "data"
    _write_log(root, "sess-a", [_record_line("u-1")])
    db = sqlite3.connect(root / "session-index.db")
    db.execute("CREATE TABLE other(id TEXT)")
    db.commit()
    db.close()
    batch = _run(root)
    (event,) = batch.events
    assert event.accounting_verified is False


def test_mixed_verified_batch_stays_unverified(tmp_path: Path) -> None:
    root = tmp_path / "data"
    _write_log(
        root,
        "sess-a",
        [_record_line("u-1", session="sess-a"), _record_line("u-2", session="ghost")],
    )
    _make_index(root, [("sess-a", "model-a", FOLDER)])
    batch = _run(root)
    got = {e.event_key: e.accounting_verified for e in batch.events}
    assert got == {key_text("usage-id", "u-1"): True, key_text("usage-id", "u-2"): False}
    assert batch.coverage == Coverage.UNVERIFIED


def test_before_after_verified_counts_on_synthetic_data(tmp_path: Path) -> None:
    root = tmp_path / "data"
    _write_log(root, "sess-a", [_record_line("u-1"), _record_line("u-2")])
    before = _run(root)
    assert sum(e.accounting_verified for e in before.events) == 0
    _make_index(root, [("sess-a", "model-a", FOLDER)])
    after = _run(root)
    assert sum(e.accounting_verified for e in after.events) == 2


def test_old_schema_cursor_restarts_with_stable_keys(tmp_path: Path) -> None:
    root = tmp_path / "data"
    _write_log(root, "sess-a", [_record_line("v-1")])
    _make_index(root, [("sess-a", "model-a", FOLDER)])
    first = _run(root)
    old = Cursor(files=first.next_cursor.files, state={"v:": "events-3"})
    replay = _run(root, old)
    assert [e.event_key for e in replay.events] == [e.event_key for e in first.events]
    assert replay.next_cursor.state.get("v:") == "events-4"
    assert all(e.accounting_verified for e in replay.events)


def test_record_on_another_kind_is_drift_gap(tmp_path: Path) -> None:
    root = tmp_path / "data"
    _write_log(root, "sess-a", [_record_line("u-1", kind="model_completed")])
    batch = _run(root)
    assert batch.events == () and batch.skipped == 1
    assert batch.coverage == Coverage.PARTIAL


def test_record_under_another_record_type_is_drift_gap(tmp_path: Path) -> None:
    root = tmp_path / "data"
    _write_log(root, "sess-a", [_record_line("u-1", rtype="reconciliation")])
    batch = _run(root)
    assert batch.events == () and batch.skipped == 1
    assert batch.coverage == Coverage.PARTIAL


def test_overlong_model_stays_unverified(tmp_path: Path) -> None:
    root = tmp_path / "data"
    _write_log(root, "sess-a", [_record_line("u-1")])
    _make_index(root, [("sess-a", "m" * 129, FOLDER)])
    (event,) = _run(root).events
    assert event.model is None and event.accounting_verified is False


def test_review_counted_with_mapping_model_digest_and_stable_key(tmp_path: Path) -> None:
    root = tmp_path / "data"
    _write_log(root, "sess-a", [_review_line()])
    _make_index(root, [("sess-a", "model-a", FOLDER)])
    seen: list[str] = []
    cursor = Cursor()
    first, second = _run(root, cursor, _digest_recorder(seen)), _run(root, cursor)
    (event,) = first.events
    assert event.session_key == "sess-a"  # the directory session: reviews name no owner
    assert (event.counters.input_uncached, event.counters.cache_read) == (30, 70)
    assert (event.counters.output, event.counters.reasoning) == (10, 2)
    assert event.model == "model-r" and event.accounting_verified is True
    assert event.cwd_digest == "dg" and seen == [FOLDER]
    assert [e.event_key for e in second.events] == [event.event_key]


def test_review_model_falls_back_to_dir_index(tmp_path: Path) -> None:
    root = tmp_path / "data"
    _write_log(root, "sess-a", [_review_line(model={"other": 1})])
    _make_index(root, [("sess-a", "model-a", FOLDER)])
    (event,) = _run(root).events
    assert event.model == "model-a" and event.accounting_verified is True


def test_review_without_any_model_stays_unverified(tmp_path: Path) -> None:
    root = tmp_path / "data"
    _write_log(root, "sess-a", [_review_line(model=_MISSING)])
    (event,) = _run(root).events
    assert event.model is None and event.accounting_verified is False


def test_review_append_is_stable_and_second_pass_quiet(tmp_path: Path) -> None:
    root = tmp_path / "data"
    path = _write_log(root, "sess-a", [_review_line()])
    _make_index(root, [("sess-a", "model-a", FOLDER)])
    first = _run(root)
    assert len(first.events) == 1  # the review is counted
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(_record_line("u-9")) + "\n")
    second = _run(root, first.next_cursor)
    assert [e.event_key for e in second.events] == [key_text("usage-id", "u-9")]
    third = _run(root, second.next_cursor)
    assert third.events == ()


def test_review_rewrite_rekeys_and_counts_new_content_once(tmp_path: Path) -> None:
    root = tmp_path / "data"
    path = _write_log(root, "sess-a", [_review_line(counts=(100, 10, 70, 30, 2))])
    _make_index(root, [("sess-a", "model-a", FOLDER)])
    before = {e.event_key for e in _run(root).events}
    assert len(before) == 1
    path.write_text(json.dumps(_review_line(counts=(50, 5, 20, 30, 1))) + "\n", encoding="utf-8")
    after = _run(root, Cursor())
    assert len(after.events) == 1  # the new generation, counted exactly once
    assert {e.event_key for e in after.events} != before  # re-keyed, never merged
    assert after.events[0].counters.input_uncached == 30


def test_review_truncation_with_intact_head_replaces_without_double(tmp_path: Path) -> None:
    root = tmp_path / "data"
    line = json.dumps(_review_line(counts=(100, 10, 70, 30, 2))) + "\n"
    path = _write_log(root, "sess-a", [])
    path.write_text(line * 40)  # head-identical prefix over 4 KiB
    _make_index(root, [("sess-a", "model-a", FOLDER)])
    first = _run(root)
    assert len(first.events) == 40
    first_keys = {e.event_key for e in first.events}
    path.write_text(line * 20)  # truncation: same inode, intact head
    second = _run(root, first.next_cursor)
    # Batch order is not offset order; compare as sets. Same keys: replace.
    assert {e.event_key for e in second.events} < first_keys
    assert len(second.events) == 20
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line * 20)  # regrow the identical tail
    third = _run(root, second.next_cursor)
    assert {e.event_key for e in third.events} == first_keys - {e.event_key for e in second.events}
    store = SqliteUsageStore(tmp_path / "state" / "usage.db", "UTC")
    for batch in (first, second, third):
        cursor = store.cursor("h1/kind-a/fmt")
        store.apply(
            "kind-a",
            Batch(batch.events, cursor, batch.coverage, cursor.revision, ()),
            (),
            source_key="h1/kind-a/fmt",
        )
    (row,) = store.query(UsageQuery("h1", "2026-10-11", "2026-10-11"), TS // 1000).rows
    assert row.counters.cache_read == 40 * 70  # replaced, never doubled


def test_ignored_kinds_stay_quiet(tmp_path: Path) -> None:
    root = tmp_path / "data"
    _write_log(
        root,
        "sess-a",
        [
            _usage_line("model_completed", {"input_tokens": 5, "output_tokens": 5}),
            _usage_line("resource_usage_sampled", {"rss_self_bytes": 9}),
            _usage_line("context_projection_checkpoint", {"input_tokens": 5, "output_tokens": 5}),
            {"payload": {"event": {"kind": "started"}}, "recorded_at": TS},
        ],
    )
    batch = _run(root)
    assert batch.events == () and batch.skipped == 0
    assert batch.coverage == Coverage.COMPLETE


def test_review_usage_without_counters_emits_a_zero_event(tmp_path: Path) -> None:
    root = tmp_path / "data"
    _write_log(root, "sess-a", [_usage_line("automated_review_completed", {"rss_self_bytes": 1})])
    batch = _run(root)
    # Mirrors the record path: missing counters default to zero, only malformed
    # values are gaps.
    (event,) = batch.events
    assert event.counters.total == 0 and batch.skipped == 0


def test_review_with_bad_timestamp_is_gap(tmp_path: Path) -> None:
    root = tmp_path / "data"
    _write_log(root, "sess-a", [_review_line(stamp="not-a-time")])
    batch = _run(root)
    assert batch.events == () and batch.skipped == 1


def test_index_caps_reject_an_absurd_index(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import relaydeck.usage.readers._session_index as index_mod

    root = tmp_path / "data"
    _write_log(root, "sess-a", [_record_line("u-1")])
    _make_index(root, [("sess-a", "model-a", FOLDER)])
    monkeypatch.setattr(index_mod, "MAX_INDEX_ROWS", 0)
    batch = _run(root)
    (event,) = batch.events
    assert event.accounting_verified is False  # over the cap: no enrichment, no error
