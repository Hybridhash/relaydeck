"""deployment recovery rehearsals (synthetic data only).

Backup/restore, revocation survival, restart redispatch, lost-device
revoke, and terminal offset discipline -- all against the REAL journal
(`relaydeck.journal`), gateway (`JournalBackend`, `GatewayState`,
`SessionStore`, `PushStore`), and connector dispatch
(`ConnectorDispatch`) code paths. No live infra, no real credentials:
every secret here is a synthetic test value, every database a tmp file.

Invariants under rehearsal:
  * Restoring a backup cannot resurrect revoked credentials.
  * Recovery never replays journaled commands or terminal bytes.
  * Lost-device revoke cuts push fanout and sessions together.
  * Gateway restart preserves journal at-least-once without duplication.
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from relaydeck.connector.journal import ConnectorDispatch
from relaydeck.gateway.app import GatewayState, create_app, revoke_account_and_close
from relaydeck.gateway.backend import BackendError
from relaydeck.gateway.production import JournalBackend
from relaydeck.gateway.push import PushStore
from relaydeck.gateway.ring import STATUS_OK, TerminalRing
from relaydeck.journal import CommandJournal

NOW = 1_700_000_000_000
SECRET = b"recovery-rehearsal-secret-32bytes!"
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


def _submit(
    backend: JournalBackend,
    command_id: str,
    host_id: str = "home-mac",
    kind: str = "shell.create",
) -> dict[str, Any]:
    return backend.submit_command(
        {
            "command_id": command_id,
            "principal": "acct-1",
            "device_id": "web-1",
            "host_id": host_id,
            "host_epoch": "epoch-7",
            "kind": kind,
            "target": {"workspace_id": "w1"},
            "payload": {"label": "rehearsal-shell"},
            "expires_ms": NOW + 120_000,
        }
    )


def _backup_live(path: Path, dest: Path) -> None:
    """Checkpoint-then-copy backup of a live sqlite store.

    The owner closes cleanly (close() runs wal_checkpoint(TRUNCATE)),
    so the single main file is the whole backup; sidecars are copied
    when present for crash-mid-write backups.
    """
    shutil.copy2(path, dest)
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(path) + suffix)
        if sidecar.exists():
            shutil.copy2(sidecar, Path(str(dest) + suffix))


class _StubHerdr:
    async def workspace_create(
        self, *, cwd: str | None = None, label: str | None = None
    ) -> dict[str, Any]:
        raise AssertionError("Unexpected workspace creation")

    async def tab_rename(self, tab_id: str, label: str) -> dict[str, Any]:
        raise AssertionError("Unexpected tab rename")

    """Scripted Herdr surface (DispatchAdapter shape, synthetic replies)."""

    async def agent_prompt(self, target: str, text: str) -> dict[str, Any]:
        return {"type": "agent_prompted", "target": target, "text": text}

    async def tab_create(self, workspace_id: str, *, label: str | None = None) -> dict[str, Any]:
        return {
            "tab": {"tab_id": f"tab-{workspace_id}"},
            "root_pane": {"pane_id": f"pane-{workspace_id}"},
            "label": label or "",
        }

    async def agent_start(
        self,
        name: str,
        kind: str,
        pane_id: str,
        args: list[str] | None = None,
        timeout_ms: int | None = None,
    ) -> dict[str, Any]:
        return {"type": "agent_started", "name": name, "kind": kind, "pane": pane_id}

    async def pane_close(self, pane_id: str) -> dict[str, Any]:
        return {"type": "pane_closed", "pane": pane_id}


def test_post_revoke_backup_restores_revoked(tmp_path: Path) -> None:
    """Revoke -> snapshot -> restore: the device stays revoked."""
    store = PushStore(tmp_path / "push.db")
    store.register_device("acct-1", "droid-1", "synthetic-fcm-token-1", "android", NOW)
    fresh = store.notify(
        account="acct-1",
        account_hosts=frozenset({"home-mac"}),
        host_id="home-mac",
        session_id="sess-1",
        event_id="ev-1",
        category="attention",
        now=NOW,
    )
    assert len(fresh) == 1
    link_token = str(fresh[0]["link_token"])
    notification_id = str(fresh[0]["notification_id"])
    assert store.revoke_device("acct-1", "droid-1", NOW) is True
    store.close()

    _backup_live(tmp_path / "push.db", tmp_path / "push.snap.db")
    shutil.copy2(tmp_path / "push.snap.db", tmp_path / "push.restored.db")
    restored = PushStore(tmp_path / "push.restored.db")
    try:
        assert restored.device_active("acct-1", "droid-1") is False
        assert restored.subscription_count("acct-1") == 0
        assert restored.resolve_link(link_token, "acct-1", NOW) is None
        assert restored.resolve_hint("acct-1", notification_id, NOW) is None
        assert (
            restored.notify(
                account="acct-1",
                account_hosts=frozenset({"home-mac"}),
                host_id="home-mac",
                session_id="sess-1",
                event_id="ev-2",
                category="attention",
                now=NOW,
            )
            == []
        )
    finally:
        restored.close()


def test_revocation_log_reapplied_after_stale_restore(tmp_path: Path) -> None:
    """A pre-revoke backup resurrects the row (sqlite truth); the drill
    re-applies revocations newer than the backup, and only then is the
    device dead again."""
    store = PushStore(tmp_path / "push.db")
    store.register_device("acct-1", "droid-1", "synthetic-fcm-token-2", "android", NOW)
    store.close()
    _backup_live(tmp_path / "push.db", tmp_path / "push.stale.db")

    live = PushStore(tmp_path / "push.db")
    assert live.revoke_device("acct-1", "droid-1", NOW) is True
    revocation_log = [("acct-1", "droid-1", NOW)]
    live.close()

    shutil.copy2(tmp_path / "push.stale.db", tmp_path / "push.db")
    revived = PushStore(tmp_path / "push.db")
    try:
        # The stale backup really does resurrect, so a restore must replay the revocation log.
        assert revived.device_active("acct-1", "droid-1") is True
        for account, device_id, at in revocation_log:
            assert revived.revoke_device(account, device_id, at) is True
        assert revived.device_active("acct-1", "droid-1") is False
        assert (
            revived.notify(
                account="acct-1",
                account_hosts=frozenset({"home-mac"}),
                host_id="home-mac",
                session_id="sess-9",
                event_id="ev-9",
                category="attention",
                now=NOW,
            )
            == []
        )
    finally:
        revived.close()


def test_lost_device_revoke_cuts_push_and_sessions(tmp_path: Path) -> None:
    """Lost-device drill (function level): account revoke kills the
    session while push revoke kills fanout + resolution together."""
    journal = CommandJournal(tmp_path / "gw.db", clock=lambda: NOW)
    backend = JournalBackend(journal, now_ms=lambda: NOW)
    push = PushStore(tmp_path / "push.db")
    state = GatewayState(backend=backend, cursor_secret=SECRET, now_ms=lambda: NOW, push=push)
    try:
        access, _ = state.sessions.create("acct-1", ALL_CAPS, frozenset({"home-mac"}), NOW)
        push.register_device("acct-1", "droid-1", "synthetic-fcm-token-3", "android", NOW)
        fresh = push.notify(
            account="acct-1",
            account_hosts=frozenset({"home-mac"}),
            host_id="home-mac",
            session_id="sess-1",
            event_id="ev-1",
            category="attention",
            now=NOW,
        )
        assert len(fresh) == 1
        link_token = str(fresh[0]["link_token"])

        closed = asyncio.run(revoke_account_and_close(state, "acct-1"))
        assert closed == 0  # no live sockets in this rehearsal
        assert push.revoke_device("acct-1", "droid-1", NOW) is True

        assert state.sessions.lookup(access, NOW) is None
        assert push.device_active("acct-1", "droid-1") is False
        assert push.resolve_link(link_token, "acct-1", NOW) is None
        assert (
            push.notify(
                account="acct-1",
                account_hosts=frozenset({"home-mac"}),
                host_id="home-mac",
                session_id="sess-1",
                event_id="ev-2",
                category="attention",
                now=NOW,
            )
            == []
        )
    finally:
        push.close()
        backend.close()


def test_lost_device_drill_over_http(tmp_path: Path) -> None:
    """Lost-device drill over the REAL routes: a surviving session
    DELETEs the lost device; its link then resolves 404 and fanout
    stops."""
    journal = CommandJournal(tmp_path / "gw.db", clock=lambda: NOW)
    backend = JournalBackend(journal, now_ms=lambda: NOW)
    push = PushStore(tmp_path / "push.db")
    state = GatewayState(backend=backend, cursor_secret=SECRET, now_ms=lambda: NOW, push=push)
    client = TestClient(create_app(state))
    try:
        access, _ = state.sessions.create(
            "acct-1", ALL_CAPS, frozenset({"home-mac"}), NOW, device_id="web-1"
        )
        headers = {"Authorization": f"Bearer {access}"}
        created = client.post(
            "/v1/devices",
            headers=headers,
            json={
                "device_id": "droid-1",
                "token": "synthetic-fcm-token-4",
                "platform": "android",
            },
        )
        assert created.status_code == 201, created.text
        fresh = push.notify(
            account="acct-1",
            account_hosts=frozenset({"home-mac"}),
            host_id="home-mac",
            session_id="sess-1",
            event_id="ev-1",
            category="attention",
            now=NOW,
        )
        link_token = str(fresh[0]["link_token"])
        alive = client.post("/v1/deep-links/resolve", headers=headers, json={"token": link_token})
        assert alive.status_code == 200, alive.text

        deleted = client.delete("/v1/devices/droid-1", headers=headers)
        assert deleted.status_code == 200, deleted.text

        dead = client.post("/v1/deep-links/resolve", headers=headers, json={"token": link_token})
        assert dead.status_code == 404, dead.text
        assert (
            push.notify(
                account="acct-1",
                account_hosts=frozenset({"home-mac"}),
                host_id="home-mac",
                session_id="sess-1",
                event_id="ev-2",
                category="attention",
                now=NOW,
            )
            == []
        )
    finally:
        push.close()
        backend.close()


def test_restart_recovers_dispatched_to_unknown_without_redispatch(tmp_path: Path) -> None:
    """Crash between dispatch marker and outcome reconciles to unknown;
    the UUID is never re-enqueued and resubmission dedupes (drill 5)."""
    journal = CommandJournal(tmp_path / "gw.db", clock=lambda: NOW)
    backend = JournalBackend(journal, now_ms=lambda: NOW)
    first = _submit(backend, "cmd-dispatched-1")
    assert first["status"] == "accepted"
    assert backend.mark_command_dispatched("cmd-dispatched-1", NOW) is True
    assert journal.record("cmd-dispatched-1") is not None
    backend.close()  # crash: marker journaled, no outcome recorded

    journal2 = CommandJournal(tmp_path / "gw.db", clock=lambda: NOW)
    backend2 = JournalBackend(journal2, now_ms=lambda: NOW)
    try:
        record = journal2.record("cmd-dispatched-1")
        assert record is not None
        assert record["status"] == "unknown"
        assert backend2.drain_outbox("home-mac") == []
        # Resubmission of the same body dedupes to the recorded outcome.
        again = _submit(backend2, "cmd-dispatched-1")
        assert again["status"] == "unknown"
        assert backend2.drain_outbox("home-mac") == []
    finally:
        backend2.close()


def test_restart_redispatches_accepted_once_with_same_uuid(tmp_path: Path) -> None:
    """Accepted-but-undispatched intents are proven unsent (write-ahead
    marker absent) and redispatch exactly once with the SAME UUID; the
    connector executes once and dedupes the redelivery (drill 5)."""
    journal = CommandJournal(tmp_path / "gw.db", clock=lambda: NOW)
    backend = JournalBackend(journal, now_ms=lambda: NOW)
    first = _submit(backend, "cmd-accepted-1")
    assert first["status"] == "accepted"
    backend.close()  # crash before any dispatch marker

    journal2 = CommandJournal(tmp_path / "gw.db", clock=lambda: NOW)
    backend2 = JournalBackend(journal2, now_ms=lambda: NOW)
    try:
        pending = backend2.drain_outbox("home-mac")
        assert [m["command_id"] for m in pending] == ["cmd-accepted-1"]
        assert backend2.drain_outbox("home-mac") == []  # drained once
        assert backend2.mark_command_dispatched("cmd-accepted-1", NOW) is True

        connector_journal = CommandJournal(tmp_path / "connector.db", clock=lambda: NOW)
        dispatch = ConnectorDispatch(
            connector_journal, _StubHerdr(), host_id="home-mac", clock=lambda: NOW
        )
        try:
            message = dict(pending[0])
            first_result = asyncio.run(dispatch.dispatch(message))
            assert first_result["status"] == "succeeded"
            assert dispatch.herdr_calls == 1
            # Redelivered dispatch (same UUID + same body): recorded
            # outcome, zero new Herdr calls -- never executes twice.
            second_result = asyncio.run(dispatch.dispatch(dict(pending[0])))
            assert second_result["status"] == "succeeded"
            assert dispatch.herdr_calls == 1
        finally:
            connector_journal.close()
    finally:
        backend2.close()


def test_restore_preserves_frame_receipt_dedupe(tmp_path: Path) -> None:
    """Frame receipts (input-seq ACKs, not outcomes) survive restore, so
    a redelivered terminal byte frame still dedupes instead of replaying."""
    journal = CommandJournal(tmp_path / "gw.db", clock=lambda: NOW)
    assert journal.record_frame_receipt("lease-7", 3, 41, NOW) is True
    assert journal.record_frame_receipt("lease-7", 3, 41, NOW) is False
    # Edge (journal owner): a duplicate receipt swallows IntegrityError
    # without committing, leaving an open transaction that trips
    # close()'s wal_checkpoint(TRUNCATE). Any committing call clears it;
    # expire_sweep is the live-path no-op here (nothing past deadline).
    journal.expire_sweep(NOW)
    journal.close()

    _backup_live(tmp_path / "gw.db", tmp_path / "gw.snap.db")
    shutil.copy2(tmp_path / "gw.snap.db", tmp_path / "gw.restored.db")
    restored = CommandJournal(tmp_path / "gw.restored.db", clock=lambda: NOW)
    try:
        assert restored.is_frame_received("lease-7", 3, 41) is True
        assert restored.record_frame_receipt("lease-7", 3, 41, NOW) is False
        assert restored.is_frame_received("lease-7", 3, 42) is False
        restored.expire_sweep(NOW)  # duplicate-receipt edge: commit before close
    finally:
        restored.close()


def test_terminal_offsets_do_not_replay_after_restart(tmp_path: Path) -> None:
    """Rings and leases are memory-only: after a restart the old lease
    is unknown (never served bytes) and the old epoch reads stale with
    zero bytes -- no forged tail is ever presented as a live screen."""
    journal = CommandJournal(tmp_path / "gw.db", clock=lambda: NOW)
    backend = JournalBackend(journal, now_ms=lambda: NOW)
    try:
        attachment, _ = backend.create_attachment(
            "home-mac",
            "term-1",
            "shell",
            "acct-1",
            "web-1",
            takeover=False,
            owner_label="web-1",
        )
        lease = backend.issue_view(attachment.attachment_id, "acct-1", "web-1")
        start, end = backend.publish_output(
            attachment.attachment_id, attachment.epoch, b"synthetic-bytes"
        )
        assert (start, end) == (0, 15)
        live = backend.read_stream(lease.lease_id, attachment.epoch, 0)
        assert live.status == STATUS_OK
        assert b"".join(data for _, data in live.chunks) == b"synthetic-bytes"
        lease_id = lease.lease_id
        epoch = attachment.epoch
    finally:
        backend.close()

    journal2 = CommandJournal(tmp_path / "gw.db", clock=lambda: NOW)
    backend2 = JournalBackend(journal2, now_ms=lambda: NOW)
    try:
        try:
            backend2.read_stream(lease_id, epoch, 0)
        except BackendError as exc:
            assert exc.code == "lease_expired"
        else:
            raise AssertionError("restart must not serve the pre-restart lease")
        # Even at ring level, the old epoch against a fresh ring is
        # stale with no bytes -- offsets never address a new stream.
        fresh_ring = TerminalRing("epoch-after-restart", NOW)
        stale = fresh_ring.read(epoch, 0)
        assert stale.status != STATUS_OK
        assert stale.chunks == ()
    finally:
        backend2.close()


def test_host_rotation_kills_old_token(tmp_path: Path) -> None:
    """Expired-credential drill mechanics (drill 7): re-enrolling an
    existing host id rotates its token; the old token dies at once."""
    journal = CommandJournal(tmp_path / "gw.db", clock=lambda: NOW)
    backend = JournalBackend(journal, now_ms=lambda: NOW)
    state = GatewayState(backend=backend, cursor_secret=SECRET, now_ms=lambda: NOW)
    try:
        old = state.hosts.enroll("home-mac", "Home Mac", "acct-1", "", "")
        assert state.hosts.lookup(old) is not None
        new = state.hosts.enroll("home-mac", "Home Mac", "acct-1", "", "")
        assert new != old
        assert state.hosts.lookup(old) is None
        assert state.hosts.lookup(new) is not None
    finally:
        backend.close()


def test_host_registry_does_not_survive_restart(tmp_path: Path) -> None:
    """Documented boundary: host tokens live in process memory
    (HostStore), so a gateway restart wipes the registry -- revoked
    tokens stay dead, but valid hosts must re-enroll (drill 5/7)."""
    journal = CommandJournal(tmp_path / "gw.db", clock=lambda: NOW)
    backend = JournalBackend(journal, now_ms=lambda: NOW)
    state = GatewayState(backend=backend, cursor_secret=SECRET, now_ms=lambda: NOW)
    try:
        token = state.hosts.enroll("home-mac", "Home Mac", "acct-1", "", "")
        assert state.hosts.lookup(token) is not None
    finally:
        backend.close()

    journal2 = CommandJournal(tmp_path / "gw.db", clock=lambda: NOW)
    backend2 = JournalBackend(journal2, now_ms=lambda: NOW)
    revived = GatewayState(backend=backend2, cursor_secret=SECRET, now_ms=lambda: NOW)
    try:
        assert revived.hosts.lookup(token) is None
        assert revived.hosts.hosts() == ()
    finally:
        backend2.close()


def test_duplicate_frame_receipt_leaves_no_open_transaction(tmp_path: Path) -> None:
    """Duplicate frame receipts (routine on retry redelivery) must not
    leave an open write txn: it would hold the RESERVED lock against
    other writers and trip close()'s wal_checkpoint with
    OperationalError. Found by the restore rehearsal."""
    journal = CommandJournal(tmp_path / "gw.db", clock=lambda: NOW)
    assert journal.record_frame_receipt("lease-1", 3, 7, now=NOW) is True
    assert journal.record_frame_receipt("lease-1", 3, 7, now=NOW) is False  # dup
    journal.close()  # must not raise
    reopened = CommandJournal(tmp_path / "gw.db", clock=lambda: NOW)
    try:
        assert reopened.is_frame_received("lease-1", 3, 7) is True
    finally:
        reopened.close()
