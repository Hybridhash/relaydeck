"""Durable command journal + unknown outcomes.

Journal core (sqlite, 0600, WAL): dedupe same-ID/same-body, conflict on
same-ID/changed-body, full status lifecycle, TTL expiry, tombstone
retention, frame-receipt vs outcome separation. Connector dispatch down
the verified Herdr chains. HTTP + tunnel loopback over JournalBackend, negative-authorization
and replay tests. Live-push subscribe /
publish / unsubscribe incl. slow-socket disconnect and leak gates.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import stat
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import websockets.asyncio.client as ws_client
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from websockets.exceptions import ConnectionClosed

import relaydeck.gateway.app as app_module
from relaydeck.connector.journal import ConnectorDispatch
from relaydeck.contracts.schemas import GATEWAY_PROTOCOL, validate
from relaydeck.gateway import terminal_wire
from relaydeck.gateway.app import GatewayState, create_app
from relaydeck.gateway.production import JournalBackend
from relaydeck.journal import (
    DEFAULT_ACCEPT_TTL_MS,
    TERMINAL_FULL_MS,
    TOMBSTONE_MS,
    CommandJournal,
    JournalConflict,
    digest_for,
)
from tests.support.creation.command_journal import NOW, _dispatch_msg
from tests.support.live_server import LiveGateway

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "gateway"
SECRET = b"command-journal-test-secret-32byts"
ALL_CAPS = frozenset(
    {
        "sessions.read",
        "terminal.view",
        "terminal.control",
        "agents.prompt",
        "agents.start",
        "shells.create",
        "terminal.attach",
        "hosts.enroll",
    }
)


def _exp(now_ms: int = NOW, ttl_s: int = 120) -> str:
    return datetime.fromtimestamp((now_ms + ttl_s * 1000) / 1000, UTC).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _envelope(
    command_id: str,
    kind: str = "agent.start",
    target: dict[str, str] | None = None,
    payload: dict[str, Any] | None = None,
    host_id: str = "home-mac",
    host_epoch: str = "epoch-7",
    expires_at: str | None = None,
) -> dict[str, Any]:
    return {
        "protocol": GATEWAY_PROTOCOL,
        "command_id": command_id,
        "host_id": host_id,
        "host_epoch": host_epoch,
        "kind": kind,
        "target": {"workspace_id": "w1"} if target is None else target,
        "payload": {"kind": "tool-a", "name": "web"} if payload is None else payload,
        "expires_at": _exp() if expires_at is None else expires_at,
    }


def _seed(tmp_path: Path, now_ms: int = NOW) -> tuple[GatewayState, JournalBackend, CommandJournal]:
    journal = CommandJournal(tmp_path / "gw.db", clock=lambda: now_ms)
    backend = JournalBackend(
        journal,
        now_ms=lambda: now_ms,
        snapshots={"home-mac": json.loads((FIXTURES / "snapshot.json").read_text())},
    )
    state = GatewayState(backend=backend, cursor_secret=SECRET, now_ms=lambda: now_ms)
    return state, backend, journal


def _token(state: GatewayState, caps: frozenset[str] = ALL_CAPS) -> str:
    access, _ = state.sessions.create("acct-1", caps, frozenset({"home-mac"}), NOW)
    return access


def _seed_mutable(
    tmp_path: Path, name: str = "gw.db"
) -> tuple[GatewayState, JournalBackend, CommandJournal, dict[str, int]]:
    """Seed with a mutable clock (`now["ms"]` advances TTL deterministically)."""
    now = {"ms": NOW}
    journal = CommandJournal(tmp_path / name, clock=lambda: now["ms"])
    backend = JournalBackend(
        journal,
        now_ms=lambda: now["ms"],
        snapshots={"home-mac": json.loads((FIXTURES / "snapshot.json").read_text())},
    )
    state = GatewayState(backend=backend, cursor_secret=SECRET, now_ms=lambda: now["ms"])
    return state, backend, journal, now


def _rec(journal: CommandJournal, command_id: str) -> dict[str, Any]:
    """Fetch a journal record that must exist (asserts presence)."""
    rec = journal.record(command_id)
    assert rec is not None
    return rec


def _wait_status(journal: CommandJournal, command_id: str, expected: str) -> None:
    """Keepalive does not acknowledge completion of the ordered journal lane."""
    deadline = time.monotonic() + 2
    while _rec(journal, command_id)["status"] != expected and time.monotonic() < deadline:
        time.sleep(0.001)
    assert _rec(journal, command_id)["status"] == expected


# -- journal core ------------------------------------------------------------


def test_submit_dedupe_conflict_and_current_outcome(tmp_path: Path) -> None:
    journal = CommandJournal(tmp_path / "j.db", clock=lambda: NOW)
    try:
        first = journal.submit(
            command_id="cmd-001",
            principal="acct-1",
            host_id="home-mac",
            kind="agent.start",
            target={"workspace_id": "w1"},
            payload={"kind": "tool-a", "name": "web"},
            now=NOW,
        )
        assert first["status"] == "accepted"
        validate("relaydeck/gateway/v1/command-record", first)
        # Same ID + same body: dedupe, returns the CURRENT outcome.
        assert journal.mark_dispatched("cmd-001", now=NOW + 1)
        again = journal.submit(
            command_id="cmd-001",
            principal="acct-1",
            host_id="home-mac",
            kind="agent.start",
            target={"workspace_id": "w1"},
            payload={"kind": "tool-a", "name": "web"},
            now=NOW + 2,
        )
        assert again["status"] == "dispatched"
        assert again["command_id"] == "cmd-001"
        # Same ID + changed body: conflict, original untouched.
        with pytest.raises(JournalConflict):
            journal.submit(
                command_id="cmd-001",
                principal="acct-1",
                host_id="home-mac",
                kind="agent.start",
                target={"workspace_id": "w1"},
                payload={"kind": "tool-a", "name": "other"},
                now=NOW + 3,
            )
        assert _rec(journal, "cmd-001")["payload"] == {"kind": "tool-a", "name": "web"}
    finally:
        journal.close()


def test_lifecycle_transitions_and_guards(tmp_path: Path) -> None:
    journal = CommandJournal(tmp_path / "j.db", clock=lambda: NOW)
    try:
        journal.submit(
            command_id="cmd-010",
            principal="a",
            host_id="h",
            kind="agent.prompt",
            target={"agent_id": "ag"},
            payload={"text": "hi"},
            now=NOW,
        )
        # accepted never becomes unknown or terminal directly.
        assert journal.mark_unknown("cmd-010", now=NOW) is False
        assert journal.mark_result("cmd-010", "succeeded", result_digest="x", now=NOW) is False
        assert journal.mark_dispatched("cmd-010", now=NOW) is True
        assert journal.mark_dispatched("cmd-010", now=NOW) is False  # no re-move
        assert journal.mark_acked("cmd-010", now=NOW) is True
        assert journal.mark_acked("cmd-010", now=NOW) is True  # idempotent
        assert journal.mark_dispatched("cmd-010", now=NOW) is False  # never backward
        assert (
            journal.mark_result(
                "cmd-010",
                "succeeded",
                result_digest="d1",
                result={"type": "agent_prompted"},
                now=NOW,
            )
            is True
        )
        rec = journal.record("cmd-010")
        assert rec is not None and rec["status"] == "succeeded"
        assert rec["result_digest"] == "d1"
        assert rec["result"] == {"type": "agent_prompted"}
        # Terminal records never move again.
        assert journal.mark_unknown("cmd-010", now=NOW) is False
        assert journal.mark_result("cmd-010", "failed", error_code="x", now=NOW) is False
        assert journal.note_progress("cmd-010", {"a": "b"}, now=NOW) is False
        # Unknown resolves only via authoritative result, never resubmit.
        journal.submit(
            command_id="cmd-011",
            principal="a",
            host_id="h",
            kind="shell.create",
            target={"workspace_id": "w"},
            payload={},
            now=NOW,
        )
        assert journal.mark_dispatched("cmd-011", now=NOW)
        assert journal.mark_unknown("cmd-011", now=NOW)
        assert (
            journal.mark_result("cmd-011", "failed", error_code="pane_not_found", now=NOW) is True
        )
        assert _rec(journal, "cmd-011")["status"] == "failed"
        with pytest.raises(ValueError):
            journal.mark_result("cmd-011", "bogus", now=NOW)
    finally:
        journal.close()


def test_wire_mapping_covers_internal_statuses(tmp_path: Path) -> None:
    journal = CommandJournal(tmp_path / "j.db", clock=lambda: NOW)
    try:
        journal.submit(
            command_id="w-1",
            principal="a",
            host_id="h",
            kind="agent.prompt",
            target={"agent_id": "ag"},
            payload={"text": "t"},
            now=NOW,
        )
        assert journal.mark_dispatched("w-1", now=NOW)
        assert journal.mark_acked("w-1", now=NOW)
        acked = journal.status("w-1")
        assert acked is not None and acked["status"] == "dispatched"  # wire view
        assert _rec(journal, "w-1")["status"] == "acked"  # journal truth
        validate("relaydeck/gateway/v1/command-record", acked)
        journal.submit(
            command_id="w-2",
            principal="a",
            host_id="h",
            kind="agent.prompt",
            target={"agent_id": "ag"},
            payload={"text": "t"},
            expires_ms=NOW + 10,
            now=NOW,
        )
        journal.expire_sweep(now=NOW + 11)
        expired = journal.status("w-2")
        assert expired is not None
        assert expired["status"] == "rejected" and expired["error_code"] == "expired"
        assert _rec(journal, "w-2")["status"] == "expired"
        validate("relaydeck/gateway/v1/command-record", expired)
    finally:
        journal.close()


def test_expiry_sweep_ttl_rules(tmp_path: Path) -> None:
    journal = CommandJournal(tmp_path / "j.db", clock=lambda: NOW)
    try:
        # Explicit expiry, never dispatched -> expired (proven non-execution).
        journal.submit(
            command_id="e-1",
            principal="a",
            host_id="h",
            kind="shell.create",
            target={"workspace_id": "w"},
            payload={},
            expires_ms=NOW + 100,
            now=NOW,
        )
        # Dispatched past deadline -> unknown (may have executed).
        journal.submit(
            command_id="e-2",
            principal="a",
            host_id="h",
            kind="shell.create",
            target={"workspace_id": "w"},
            payload={},
            expires_ms=NOW + 100,
            now=NOW,
        )
        assert journal.mark_dispatched("e-2", now=NOW)
        # No explicit expiry -> default accept TTL from creation.
        journal.submit(
            command_id="e-3",
            principal="a",
            host_id="h",
            kind="shell.create",
            target={"workspace_id": "w"},
            payload={},
            now=NOW,
        )
        assert journal.expire_sweep(now=NOW + 50) == {"expired": [], "unknown": []}
        swept = journal.expire_sweep(now=NOW + 101)
        assert swept == {"expired": ["e-1"], "unknown": ["e-2"]}
        assert _rec(journal, "e-1")["status"] == "expired"
        assert _rec(journal, "e-2")["status"] == "unknown"
        swept = journal.expire_sweep(now=NOW + DEFAULT_ACCEPT_TTL_MS + 1)
        assert swept == {"expired": ["e-3"], "unknown": []}
    finally:
        journal.close()


def test_tombstone_retention_and_purge(tmp_path: Path) -> None:
    journal = CommandJournal(tmp_path / "j.db", clock=lambda: NOW)
    try:
        journal.submit(
            command_id="t-1",
            principal="a",
            host_id="h",
            kind="agent.prompt",
            target={"agent_id": "ag"},
            payload={"text": "secret-prompt"},
            now=NOW,
        )
        assert journal.mark_dispatched("t-1", now=NOW)
        assert journal.mark_result(
            "t-1", "succeeded", result_digest="d", result={"type": "ok"}, now=NOW
        )
        assert journal.purge(now=NOW + 1) == {"tombstoned": 0, "purged": 0, "frames": 0}
        # Terminal + 24h -> tombstone: bodies dropped, dedupe/conflict live on.
        out = journal.purge(now=NOW + TERMINAL_FULL_MS + 1)
        assert out == {"tombstoned": 1, "purged": 0, "frames": 0}
        rec = journal.record("t-1")
        assert rec is not None and rec["tombstone"] is True
        assert rec["payload"] == {} and rec["result"] == {}
        assert rec["target"] == {"agent_id": "ag"}  # routing survives
        same = journal.submit(
            command_id="t-1",
            principal="a",
            host_id="h",
            kind="agent.prompt",
            target={"agent_id": "ag"},
            payload={"text": "secret-prompt"},
            now=NOW + TERMINAL_FULL_MS + 2,
        )
        assert same["status"] == "succeeded"  # dedupe via tombstone digest
        with pytest.raises(JournalConflict):
            journal.submit(
                command_id="t-1",
                principal="a",
                host_id="h",
                kind="agent.prompt",
                target={"agent_id": "ag"},
                payload={"text": "changed"},
                now=NOW + TERMINAL_FULL_MS + 3,
            )
        # Tombstone + 7d -> purged: the ID is new again (window limit).
        out = journal.purge(now=NOW + TOMBSTONE_MS + TERMINAL_FULL_MS + 10)
        assert out["purged"] == 1
        assert journal.record("t-1") is None
        fresh = journal.submit(
            command_id="t-1",
            principal="a",
            host_id="h",
            kind="agent.prompt",
            target={"agent_id": "ag"},
            payload={"text": "changed"},
            now=NOW + TOMBSTONE_MS + TERMINAL_FULL_MS + 11,
        )
        assert fresh["status"] == "accepted"
    finally:
        journal.close()


def test_frame_receipt_separation(tmp_path: Path) -> None:
    journal = CommandJournal(tmp_path / "j.db", clock=lambda: NOW)
    try:
        journal.submit(
            command_id="f-1",
            principal="a",
            host_id="h",
            kind="agent.prompt",
            target={"agent_id": "ag"},
            payload={"text": "t"},
            now=NOW,
        )
        assert journal.record_frame_receipt("lease-1", 3, 7, now=NOW) is True
        assert journal.record_frame_receipt("lease-1", 3, 7, now=NOW) is False  # dup
        assert journal.is_frame_received("lease-1", 3, 7) is True
        assert journal.is_frame_received("lease-1", 3, 8) is False
        # Frame ACKs never move command state; commands never consult frames.
        assert _rec(journal, "f-1")["status"] == "accepted"
        assert journal.mark_dispatched("f-1", now=NOW) is True
        assert journal.is_frame_received("lease-1", 3, 7) is True
        # Crash recovery ignores frame rows (only TTL purge drops them).
        assert journal.recover(now=NOW)["unknown"] == ["f-1"]
        assert journal.is_frame_received("lease-1", 3, 7) is True
    finally:
        journal.close()


def test_file_perms_wal_and_reopen(tmp_path: Path) -> None:
    import sqlite3

    target = tmp_path / "j.db"
    journal = CommandJournal(target, clock=lambda: NOW)
    journal.submit(
        command_id="p-1",
        principal="a",
        host_id="h",
        kind="shell.create",
        target={"workspace_id": "w"},
        payload={},
        now=NOW,
    )
    assert journal.durability() == {"journal_mode": "wal", "synchronous": 2}  # FULL
    journal.close()
    mode = stat.S_IMODE(os.stat(target).st_mode)
    assert mode == 0o600, oct(mode)
    probe = sqlite3.connect(str(target))
    try:
        assert probe.execute("PRAGMA journal_mode;").fetchone()[0].lower() == "wal"
        row = probe.execute("SELECT v FROM journal_meta WHERE k='version'").fetchone()
        assert row[0] == "1"
    finally:
        probe.close()
    reopened = CommandJournal(target, clock=lambda: NOW)
    try:
        rec = reopened.record("p-1")
        assert rec is not None and rec["status"] == "accepted"
        assert rec["digest"] == digest_for("h", "shell.create", {"workspace_id": "w"}, {})
    finally:
        reopened.close()


def test_sidecar_perms_0600_after_writes(tmp_path: Path) -> None:
    """Every mutation commit re-chmods the WAL sidecars to 0600 (they
    inherit the umask at creation), so prompt text is never left
    world-readable on disk after a write."""
    target = tmp_path / "j.db"
    journal = CommandJournal(target, clock=lambda: NOW)
    try:
        journal.submit(
            command_id="s-1",
            principal="a",
            host_id="h",
            kind="agent.prompt",
            target={"agent_id": "ag"},
            payload={"text": "secret-prompt"},
            now=NOW,
        )
        assert journal.mark_dispatched("s-1", now=NOW)
        for suffix in ("-wal", "-shm"):
            sidecar = Path(str(target) + suffix)
            assert sidecar.exists(), suffix
            mode = stat.S_IMODE(os.stat(sidecar).st_mode)
            assert mode == 0o600, (suffix, oct(mode))
    finally:
        journal.close()


# -- HTTP over JournalBackend -------------------------------------------------


def test_http_submit_dedupe_conflict_status(tmp_path: Path) -> None:
    state, backend, journal = _seed(tmp_path)
    try:
        client = TestClient(create_app(state))
        token = _token(state)
        headers = {"Authorization": f"Bearer {token}"}
        body = _envelope("http-001")
        first = client.post("/v1/hosts/home-mac/commands", headers=headers, json=body)
        assert first.status_code == 202, first.text
        assert first.json()["status"] == "accepted"
        assert _rec(journal, "http-001")["principal"] == "acct-1"
        replay = client.post("/v1/hosts/home-mac/commands", headers=headers, json=body)
        assert replay.status_code == 202
        assert replay.json()["status"] == "accepted"
        assert len(backend.drain_outbox("home-mac")) == 1  # dispatched once
        assert backend.drain_outbox("home-mac") == []
        clash = dict(body, payload={"kind": "tool-a", "name": "other", "argv": ["x"]})
        denied = client.post("/v1/hosts/home-mac/commands", headers=headers, json=clash)
        assert denied.status_code == 409
        assert denied.json()["code"] == "command_conflict"
        status = client.get("/v1/hosts/home-mac/commands/http-001", headers=headers)
        assert status.status_code == 200
        assert status.json()["command_id"] == "http-001"
        missing = client.get("/v1/hosts/home-mac/commands/nope", headers=headers)
        assert missing.status_code == 404
    finally:
        backend.close()


def test_http_dispatch_ack_result_lifecycle(tmp_path: Path) -> None:
    state, backend, journal = _seed(tmp_path)
    try:
        client = TestClient(create_app(state))
        token = _token(state)
        headers = {"Authorization": f"Bearer {token}"}
        body = _envelope(
            "http-002",
            kind="agent.prompt",
            target={"agent_id": "ag-1"},
            payload={"text": "explain"},
        )
        assert (
            client.post("/v1/hosts/home-mac/commands", headers=headers, json=body).status_code
            == 202
        )
        host_token = state.hosts.enroll("home-mac", "Home Mac", "acct-1")
        with client.websocket_connect(f"/v1/connectors?token={host_token}") as ws:
            hello = json.loads(ws.receive_text())
            assert hello == {
                "kind": "hello",
                "host_id": "home-mac",
                "snapshot_chunks": True,
                "epochs": [],
            }
            dispatch = json.loads(ws.receive_text())
            assert dispatch["kind"] == "command.dispatch"
            assert dispatch["command_id"] == "http-002"
            assert dispatch["gateway_kind"] == "agent.prompt"
            assert dispatch["payload"] == {"text": "explain"}
            # Write-ahead proof: the marker precedes the wire bytes we hold.
            assert _rec(journal, "http-002")["status"] == "dispatched"
            ws.send_text(
                json.dumps({"kind": "command.ack", "command_id": "http-002", "host_id": "home-mac"})
            )
            ws.send_text(json.dumps({"kind": "ping"}))
            assert json.loads(ws.receive_text()) == {"kind": "pong"}
            _wait_status(journal, "http-002", "acked")
            assert _rec(journal, "http-002")["status"] == "acked"
            ws.send_text(
                json.dumps(
                    {
                        "kind": "command.result",
                        "command_id": "http-002",
                        "host_id": "home-mac",
                        "status": "succeeded",
                        "result_digest": "d-9",
                        "result": {"type": "agent_prompted"},
                    }
                )
            )
            ws.send_text(json.dumps({"kind": "ping"}))
            assert json.loads(ws.receive_text()) == {"kind": "pong"}
            _wait_status(journal, "http-002", "succeeded")
            rec = journal.record("http-002")
            assert rec is not None and rec["status"] == "succeeded"
            assert rec["result_digest"] == "d-9"
            assert rec["result"] == {"type": "agent_prompted"}
            status = client.get("/v1/hosts/home-mac/commands/http-002", headers=headers)
            assert status.json()["status"] == "succeeded"
            ws.send_text(json.dumps({"kind": "bye"}))
    finally:
        backend.close()


def test_http_session_close_severs_sockets_and_dispatches(tmp_path: Path) -> None:
    state, backend, journal = _seed(tmp_path)
    try:
        client = TestClient(create_app(state))
        token = _token(state)
        headers = {"Authorization": f"Bearer {token}"}
        attachment, _ = backend.create_attachment(
            "home-mac", "term-9", "shell", "acct-1", "dev-1", takeover=False
        )
        lease = backend.acquire_control(attachment.attachment_id, "acct-1", "dev-1")

        class Stub:
            def __init__(self) -> None:
                self.closes: list[tuple[int, str]] = []

            async def close(self, code: int = 1000, reason: str = "") -> None:
                self.closes.append((code, reason))

            async def send_text(self, text: str) -> None:
                pass

        stub = Stub()
        state.sockets.register("acct-1", lease.lease_id, stub, "dev-1")
        body = _envelope(
            "http-003", kind="session.close", target={"terminal_id": "term-9"}, payload={}
        )
        done = client.post("/v1/hosts/home-mac/commands", headers=headers, json=body)
        assert done.status_code == 202, done.text
        assert stub.closes == [(1008, "lease_expired")]
        closed_att = backend.get_attachment(attachment.attachment_id)
        assert closed_att is not None and closed_att.closed is True
        assert backend.open_lease(lease.lease_id) is None
        pending = backend.drain_outbox("home-mac")
        assert len(pending) == 1
        assert pending[0]["gateway_kind"] == "session.close"
        assert pending[0]["target"] == {"terminal_id": "term-9"}
        assert _rec(journal, "http-003")["status"] == "accepted"
    finally:
        backend.close()


# -- live-path TTL (enqueue + dispatch both enforce) ---------------------------


def test_live_path_ttl_pump_never_sends_expired(tmp_path: Path) -> None:
    """Enqueue, advance the clock past TTL, open the tunnel: the pump
    sends nothing (hello, then pong to our ping -- no dispatch) and
    the record settles expired."""
    state, backend, journal, now = _seed_mutable(tmp_path)
    try:
        client = TestClient(create_app(state))
        token = _token(state)
        headers = {"Authorization": f"Bearer {token}"}
        body = _envelope("ttl-0001")  # expires NOW + 120 s
        assert (
            client.post("/v1/hosts/home-mac/commands", headers=headers, json=body).status_code
            == 202
        )
        assert _rec(journal, "ttl-0001")["status"] == "accepted"
        now["ms"] = NOW + 121_000  # past TTL (access token still valid: 15 min)
        host_token = state.hosts.enroll("home-mac", "Home Mac", "acct-1")
        with client.websocket_connect(f"/v1/connectors?token={host_token}") as ws:
            assert json.loads(ws.receive_text()) == {
                "kind": "hello",
                "host_id": "home-mac",
                "snapshot_chunks": True,
                "epochs": [],
            }
            ws.send_text(json.dumps({"kind": "ping"}))
            assert json.loads(ws.receive_text()) == {"kind": "pong"}  # never a dispatch
            _wait_status(journal, "ttl-0001", "expired")
            ws.send_text(json.dumps({"kind": "bye"}))
        assert _rec(journal, "ttl-0001")["status"] == "expired"
        assert backend.drain_outbox("home-mac") == []
    finally:
        backend.close()


def test_resubmit_after_ttl_returns_expired_never_accepted(tmp_path: Path) -> None:
    """A resubmit past TTL returns expired (wire: rejected + expired),
    never accepted, and is never re-enqueued. The resubmit carries a
    fresh expires_at (the route 403s stale envelopes); dedupe still
    matches by UUID + body digest."""
    state, backend, journal, now = _seed_mutable(tmp_path)
    try:
        client = TestClient(create_app(state))
        token = _token(state)
        headers = {"Authorization": f"Bearer {token}"}
        assert (
            client.post(
                "/v1/hosts/home-mac/commands", headers=headers, json=_envelope("ttl-0002")
            ).status_code
            == 202
        )
        now["ms"] = NOW + 121_000  # past the original TTL
        again = client.post(
            "/v1/hosts/home-mac/commands",
            headers=headers,
            json=_envelope("ttl-0002", expires_at=_exp(now_ms=now["ms"])),
        )
        assert again.status_code == 202, again.text
        assert again.json()["status"] == "rejected"
        assert again.json()["error_code"] == "expired"
        assert _rec(journal, "ttl-0002")["status"] == "expired"
        # Never re-enqueued: only the stale first-enqueue message remains,
        # and the dispatch marker refuses it (proven never sent).
        pending = backend.drain_outbox("home-mac")
        assert [m["command_id"] for m in pending] == ["ttl-0002"]
        assert backend.mark_command_dispatched("ttl-0002", now["ms"]) is False
        assert _rec(journal, "ttl-0002")["status"] == "expired"
        assert backend.drain_outbox("home-mac") == []
    finally:
        backend.close()


# -- negative authorization + replay ----------------------------------


def test_negative_auth_forged_wronghost_revoked(tmp_path: Path) -> None:
    state, backend, _ = _seed(tmp_path)
    try:
        client = TestClient(create_app(state))
        token = _token(state)
        headers = {"Authorization": f"Bearer {token}"}
        body = _envelope("neg-0001")
        assert (
            client.post("/v1/hosts/home-mac/commands", headers=headers, json=body).status_code
            == 202
        )
        # Forged principal: bad token cannot submit or query.
        forged = {"Authorization": "Bearer forged-token-value"}
        assert (
            client.post("/v1/hosts/home-mac/commands", headers=forged, json=body).status_code == 401
        )
        assert client.get("/v1/hosts/home-mac/commands/neg-0001", headers=forged).status_code == 401
        # Wrong host path: another host namespace cannot see the command.
        other = state.sessions.create("acct-1", ALL_CAPS, frozenset({"other-mac"}), NOW)[0]
        r = client.get(
            "/v1/hosts/other-mac/commands/neg-0001", headers={"Authorization": f"Bearer {other}"}
        )
        assert r.status_code == 404
        # Revoked device: logout kills queries immediately.
        assert client.post("/v1/logout", headers=headers).status_code == 200
        assert (
            client.get("/v1/hosts/home-mac/commands/neg-0001", headers=headers).status_code == 401
        )
    finally:
        backend.close()


def test_negative_auth_tunnel_forgery_closes(tmp_path: Path) -> None:
    state, backend, journal = _seed(tmp_path)
    try:
        client = TestClient(create_app(state))
        token = _token(state)
        headers = {"Authorization": f"Bearer {token}"}
        # A second host exists; its connector must not report home-mac's ids.
        state.hosts.enroll("other-mac", "Other", "acct-1")
        other_token = state.hosts.enroll("other-mac", "Other", "acct-1")
        body = _envelope("neg-0010")
        assert (
            client.post("/v1/hosts/home-mac/commands", headers=headers, json=body).status_code
            == 202
        )
        assert journal.mark_dispatched("neg-0010", now=NOW)
        with client.websocket_connect(f"/v1/connectors?token={other_token}") as ws:
            ws.receive_text()  # hello
            backend.drain_outbox("home-mac")
            ws.send_text(
                json.dumps(
                    {
                        "kind": "command.result",
                        "command_id": "neg-0010",
                        "host_id": "other-mac",
                        "status": "succeeded",
                        "result_digest": "forged",
                    }
                )
            )
            with pytest.raises(WebSocketDisconnect) as exc:
                ws.receive_text()
            assert exc.value.code == 1008
        # Forged outcome refused: the record is untouched.
        assert _rec(journal, "neg-0010")["status"] == "dispatched"
    finally:
        backend.close()


def test_tunnel_forged_host_field_closes_and_leaves_record(tmp_path: Path) -> None:
    """Per-message host binding (not just owner_of): a tunnel message
    whose doc.host_id differs from the authenticated tunnel host fails
    closed with 1008 even when the tunnel owner COULD report that id."""
    state, backend, journal = _seed(tmp_path)
    try:
        client = TestClient(create_app(state))
        token = _token(state)
        headers = {"Authorization": f"Bearer {token}"}
        body = _envelope("neg-0011")
        assert (
            client.post("/v1/hosts/home-mac/commands", headers=headers, json=body).status_code
            == 202
        )
        host_token = state.hosts.enroll("home-mac", "Home Mac", "acct-1")
        with client.websocket_connect(f"/v1/connectors?token={host_token}") as ws:
            ws.receive_text()  # hello
            ws.receive_text()  # dispatch (pump marks neg-0011 dispatched)
            ws.send_text(
                json.dumps(
                    {
                        "kind": "command.result",
                        "command_id": "neg-0011",
                        "host_id": "other-mac",  # forged field: tunnel is home-mac
                        "status": "succeeded",
                        "result_digest": "forged",
                    }
                )
            )
            with pytest.raises(WebSocketDisconnect) as exc:
                ws.receive_text()
            assert exc.value.code == 1008
        # Still dispatched (legit pump), never transitioned by the forgery.
        assert _rec(journal, "neg-0011")["status"] == "dispatched"  # untouched
    finally:
        backend.close()


def test_negative_auth_result_for_undispatched_refused(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setattr(app_module, "TUNNEL_POLL_S", 3600.0)
    state, backend, journal = _seed(tmp_path)
    try:
        client = TestClient(create_app(state))
        token = _token(state)
        headers = {"Authorization": f"Bearer {token}"}
        body = _envelope("neg-0020")
        assert (
            client.post("/v1/hosts/home-mac/commands", headers=headers, json=body).status_code
            == 202
        )
        host_token = state.hosts.enroll("home-mac", "Home Mac", "acct-1")
        with client.websocket_connect(f"/v1/connectors?token={host_token}") as ws:
            ws.receive_text()  # hello
            ws.receive_text()  # dispatch (accepted -> dispatched, write-ahead)
            # A result for a DIFFERENT accepted-but-undispatched id is forged.
            body2 = _envelope("neg-0021")
            assert (
                client.post("/v1/hosts/home-mac/commands", headers=headers, json=body2).status_code
                == 202
            )
            backend.drain_outbox("home-mac")  # drained, marker NOT journaled
            assert _rec(journal, "neg-0021")["status"] == "accepted"
            ws.send_text(
                json.dumps(
                    {
                        "kind": "command.result",
                        "command_id": "neg-0021",
                        "host_id": "home-mac",
                        "status": "succeeded",
                        "result_digest": "forged",
                    }
                )
            )
            with pytest.raises(WebSocketDisconnect) as exc:
                ws.receive_text()
            assert exc.value.code == 1008
        assert _rec(journal, "neg-0021")["status"] == "accepted"
    finally:
        backend.close()


def test_replay_submit_and_status_have_no_side_effects(tmp_path: Path) -> None:
    state, backend, journal = _seed(tmp_path)
    try:
        client = TestClient(create_app(state))
        token = _token(state)
        headers = {"Authorization": f"Bearer {token}"}
        body = _envelope("rep-0001")
        first = client.post("/v1/hosts/home-mac/commands", headers=headers, json=body)
        assert first.status_code == 202
        for _ in range(3):  # replayed submits: identical outcome, one dispatch
            dup = client.post("/v1/hosts/home-mac/commands", headers=headers, json=body)
            assert dup.status_code == 202
            assert dup.json() == first.json()
        assert len(backend.drain_outbox("home-mac")) == 1
        before = journal.record("rep-0001")
        assert before is not None
        seen = client.get("/v1/hosts/home-mac/commands/rep-0001", headers=headers).json()
        again = client.get("/v1/hosts/home-mac/commands/rep-0001", headers=headers).json()
        assert seen == again  # replayed status reads are side-effect free
        after = journal.record("rep-0001")
        assert after is not None and after["updated_ms"] == before["updated_ms"]
        assert backend.drain_outbox("home-mac") == []
    finally:
        backend.close()


# -- connector dispatch (scripted Herdr; never the live daemon) ----------------


class _ScriptAdapter:
    async def workspace_create(
        self, *, cwd: str | None = None, label: str | None = None
    ) -> dict[str, Any]:
        raise AssertionError("Unexpected workspace creation")

    async def tab_rename(self, tab_id: str, label: str) -> dict[str, Any]:
        raise AssertionError("Unexpected tab rename")

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.fail: dict[str, Exception] = {}

    async def agent_prompt(self, target: str, text: str) -> dict[str, Any]:
        self.calls.append(("agent.prompt", target, text))
        if "agent.prompt" in self.fail:
            raise self.fail["agent.prompt"]
        return {"type": "agent_prompted", "agent": {"id": target}}

    async def tab_create(self, workspace_id: str, *, label: str | None = None) -> dict[str, Any]:
        self.calls.append(("tab.create", workspace_id, label or ""))
        if "tab.create" in self.fail:
            raise self.fail["tab.create"]
        return {
            "type": "tab_created",
            "tab": {"tab_id": "tab-1"},
            "root_pane": {"pane_id": "pane-1"},
        }

    async def agent_start(
        self,
        name: str,
        kind: str,
        pane_id: str,
        args: list[str] | None = None,
        timeout_ms: int | None = None,
    ) -> dict[str, Any]:
        self.calls.append(("agent.start", name, kind, pane_id, repr(sorted(args or []))))
        if "agent.start" in self.fail:
            raise self.fail["agent.start"]
        return {"type": "agent_started", "agent": {"id": "ag-9"}, "argv": args or []}

    async def pane_close(self, pane_id: str) -> dict[str, Any]:
        self.calls.append(("pane.close", pane_id))
        if "pane.close" in self.fail:
            raise self.fail["pane.close"]
        return {"type": "ok"}


def test_connector_executes_all_kinds(tmp_path: Path) -> None:
    async def scenario() -> None:
        journal = CommandJournal(tmp_path / "c.db", clock=lambda: NOW)
        try:
            adapter = _ScriptAdapter()
            dispatcher = ConnectorDispatch(journal, adapter, host_id="home-mac", clock=lambda: NOW)
            # agent.start: a named tab, then agent.start on its root pane.
            started = await dispatcher.dispatch(
                _dispatch_msg(
                    "cx-1",
                    "agent.start",
                    {"workspace_id": "w1"},
                    {"kind": "tool-a", "name": "web", "argv": ["--model", "o3"]},
                )
            )
            assert started["status"] == "succeeded"
            assert started["result"]["type"] == "agent_started"
            assert started["result"]["pane_id"] == "pane-1"
            assert adapter.calls == [
                ("tab.create", "w1", "web"),
                ("agent.start", "web", "tool-a", "pane-1", "['--model', 'o3']"),
            ]
            assert dispatcher.herdr_calls == 2
            # shell.create passes the label through to tab.create.
            shelled = await dispatcher.dispatch(
                _dispatch_msg("cx-2", "shell.create", {"workspace_id": "w1"}, {"label": "ops"})
            )
            assert shelled["result"]["type"] == "tab_created"
            assert adapter.calls[-1] == ("tab.create", "w1", "ops")
            # agent.prompt is the prompt_text seam, connector side.
            prompted = await dispatcher.dispatch(
                _dispatch_msg("cx-3", "agent.prompt", {"agent_id": "ag-1"}, {"text": "explain"})
            )
            assert prompted["result"] == {"type": "agent_prompted"}
            assert adapter.calls[-1] == ("agent.prompt", "ag-1", "explain")
            # session.close ends the pane (no upstream agent.close exists).
            closed = await dispatcher.dispatch(
                _dispatch_msg("cx-4", "session.close", {"terminal_id": "term-9"}, {})
            )
            assert closed["result"] == {"type": "ok"}
            assert list(adapter.calls[-1]) == ["pane.close", "term-9"]
            for cid in ("cx-1", "cx-2", "cx-3", "cx-4"):
                rec = journal.record(cid)
                assert rec is not None and rec["status"] == "succeeded"
                assert rec["result_digest"]  # every outcome carries a digest
        finally:
            journal.close()

    asyncio.run(scenario())


def test_connector_dedupe_never_redispatches(tmp_path: Path) -> None:
    async def scenario() -> None:
        journal = CommandJournal(tmp_path / "c.db", clock=lambda: NOW)
        try:
            adapter = _ScriptAdapter()
            dispatcher = ConnectorDispatch(journal, adapter, host_id="home-mac", clock=lambda: NOW)
            msg = _dispatch_msg("cx-9", "shell.create", {"workspace_id": "w1"}, {})
            first = await dispatcher.dispatch(msg)
            assert first["status"] == "succeeded"
            assert dispatcher.herdr_calls == 1
            second = await dispatcher.dispatch(dict(msg))  # redelivery
            assert second == first
            assert dispatcher.herdr_calls == 1  # zero new Herdr calls
            # Same ID + changed body: reject the conflicting identity, never execute.
            tampered = _dispatch_msg(
                "cx-9", "shell.create", {"workspace_id": "w1"}, {"label": "evil"}
            )
            third = await dispatcher.dispatch(tampered)
            assert third["status"] == "rejected"
            assert third["error_code"] == "command_conflict"
            stored = journal.record("cx-9")
            assert stored is not None and stored["status"] == "succeeded"
            assert dispatcher.herdr_calls == 1
        finally:
            journal.close()

    asyncio.run(scenario())


def test_connector_refused_marker_skips_execution(tmp_path: Path, monkeypatch: Any) -> None:
    """Defense in depth: a refused write-ahead marker (redelivery race
    or TTL lapsed mid-dispatch) returns the recorded outcome with zero
    new Herdr calls -- never executes twice."""

    async def scenario() -> None:
        journal = CommandJournal(tmp_path / "c.db", clock=lambda: NOW)
        try:
            adapter = _ScriptAdapter()
            dispatcher = ConnectorDispatch(journal, adapter, host_id="home-mac", clock=lambda: NOW)
            journal.submit(
                command_id="cx-r",
                principal="acct-1",
                host_id="home-mac",
                kind="shell.create",
                target={"workspace_id": "w1"},
                payload={},
                now=NOW,
            )
            monkeypatch.setattr(journal, "mark_dispatched", lambda command_id, now=None: False)
            out = await dispatcher.dispatch(
                _dispatch_msg("cx-r", "shell.create", {"workspace_id": "w1"}, {})
            )
            assert out["status"] == "unknown"  # recorded non-terminal outcome
            assert adapter.calls == []
            assert dispatcher.herdr_calls == 0
        finally:
            journal.close()

    asyncio.run(scenario())


def test_connector_digest_mismatch_and_unknown_kind(tmp_path: Path) -> None:
    async def scenario() -> None:
        journal = CommandJournal(tmp_path / "c.db", clock=lambda: NOW)
        try:
            adapter = _ScriptAdapter()
            dispatcher = ConnectorDispatch(journal, adapter, host_id="home-mac", clock=lambda: NOW)
            msg = _dispatch_msg("cx-t", "shell.create", {"workspace_id": "w1"}, {})
            msg["payload"] = {"label": "tampered"}  # digest no longer matches
            with pytest.raises(ValueError, match="digest mismatch"):
                await dispatcher.dispatch(msg)
            assert journal.record("cx-t") is None  # never journaled, never ran
            assert dispatcher.herdr_calls == 0
            weird = _dispatch_msg("cx-u", "workspace.create", {"x": "y"}, {})
            out = await dispatcher.dispatch(weird)
            assert out["status"] == "rejected"
            assert out["error_code"] == "unsupported_kind"
            assert dispatcher.herdr_calls == 0
        finally:
            journal.close()

    asyncio.run(scenario())


def test_connector_failures_map_to_failed_or_unknown(tmp_path: Path) -> None:
    from relaydeck.connector.herdr import HerdrError, HerdrProtocolError

    async def scenario() -> None:
        journal = CommandJournal(tmp_path / "c.db", clock=lambda: NOW)
        try:
            adapter = _ScriptAdapter()
            dispatcher = ConnectorDispatch(journal, adapter, host_id="home-mac", clock=lambda: NOW)
            adapter.fail["agent.prompt"] = HerdrError("agent_blocked", "blocked")
            out = await dispatcher.dispatch(
                _dispatch_msg("cx-f", "agent.prompt", {"agent_id": "a"}, {"text": "t"})
            )
            assert out["status"] == "failed"
            assert out["error_code"] == "agent_blocked"
            assert _rec(journal, "cx-f")["status"] == "failed"
            for name, exc in (
                ("cx-o", OSError("daemon gone")),
                ("cx-p", HerdrProtocolError("bad_envelope", "junk")),
                ("cx-t", TimeoutError("slow")),
            ):
                adapter.fail["agent.prompt"] = exc
                out = await dispatcher.dispatch(
                    _dispatch_msg(name, "agent.prompt", {"agent_id": "a"}, {"text": "t"})
                )
                assert out["status"] == "unknown", name
                assert _rec(journal, name)["status"] == "unknown", name
                # Reconcile query carries the record; resubmission is refused
                # by the gateway (conflict/dedupe), never a blind retry here.
                reconciled = dispatcher.reconcile(name)
                assert reconciled is not None and reconciled["status"] == "unknown"
        finally:
            journal.close()

    asyncio.run(scenario())


# -- live push over StreamFanout (deferred F4) ---------------------


async def _wait_for(pred: Any, timeout: float = 30.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not pred():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("timed out waiting for live-push condition")
        await asyncio.sleep(0.01)


def _live_ticket(state: GatewayState, lease_id: str) -> str:
    from relaydeck.gateway.identity import TicketGrant

    session = state.sessions.lookup(_token(state), NOW)
    assert session is not None
    return state.tickets.issue(
        TicketGrant(
            account=session.account,
            device_id=session.device_id,
            capabilities=session.capabilities,
            hosts=session.hosts,
            lease_id=lease_id,
            host_id="home-mac",
        ),
        NOW,
    )


def test_live_push_subscribe_publish_unsubscribe(tmp_path: Path) -> None:
    async def scenario() -> None:
        state, backend, _ = _seed(tmp_path)
        try:
            attachment, _ = backend.create_attachment(
                "home-mac", "term-1", "shell", "acct-1", "dev-1", takeover=False
            )
            lease = backend.acquire_control(attachment.attachment_id, "acct-1", "dev-1")
            ticket = _live_ticket(state, lease.lease_id)
            with LiveGateway(create_app(state)) as live:
                url = (
                    f"ws://127.0.0.1:{live.port}/v1/terminal/{lease.lease_id}"
                    f"?ticket={ticket}&epoch=e1&offset=0"
                )
                async with ws_client.connect(url) as ws:
                    await _wait_for(
                        lambda: backend.rings.viewer_count(attachment.attachment_id) == 1
                    )
                    backend.publish_output(attachment.attachment_id, "e1", b"hello ")
                    backend.publish_output(attachment.attachment_id, "e1", b"world")
                    raw_first = await asyncio.wait_for(ws.recv(), timeout=10)
                    assert isinstance(raw_first, bytes)
                    first = terminal_wire.decode_output(raw_first)
                    raw_second = await asyncio.wait_for(ws.recv(), timeout=10)
                    assert isinstance(raw_second, bytes)
                    second = terminal_wire.decode_output(raw_second)
                    assert (first.epoch, first.offset, first.data) == ("e1", 0, b"hello ")
                    assert (second.epoch, second.offset, second.data) == ("e1", 6, b"world")
                    await ws.send(json.dumps({"kind": "bye"}))
            await _wait_for(lambda: backend.rings.viewer_count(attachment.attachment_id) == 0)
            assert len(state.sockets) == 0
        finally:
            backend.close()

    asyncio.run(scenario())


def test_slow_socket_overflow_disconnects(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setattr(app_module, "TERMINAL_PUSH_POLL_S", 3600.0)

    async def scenario() -> None:
        state, backend, _ = _seed(tmp_path)
        try:
            attachment, _ = backend.create_attachment(
                "home-mac", "term-2", "shell", "acct-1", "dev-1", takeover=False
            )
            lease = backend.acquire_control(attachment.attachment_id, "acct-1", "dev-1")
            ticket = _live_ticket(state, lease.lease_id)
            with LiveGateway(create_app(state)) as live:
                url = (
                    f"ws://127.0.0.1:{live.port}/v1/terminal/{lease.lease_id}"
                    f"?ticket={ticket}&epoch=e1&offset=0"
                )
                async with ws_client.connect(url) as ws:
                    await _wait_for(
                        lambda: backend.rings.viewer_count(attachment.attachment_id) == 1
                    )
                    # No polls can drain (3600s): 1.2 MiB overflows the 1 MiB
                    # viewer queue and latches; nothing is read meanwhile.
                    backend.publish_output(attachment.attachment_id, "e1", b"a" * 600_000)
                    backend.publish_output(attachment.attachment_id, "e1", b"b" * 600_000)
                    await ws.send(json.dumps({"kind": "ack"}))
                    doc = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
                    assert doc == {"kind": "reset", "detail": "overflow"}
                    with pytest.raises(ConnectionClosed) as exc:
                        await asyncio.wait_for(ws.recv(), timeout=10)
                    assert exc.value.code == 1008
            await _wait_for(lambda: backend.rings.viewer_count(attachment.attachment_id) == 0)
            assert len(state.sockets) == 0
        finally:
            backend.close()

    asyncio.run(scenario())


def test_tunnel_loopback_output_reaches_viewer(tmp_path: Path) -> None:
    async def scenario() -> None:
        state, backend, _ = _seed(tmp_path)
        try:
            attachment, _ = backend.create_attachment(
                "home-mac", "term-3", "shell", "acct-1", "dev-1", takeover=False
            )
            lease = backend.acquire_control(attachment.attachment_id, "acct-1", "dev-1")
            ticket = _live_ticket(state, lease.lease_id)
            host_token = state.hosts.enroll("home-mac", "Home Mac", "acct-1")
            with LiveGateway(create_app(state)) as live:
                viewer_url = (
                    f"ws://127.0.0.1:{live.port}/v1/terminal/{lease.lease_id}"
                    f"?ticket={ticket}&epoch=e1&offset=0"
                )
                tunnel_url = f"ws://127.0.0.1:{live.port}/v1/connectors?token={host_token}"
                async with ws_client.connect(viewer_url) as viewer:
                    await _wait_for(
                        lambda: backend.rings.viewer_count(attachment.attachment_id) == 1
                    )
                    async with ws_client.connect(tunnel_url) as tunnel:
                        hello = json.loads(await asyncio.wait_for(tunnel.recv(), timeout=10))
                        assert hello == {
                            "kind": "hello",
                            "host_id": "home-mac",
                            "snapshot_chunks": True,
                            "epochs": [
                                {
                                    "terminal_id": "term-3",
                                    "epoch": "e1",
                                    "kind": "shell",
                                    "mode": "control",
                                }
                            ],
                        }
                        await tunnel.send(
                            json.dumps(
                                {
                                    "kind": "terminal.output",
                                    "host_id": "home-mac",
                                    "terminal_id": "term-3",
                                    "epoch": "e1",
                                    "data_b64": base64.b64encode(b"$ ").decode(),
                                }
                            )
                        )
                        raw_chunk = await asyncio.wait_for(viewer.recv(), timeout=10)
                        assert isinstance(raw_chunk, bytes)
                        chunk = terminal_wire.decode_output(raw_chunk)
                        assert (chunk.epoch, chunk.offset, chunk.data) == ("e1", 0, b"$ ")
                        # Unknown terminal: frame dropped, tunnel survives.
                        await tunnel.send(
                            json.dumps(
                                {
                                    "kind": "terminal.output",
                                    "host_id": "home-mac",
                                    "terminal_id": "nope",
                                    "epoch": "e1",
                                    "data_b64": base64.b64encode(b"junk").decode(),
                                }
                            )
                        )
                        await tunnel.send(json.dumps({"kind": "ping"}))
                        assert json.loads(await asyncio.wait_for(tunnel.recv(), timeout=10)) == {
                            "kind": "pong"
                        }
                        # Oversized tunnel text: fail closed on the tunnel.
                        await tunnel.send("x" * (64 * 1024 + 1))
                        with pytest.raises(ConnectionClosed) as exc:
                            await asyncio.wait_for(tunnel.recv(), timeout=10)
                        assert exc.value.code == 1008
                    await viewer.send(json.dumps({"kind": "bye"}))
            await _wait_for(lambda: backend.rings.viewer_count(attachment.attachment_id) == 0)
            assert len(state.sockets) == 0
        finally:
            backend.close()

    asyncio.run(scenario())


def test_single_tunnel_per_host(tmp_path: Path) -> None:
    async def scenario() -> None:
        state, backend, _ = _seed(tmp_path)
        try:
            host_token = state.hosts.enroll("home-mac", "Home Mac", "acct-1")
            with LiveGateway(create_app(state)) as live:
                url = f"ws://127.0.0.1:{live.port}/v1/connectors?token={host_token}"
                async with ws_client.connect(url) as first:
                    assert (
                        json.loads(await asyncio.wait_for(first.recv(), timeout=10))["kind"]
                        == "hello"
                    )
                    async with ws_client.connect(url) as second:
                        assert (
                            json.loads(await asyncio.wait_for(second.recv(), timeout=10))["kind"]
                            == "hello"
                        )
                        with pytest.raises(ConnectionClosed) as exc:
                            await asyncio.wait_for(first.recv(), timeout=10)
                        assert exc.value.code == 1008
                        assert state.sockets.host_count("home-mac") == 1
                        await second.send(json.dumps({"kind": "bye"}))
            assert state.sockets.host_count("home-mac") == 0
        finally:
            backend.close()

    asyncio.run(scenario())
