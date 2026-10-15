"""Rollout and message formats digest the session working
folder with the budget callable and pin it per file. Turns and event formats
emit no digests."""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

from relaydeck.usage.contracts import Batch, Budget, Cursor, FileCursor
from relaydeck.usage.link_digests import cwd_digest, load_or_create_link_key
from relaydeck.usage.readers._files import context_of, file_key
from relaydeck.usage.readers.json_session_turns import READER as TURNS
from relaydeck.usage.readers.jsonl_cumulative_rollout import READER as ROLLOUT
from relaydeck.usage.readers.jsonl_event_usage import READER as EVENTS
from relaydeck.usage.readers.jsonl_message_usage import READER as MESSAGES

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "usage"
FOLDER = "/synthetic/work/alpha"


def _copy(tmp_path: Path, name: str) -> Path:
    target = tmp_path / name
    shutil.copytree(FIXTURES / "session_workdirs" / name, target)
    return target


def _run(reader, root: Path, cursor: Cursor | None = None, digest=None) -> Batch:
    return reader.read(root, cursor or Cursor(), Budget(digest=digest))


def _digest_fn(tmp_path: Path):
    (tmp_path / "state").mkdir(exist_ok=True)
    key = load_or_create_link_key(tmp_path / "state")
    assert key is not None
    return lambda folder: cwd_digest(key, folder)


def test_rollout_pins_session_meta_cwd_digest(tmp_path: Path) -> None:
    root = _copy(tmp_path, "rollout")
    expected = _digest_fn(tmp_path)(FOLDER)
    batch = _run(ROLLOUT, root, digest=_digest_fn(tmp_path))
    assert batch.events
    assert {event.cwd_digest for event in batch.events} == {expected}
    (key,) = batch.next_cursor.files
    assert context_of(batch.next_cursor.files[key]).get("r", {}).get("d") == expected


def test_message_pins_record_cwd_digest(tmp_path: Path) -> None:
    root = _copy(tmp_path, "message")
    expected = _digest_fn(tmp_path)(FOLDER)
    batch = _run(MESSAGES, root, digest=_digest_fn(tmp_path))
    assert len(batch.events) == 2
    assert {event.cwd_digest for event in batch.events} == {expected}
    (key,) = batch.next_cursor.files
    assert context_of(batch.next_cursor.files[key]).get("r", {}).get("d") == expected


def test_no_digest_callable_means_no_digests_and_untouched_context(tmp_path: Path) -> None:
    rollout = _run(ROLLOUT, _copy(tmp_path, "rollout"))
    assert rollout.events and {event.cwd_digest for event in rollout.events} == {None}
    (key,) = rollout.next_cursor.files
    assert context_of(rollout.next_cursor.files[key]).get("r", {}).get("d") is None
    messages = _run(MESSAGES, _copy(tmp_path, "message"))
    assert {event.cwd_digest for event in messages.events} == {None}
    (mkey,) = messages.next_cursor.files
    assert context_of(messages.next_cursor.files[mkey]).get("r") is None


def test_digest_callable_receives_the_raw_folder_only(tmp_path: Path) -> None:
    root = _copy(tmp_path, "rollout")
    seen: list[str] = []
    batch = _run(ROLLOUT, root, digest=lambda folder: seen.append(folder) or "dd")
    assert seen == [FOLDER]
    assert {event.cwd_digest for event in batch.events} == {"dd"}


def test_old_schema_cursor_restarts_and_captures_the_digest(tmp_path: Path) -> None:
    root = _copy(tmp_path, "rollout")
    rel = (
        "sessions/2026/10/11/rollout-2026-10-11T09-00-00-aaaaaaaa-0000-4000-8000-000000000001.jsonl"
    )
    info = (root / rel).stat()
    head = hashlib.sha256((root / rel).read_bytes()[:4096]).hexdigest()
    consumed = FileCursor(
        info.st_dev, info.st_ino, info.st_size, info.st_size, info.st_mtime_ns, head
    )
    legacy = Cursor({file_key(rel): consumed}, {"v:": "rollout-3"})
    batch = _run(ROLLOUT, root, legacy, _digest_fn(tmp_path))
    assert batch.events  # fully re-read despite the consumed offset
    assert {event.cwd_digest for event in batch.events} != {None}
    current = Cursor({file_key(rel): consumed}, {"v:": "rollout-4"})
    settled = _run(ROLLOUT, root, current, _digest_fn(tmp_path))
    assert settled.events == ()  # the same cursor on the new schema stays settled


def test_turns_and_events_emit_no_digests(tmp_path: Path) -> None:
    digest = _digest_fn(tmp_path)
    turns_root = tmp_path / "turns"
    shutil.copytree(FIXTURES / "readers" / "turns", turns_root)
    assert {event.cwd_digest for event in _run(TURNS, turns_root, digest=digest).events} == {None}
    events_root = tmp_path / "events"
    shutil.copytree(FIXTURES / "readers" / "events", events_root)
    assert {event.cwd_digest for event in _run(EVENTS, events_root, digest=digest).events} == {None}


def test_digest_is_stable_json_and_short(tmp_path: Path) -> None:
    root = _copy(tmp_path, "message")
    batch = _run(MESSAGES, root, digest=_digest_fn(tmp_path))
    for event in batch.events:
        assert event.cwd_digest is not None and len(event.cwd_digest) <= 256
    json.dumps([event.cwd_digest for event in batch.events])
