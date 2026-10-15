"""Deterministic crash simulation (drop in-memory
state, reopen the journal from disk) plus a thread-based server restart.

Honesty limits: the restart test runs uvicorn in a thread (LiveGateway
pattern) with manual object drops -- there is NO OS process boundary,
no SIGKILL, and no file-descriptor loss. Shutdown is graceful and
`close()` checkpoints the WAL first, so unclean-shutdown WAL replay is
never exercised here; only journal-file durability across reopening.

Decision table (/; each window -> outcome + justification):
  crash before dispatch   -> accepted stays accepted; recovery may
      dispatch ONCE with the SAME UUID. Justification: the dispatched
      marker is write-ahead (journaled before any wire byte), so its
      absence PROVES non-dispatch; same-UUID delivery is idempotent at
      the connector journal (dedupe by UUID + digest). TTL-expired
      accepted intents are swept to expired instead, never dispatched.
  crash after dispatch    -> unknown, reconcile only. Justification: the
      marker proves dispatch was attempted, nothing about execution; a
      resubmitted UUID dedupes to unknown and is never re-enqueued.
  crash mid fanout-publish -> no duplicate bytes presented as new.
      Justification: rings are offset-addressed ((epoch, offset) is
      stable across replays, so redelivery is detectable client-side by
      discarding offsets below next-expected); a real crash loses the
      memory-only rings, so old cursors resolve stale (offset past the
      live edge) or read zero bytes -- never resurrected content.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from relaydeck.connector.journal import ConnectorDispatch
from relaydeck.contracts.schemas import GATEWAY_PROTOCOL
from relaydeck.gateway.app import GatewayState, create_app
from relaydeck.gateway.production import JournalBackend
from relaydeck.journal import CommandJournal
from tests.support.live_server import LiveGateway

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "gateway"
NOW = 1_700_000_000_000
SECRET = b"crash-windows-test-secret-32bytes!"
CAPS = frozenset(
    {
        "sessions.read",
        "terminal.view",
        "terminal.control",
        "agents.prompt",
        "agents.start",
        "shells.create",
        "terminal.attach",
    }
)


def _rec(journal: CommandJournal, command_id: str) -> dict[str, Any]:
    """Fetch a journal record that must exist (asserts presence)."""
    rec = journal.record(command_id)
    assert rec is not None
    return rec


def _crash_reopen(path: Path, clock_now: int = NOW) -> CommandJournal:
    """Deterministic crash: the caller drops its journal object (and all
    in-memory backend state); the same disk file reopens fresh."""
    return CommandJournal(path, clock=lambda: clock_now)


def _envelope(command_id: str, now_ms: int = NOW) -> dict[str, Any]:
    expires = datetime.fromtimestamp((now_ms + 120_000) / 1000, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "protocol": GATEWAY_PROTOCOL,
        "command_id": command_id,
        "host_id": "home-mac",
        "host_epoch": "epoch-7",
        "kind": "agent.start",
        "target": {"workspace_id": "w1"},
        "payload": {"kind": "tool-a", "name": "web"},
        "expires_at": expires,
    }


def test_crash_before_dispatch_redispatches_once_same_uuid(tmp_path: Path) -> None:
    path = tmp_path / "gw.db"
    first = CommandJournal(path, clock=lambda: NOW)
    backend = JournalBackend(first, now_ms=lambda: NOW)
    try:
        backend.submit_command(
            {
                **_envelope("crash-001"),
                "principal": "acct-1",
                "device_id": "d1",
                "expires_ms": NOW + 120_000,
            }
        )
        assert _rec(first, "crash-001")["status"] == "accepted"
        assert len(backend.drain_outbox("home-mac")) == 1
        # CRASH: drop all memory (outbox, enqueue set, leases, rings).
        backend.close()
        del backend
        del first
    except Exception:
        raise
    reopened = _crash_reopen(path)
    try:
        recovered = reopened.recover(now=NOW + 1)
        assert recovered == {"unknown": [], "redispatchable": ["crash-001"]}
        backend2 = JournalBackend(reopened, now_ms=lambda: NOW + 1)
        try:
            pending = backend2.drain_outbox("home-mac")  # same UUID, once
            assert len(pending) == 1
            assert pending[0]["command_id"] == "crash-001"
            assert backend2.drain_outbox("home-mac") == []
            # Resubmission dedupes to the live record; never re-enqueues.
            dup = backend2.submit_command(
                {
                    **_envelope("crash-001"),
                    "principal": "acct-1",
                    "device_id": "d1",
                    "expires_ms": NOW + 120_000,
                }
            )
            assert dup["status"] == "accepted"
            assert len(backend2.drain_outbox("home-mac")) == 1
        finally:
            backend2.close()
    except Exception:
        reopened.close()
        raise


def test_crash_before_dispatch_expired_is_never_sent(tmp_path: Path) -> None:
    path = tmp_path / "gw.db"
    first = CommandJournal(path, clock=lambda: NOW)
    first.submit(
        command_id="crash-002",
        principal="acct-1",
        host_id="home-mac",
        kind="shell.create",
        target={"workspace_id": "w1"},
        payload={},
        expires_ms=NOW + 100,
        now=NOW,
    )
    first.close()
    del first
    reopened = _crash_reopen(path)
    try:
        # Past TTL at recovery: swept to expired, NOT redispatchable.
        assert reopened.recover(now=NOW + 101) == {"unknown": [], "redispatchable": []}
        assert _rec(reopened, "crash-002")["status"] == "expired"
        backend2 = JournalBackend(reopened, now_ms=lambda: NOW + 101)
        try:
            assert backend2.drain_outbox("home-mac") == []
        finally:
            backend2.close()
    except Exception:
        reopened.close()
        raise


def test_crash_after_dispatch_is_unknown_no_blind_retry(tmp_path: Path) -> None:
    path = tmp_path / "gw.db"
    first = CommandJournal(path, clock=lambda: NOW)
    backend = JournalBackend(first, now_ms=lambda: NOW)
    try:
        backend.submit_command(
            {
                **_envelope("crash-010"),
                "principal": "acct-1",
                "device_id": "d1",
                "expires_ms": NOW + 120_000,
            }
        )
        pending = backend.drain_outbox("home-mac")
        assert len(pending) == 1
        assert backend.mark_command_dispatched("crash-010", NOW)  # pump sent it
        # CRASH after upstream dispatch, before any ack.
        backend.close()
        del backend
        del first
    except Exception:
        raise
    reopened = _crash_reopen(path)
    try:
        recovered = reopened.recover(now=NOW + 1)
        assert recovered == {"unknown": ["crash-010"], "redispatchable": []}
        backend2 = JournalBackend(reopened, now_ms=lambda: NOW + 1)
        try:
            # No blind retry: nothing re-enqueued, resubmit dedupes unknown.
            assert backend2.drain_outbox("home-mac") == []
            dup = backend2.submit_command(
                {
                    **_envelope("crash-010"),
                    "principal": "acct-1",
                    "device_id": "d1",
                    "expires_ms": NOW + 120_000,
                }
            )
            assert dup["status"] == "unknown"
            assert backend2.drain_outbox("home-mac") == []
            # Explicit reconcile carries the record (authoritative result
            # may still resolve it later; resubmission never does).
            rec = reopened.record("crash-010")
            assert rec is not None and rec["status"] == "unknown"
            assert reopened.mark_result(
                "crash-010",
                "succeeded",
                result_digest="late-proof",
                result={"type": "agent_started"},
                now=NOW + 2,
            )
            assert _rec(reopened, "crash-010")["status"] == "succeeded"
        finally:
            backend2.close()
    except Exception:
        reopened.close()
        raise


def test_crash_mid_publish_redelivery_is_detectable(tmp_path: Path) -> None:
    """Ring offsets are stable across replays: a redelivered prefix
    carries its ORIGINAL (epoch, offset), so a client tracking
    next-expected discards it instead of rendering duplicates as new."""
    journal = CommandJournal(tmp_path / "gw.db", clock=lambda: NOW)
    backend = JournalBackend(journal, now_ms=lambda: NOW)
    try:
        attachment, _ = backend.create_attachment(
            "home-mac", "term-1", "shell", "acct-1", "dev-1", takeover=False
        )
        lease = backend.acquire_control(attachment.attachment_id, "acct-1", "dev-1")
        backend.publish_output(attachment.attachment_id, "e1", b"hello")
        backend.publish_output(attachment.attachment_id, "e1", b" world")
        first = backend.read_stream(lease.lease_id, "e1", 0)
        assert first.status == "ok"
        assert first.chunks == ((0, b"hello"), (5, b" world"))
        assert first.next_offset == 11
        # Crash mid-publish, upstream redelivers the same prefix: the
        # replay carries identical offsets, never renumbered as new.
        replay = backend.read_stream(lease.lease_id, "e1", 0)
        assert replay.chunks == first.chunks
        assert replay.next_offset == 11
        next_expected = 11  # client-side continuity cursor
        assert all(off < next_expected for off, _ in replay.chunks)  # dup: discard
        live = backend.read_stream(lease.lease_id, "e1", 11)
        assert live.status == "ok" and live.chunks == ()  # nothing new
    finally:
        backend.close()


def test_crash_loses_rings_old_cursor_never_new_bytes(tmp_path: Path) -> None:
    """A real crash loses the memory-only rings and leases. Reattach
    starts a fresh stream: the pre-crash cursor resolves stale (past
    the live edge) or reads zero bytes -- pre-crash bytes are never
    presented as new content."""
    journal = CommandJournal(tmp_path / "gw.db", clock=lambda: NOW)
    backend = JournalBackend(journal, now_ms=lambda: NOW)
    attachment, _ = backend.create_attachment(
        "home-mac", "term-1", "shell", "acct-1", "dev-1", takeover=False
    )
    backend.acquire_control(attachment.attachment_id, "acct-1", "dev-1")
    backend.publish_output(attachment.attachment_id, "e1", b"pre-crash-bytes")
    old_cursor = ("e1", 15)
    # CRASH: drop every in-memory store; only the journal file survives.
    backend.close()
    del backend
    reopened = _crash_reopen(tmp_path / "gw.db")
    backend2 = JournalBackend(reopened, now_ms=lambda: NOW + 1)
    try:
        attachment2, _ = backend2.create_attachment(
            "home-mac", "term-1", "shell", "acct-1", "dev-1", takeover=False
        )
        lease2 = backend2.acquire_control(attachment2.attachment_id, "acct-1", "dev-1")
        stale = backend2.read_stream(lease2.lease_id, old_cursor[0], old_cursor[1])
        assert stale.status == "stale_offset"
        assert stale.chunks == ()  # never any bytes on a stale read
        empty = backend2.read_stream(lease2.lease_id, attachment2.epoch, 0)
        assert empty.chunks == ()  # fresh stream, zero resurrected bytes
    finally:
        backend2.close()


def test_connector_crash_between_creation_steps(tmp_path: Path) -> None:
    """agent.start crashes between tab.create and agent.start: unknown
    with journaled partial identities; reconcile reuses them and the
    redelivered UUID never re-executes."""

    class _Crashy:
        async def workspace_create(
            self, *, cwd: str | None = None, label: str | None = None
        ) -> dict[str, Any]:
            raise AssertionError("Unexpected workspace creation")

        async def tab_rename(self, tab_id: str, label: str) -> dict[str, Any]:
            raise AssertionError("Unexpected tab rename")

        def __init__(self) -> None:
            self.calls = 0

        async def agent_prompt(self, target: str, text: str) -> dict[str, Any]:
            raise AssertionError("not used")

        async def tab_create(
            self, workspace_id: str, *, label: str | None = None
        ) -> dict[str, Any]:
            self.calls += 1
            return {
                "type": "tab_created",
                "tab": {"tab_id": "tab-7"},
                "root_pane": {"pane_id": "pane-7"},
            }

        async def agent_start(
            self,
            name: str,
            kind: str,
            pane_id: str,
            args: list[str] | None = None,
            timeout_ms: int | None = None,
        ) -> dict[str, Any]:
            self.calls += 1
            raise OSError("daemon died mid-chain")

        async def pane_close(self, pane_id: str) -> dict[str, Any]:
            raise AssertionError("not used")

    async def scenario() -> None:
        from relaydeck.journal import digest_for

        journal = CommandJournal(tmp_path / "c.db", clock=lambda: NOW)
        try:
            adapter = _Crashy()
            dispatcher = ConnectorDispatch(journal, adapter, host_id="home-mac", clock=lambda: NOW)
            target = {"workspace_id": "w1"}
            payload = {"kind": "tool-a", "name": "web"}
            msg = {
                "kind": "command.dispatch",
                "command_id": "chain-001",
                "principal": "acct-1",
                "host_id": "home-mac",
                "gateway_kind": "agent.start",
                "target": target,
                "payload": payload,
                "digest": digest_for("home-mac", "agent.start", target, payload),
                "generation": 0,
                "expires_ms": 0,
            }
            out = await dispatcher.dispatch(msg)
            assert out["status"] == "unknown"
            assert adapter.calls == 2  # tab.create ran, agent.start died
            # Reconcile carries the partial identities (reuse,
            # never duplicate creation).
            rec = dispatcher.reconcile("chain-001")
            assert rec is not None and rec["status"] == "unknown"
            assert rec["progress"] == {"workspace_id": "w1", "tab_id": "tab-7", "pane_id": "pane-7"}
            # Redelivery of the same UUID: recorded unknown, zero new calls.
            again = await dispatcher.dispatch(dict(msg))
            assert again["status"] == "unknown"
            assert adapter.calls == 2
        finally:
            journal.close()

    asyncio.run(scenario())


@contextlib.contextmanager
def _restart(app: Any, port: int) -> Any:
    last: Exception | None = None
    for _ in range(10):
        live = LiveGateway(app, port=port)
        try:
            live.__enter__()
            break
        except (OSError, RuntimeError) as exc:  # bind race
            last = exc
            time.sleep(0.2)
    else:
        raise RuntimeError(f"could not rebind {port}") from last
    try:
        yield live
    finally:
        live.__exit__(None, None, None)


def test_server_restart_journal_survives(tmp_path: Path) -> None:
    """LiveGateway fixed-port restart (test_reconnect pattern): the
    journal file outlives the server thread; accepted stays accepted,
    dispatched-during-life becomes unknown, and resubmits dedupe with
    no blind retry. Thread-based: no OS process boundary (see module
    docstring for the stated limits)."""

    async def scenario() -> None:
        snapshot = json.loads((FIXTURES / "snapshot.json").read_text())
        path = tmp_path / "gw.db"
        journal1 = CommandJournal(path, clock=lambda: NOW)
        backend1 = JournalBackend(journal1, now_ms=lambda: NOW, snapshots={"home-mac": snapshot})
        state1 = GatewayState(backend=backend1, cursor_secret=SECRET, now_ms=lambda: NOW)
        access1, _ = state1.sessions.create("acct-1", CAPS, frozenset({"home-mac"}), NOW)
        headers1 = {"Authorization": f"Bearer {access1}"}
        with LiveGateway(create_app(state1)) as probe:
            port = probe.port
        with _restart(create_app(state1), port) as live:
            base = live.base_url
            async with httpx.AsyncClient(base_url=base, timeout=10.0) as http:
                r = await http.post(
                    "/v1/hosts/home-mac/commands", headers=headers1, json=_envelope("boot-0001")
                )
                assert r.status_code == 202, r.text
                r = await http.post(
                    "/v1/hosts/home-mac/commands", headers=headers1, json=_envelope("boot-0002")
                )
                assert r.status_code == 202, r.text
                # boot-0002 was pumped down the tunnel before the crash.
                assert journal1.mark_dispatched("boot-0002", now=NOW)
        # Server shutdown: drop everything except the journal file.
        backend1.close()
        del backend1
        del state1
        del journal1

        journal2 = _crash_reopen(path)
        backend2 = JournalBackend(journal2, now_ms=lambda: NOW, snapshots={"home-mac": snapshot})
        state2 = GatewayState(backend=backend2, cursor_secret=SECRET, now_ms=lambda: NOW)
        access2, _ = state2.sessions.create("acct-1", CAPS, frozenset({"home-mac"}), NOW)
        headers2 = {"Authorization": f"Bearer {access2}"}
        try:
            with _restart(create_app(state2), port) as live2:
                base2 = live2.base_url
                async with httpx.AsyncClient(base_url=base2, timeout=10.0) as http:
                    r = await http.get("/v1/hosts/home-mac/commands/boot-0001", headers=headers2)
                    assert r.status_code == 200
                    assert r.json()["status"] == "accepted"
                    r = await http.get("/v1/hosts/home-mac/commands/boot-0002", headers=headers2)
                    assert r.status_code == 200
                    assert r.json()["status"] == "unknown"
                    # Resubmits dedupe; recovery enqueued boot-0001 exactly once.
                    r = await http.post(
                        "/v1/hosts/home-mac/commands", headers=headers2, json=_envelope("boot-0001")
                    )
                    assert r.status_code == 202
                    assert r.json()["status"] == "accepted"
                    r = await http.post(
                        "/v1/hosts/home-mac/commands", headers=headers2, json=_envelope("boot-0002")
                    )
                    assert r.status_code == 202
                    assert r.json()["status"] == "unknown"
                    pending = backend2.drain_outbox("home-mac")
                    assert [m["command_id"] for m in pending] == ["boot-0001"]
        finally:
            backend2.close()

    asyncio.run(scenario())
