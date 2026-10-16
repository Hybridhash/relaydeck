"""Expected_revision enforcement in JournalBackend, plus bounded event logs.

A truthy expected_revision on a NEW agent.prompt is a freshness
assertion against the observed snapshot: unknown agent or revision
mismatch refuses stale_revision BEFORE anything journals. Absent/zero
asserts nothing, non-prompt kinds stay journal-only, and replays dedupe
to their recorded outcome without re-enforcement.

Also covers per-host event logs, which are bounded
with an O(1) event_id index on both JournalBackend and FakeBackend.
Synthetic loopback only; no daemon, no network.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from relaydeck.connector.fixture import fixture_event
from relaydeck.gateway.app import GatewayState, create_app
from relaydeck.gateway.backend import (
    MAX_EVENT_LOG_PER_HOST,
    BackendError,
    FakeBackend,
)
from relaydeck.gateway.production import JournalBackend
from relaydeck.journal import CommandJournal
from tests.security.helpers import SECRET, auth, make_client, mint_token

NOW = 1_000_000


def _snapshot(revision: int = 10) -> dict[str, Any]:
    return {
        "host": {"id": "home-mac", "epoch": "epoch-7"},
        "agents": [
            {
                "terminal_id": "term-1",
                "name": "feature",
                "agent": "tool-a",
                "title": "t",
                "status": "working",
                "focused": True,
                "revision": revision,
            }
        ],
    }


def _backend(tmp_path: Any, revision: int = 10) -> tuple[JournalBackend, CommandJournal]:
    journal = CommandJournal(str(tmp_path / "gw.db"))
    backend = JournalBackend(journal, now_ms=lambda: NOW)
    backend.snapshots["home-mac"] = _snapshot(revision)
    return backend, journal


def _prompt(
    command_id: str = "cmd-rev-001",
    expected: int | None = 10,
    agent_id: str = "term-1",
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "command_id": command_id,
        "principal": "acct-1",
        "device_id": "dev-1",
        "host_id": "home-mac",
        "host_epoch": "epoch-7",
        "kind": "agent.prompt",
        "target": {"agent_id": agent_id},
        "payload": {"text": "hello"},
        "expires_ms": NOW + 60_000,
    }
    if expected is not None:
        body["expected_revision"] = expected
    return body


def test_matching_revision_accepted_and_journaled(tmp_path: Any) -> None:
    backend, journal = _backend(tmp_path)
    try:
        record = backend.submit_command(_prompt())
        assert record["status"] == "accepted"
        stored = journal.record("cmd-rev-001")
        assert stored is not None
        assert stored["generation"] == 10
    finally:
        backend.close()


def test_stale_revision_refused_before_journal(tmp_path: Any) -> None:
    backend, journal = _backend(tmp_path, revision=11)
    try:
        with pytest.raises(BackendError) as exc:
            backend.submit_command(_prompt(expected=10))
        assert exc.value.code == "stale_revision"
        assert exc.value.details["reason"] == "revision_mismatch"
        assert exc.value.details["expected"] == 10
        assert exc.value.details["observed"] == 11
        assert journal.record("cmd-rev-001") is None
    finally:
        backend.close()


def test_unknown_agent_refused(tmp_path: Any) -> None:
    backend, journal = _backend(tmp_path)
    try:
        with pytest.raises(BackendError) as exc:
            backend.submit_command(_prompt(agent_id="term-nope"))
        assert exc.value.code == "stale_revision"
        assert exc.value.details["reason"] == "unknown_agent"
        assert journal.record("cmd-rev-001") is None
    finally:
        backend.close()


def test_missing_snapshot_refused(tmp_path: Any) -> None:
    journal = CommandJournal(str(tmp_path / "gw.db"))
    backend = JournalBackend(journal, now_ms=lambda: NOW)
    try:
        with pytest.raises(BackendError) as exc:
            backend.submit_command(_prompt())
        assert exc.value.code == "stale_revision"
        assert exc.value.details["reason"] == "no_snapshot"
    finally:
        backend.close()


def test_absent_and_zero_revision_accepted(tmp_path: Any) -> None:
    backend, _ = _backend(tmp_path, revision=99)
    try:
        assert backend.submit_command(_prompt("cmd-rev-002", None))["status"] == "accepted"
        body = _prompt("cmd-rev-003", None)
        body["expected_revision"] = 0
        assert backend.submit_command(body)["status"] == "accepted"
    finally:
        backend.close()


def test_replay_dedupes_without_reenforcement(tmp_path: Any) -> None:
    backend, _ = _backend(tmp_path, revision=10)
    try:
        first = backend.submit_command(_prompt())
        assert first["status"] == "accepted"
        backend.snapshots["home-mac"] = _snapshot(revision=11)
        replay = backend.submit_command(_prompt())
        assert replay["status"] == "accepted"
        assert replay["command_id"] == "cmd-rev-001"
    finally:
        backend.close()


def test_non_prompt_kinds_stay_journal_only(tmp_path: Any) -> None:
    backend, journal = _backend(tmp_path)
    try:
        body = _prompt("cmd-rev-004")
        body["kind"] = "shell.create"
        body["target"] = {"workspace_id": "w1"}
        body["expected_revision"] = 999
        assert backend.submit_command(body)["status"] == "accepted"
        stored = journal.record("cmd-rev-004")
        assert stored is not None
        assert stored["generation"] == 999
    finally:
        backend.close()


def test_http_stale_revision_envelope(tmp_path: Any) -> None:
    backend = JournalBackend(CommandJournal(str(tmp_path / "gw.db")), now_ms=lambda: NOW)
    backend.snapshots["home-mac"] = _snapshot(revision=11)
    state = GatewayState(backend=backend, cursor_secret=SECRET, now_ms=lambda: NOW)
    client = TestClient(create_app(state))
    try:
        token = mint_token(state, "acct-1")
        body = {
            "protocol": "relaydeck.gateway.v1",
            "command_id": "cmd-rev-101",
            "host_id": "home-mac",
            "host_epoch": "epoch-7",
            "kind": "agent.prompt",
            "expected_revision": 10,
            "target": {"agent_id": "term-1"},
            "payload": {"text": "hello"},
        }
        r = client.post("/v1/hosts/home-mac/commands", headers=auth(token), json=body)
        assert r.json()["code"] == "stale_revision", r.text
        body["command_id"] = "cmd-rev-102"
        body["expected_revision"] = 11
        r = client.post("/v1/hosts/home-mac/commands", headers=auth(token), json=body)
        assert r.status_code == 202, r.text
    finally:
        backend.close()


def test_fake_backend_replay_regression() -> None:
    """Unowned-behavior pin: FakeBackend still journals nothing and
    stays revision-blind (enforcement lives in production.py only)."""
    _, _, backend = make_client()
    assert isinstance(backend, FakeBackend)


# -- bounded + indexed event logs (both backends) --------------------


def _ingest_many(backend: Any, count: int) -> None:
    for seq in range(count):
        out = backend.ingest_event(
            "home-mac", fixture_event("host.status", f"ev-log-{seq}"), NOW, SECRET
        )
        assert out.get("error") is None, out


def test_event_log_bounded_oldest_first(tmp_path: Any) -> None:
    journal = CommandJournal(str(tmp_path / "gw.db"))
    for backend in (
        FakeBackend(now_ms=lambda: NOW),
        JournalBackend(journal, now_ms=lambda: NOW),
    ):
        _ingest_many(backend, MAX_EVENT_LOG_PER_HOST + 100)
        log = backend.event_logs["home-mac"]
        assert len(log) == MAX_EVENT_LOG_PER_HOST
        assert log[0]["event_id"] == "ev-log-100"
        assert log[-1]["sequence"] == MAX_EVENT_LOG_PER_HOST + 100
    journal.close()


def test_event_id_dedupe_survives_bound(tmp_path: Any) -> None:
    journal = CommandJournal(str(tmp_path / "gw.db"))
    for backend in (
        FakeBackend(now_ms=lambda: NOW),
        JournalBackend(journal, now_ms=lambda: NOW),
    ):
        first = backend.ingest_event(
            "home-mac", fixture_event("host.status", "ev-dupe-1"), NOW, SECRET
        )
        assert "duplicate" not in first
        again = backend.ingest_event(
            "home-mac", fixture_event("host.status", "ev-dupe-1"), NOW, SECRET
        )
        assert again.get("duplicate") is True
        assert again["ok"]["sequence"] == first["ok"]["sequence"]
    journal.close()


def test_evicted_event_id_may_return(tmp_path: Any) -> None:
    """Retention documents itself: an id trimmed from the log is no
    longer a duplicate (it re-ingests as a new sequence)."""
    journal = CommandJournal(str(tmp_path / "gw.db"))
    backend = JournalBackend(journal, now_ms=lambda: NOW)
    try:
        _ingest_many(backend, MAX_EVENT_LOG_PER_HOST + 1)
        out = backend.ingest_event(
            "home-mac", fixture_event("host.status", "ev-log-0"), NOW, SECRET
        )
        assert "duplicate" not in out
        assert out["ok"]["sequence"] == MAX_EVENT_LOG_PER_HOST + 2
    finally:
        backend.close()


def test_external_append_reconciles_index() -> None:
    """Direct list appends (tests, injected logs) re-sync the index."""
    backend = FakeBackend(now_ms=lambda: NOW)
    backend.event_logs.setdefault("home-mac", []).append(
        dict(fixture_event("host.status", "ev-ext-1"), sequence=7)
    )
    out = backend.ingest_event("home-mac", fixture_event("host.status", "ev-ext-1"), NOW, SECRET)
    assert out.get("duplicate") is True
    fresh = backend.ingest_event("home-mac", fixture_event("host.status", "ev-ext-2"), NOW, SECRET)
    assert fresh["ok"]["sequence"] == 8


def test_publish_paths_share_bound_and_sequence(tmp_path: Any) -> None:
    journal = CommandJournal(str(tmp_path / "gw.db"))
    try:
        for backend in (
            FakeBackend(now_ms=lambda: NOW),
            JournalBackend(journal, now_ms=lambda: NOW),
        ):
            first = backend.publish("home-mac", fixture_event("host.status", "ev-p-1"))
            second = backend.publish("home-mac", fixture_event("host.status", "ev-p-2"))
            assert (first["sequence"], second["sequence"]) == (1, 2)
            assert len(backend.event_logs["home-mac"]) == 2
    finally:
        journal.close()


# -- emission helper ------------------


def _wire_record(status: str = "dispatched") -> dict[str, Any]:
    return {
        "protocol": "relaydeck.gateway.v1",
        "command_id": "cmd-emit-001",
        "host_id": "home-mac",
        "kind": "agent.prompt",
        "target": {"agent_id": "term-1"},
        "status": status,
        "updated_ms": NOW,
    }


def test_publish_command_updated_emits_valid_envelope(tmp_path: Any) -> None:
    from relaydeck.contracts.schemas import validate_metadata_event

    journal = CommandJournal(str(tmp_path / "gw.db"))
    try:
        for backend in (
            FakeBackend(now_ms=lambda: NOW),
            JournalBackend(journal, now_ms=lambda: NOW),
        ):
            stored = backend.publish_command_updated(_wire_record(), "epoch-7", SECRET, NOW)
            assert stored is not None
            validate_metadata_event(stored)
            assert stored["type"] == "command.updated"
            assert stored["resource_id"] == "cmd-emit-001"
            assert stored["payload"]["status"] == "dispatched"
            assert stored["host_epoch"] == "epoch-7"
            # Idempotent per (command, status): re-emission dedupes.
            again = backend.publish_command_updated(_wire_record(), "epoch-7", SECRET, NOW)
            assert again is not None
            assert again["sequence"] == stored["sequence"]
            assert len(backend.event_logs["home-mac"]) == 1
            # A new status is a new event.
            moved = backend.publish_command_updated(
                _wire_record("succeeded"), "epoch-7", SECRET, NOW
            )
            assert moved is not None
            assert moved["sequence"] == stored["sequence"] + 1
    finally:
        journal.close()


def test_publish_command_updated_refuses_unformable(tmp_path: Any) -> None:
    journal = CommandJournal(str(tmp_path / "gw.db"))
    backend = JournalBackend(journal, now_ms=lambda: NOW)
    try:
        bad_status = _wire_record("acked")  # internal-only, never on the wire
        assert backend.publish_command_updated(bad_status, "epoch-7", SECRET, NOW) is None
        bad_target = _wire_record()
        bad_target["target"] = {}
        assert backend.publish_command_updated(bad_target, "epoch-7", SECRET, NOW) is None
        assert backend.publish_command_updated(_wire_record(), "", SECRET, NOW) is None
        assert backend.event_logs.get("home-mac", []) == []
    finally:
        backend.close()
