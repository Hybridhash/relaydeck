"""authenticated connector metadata -> held transition -> outbox -> provider double."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from relaydeck.gateway.app import create_app
from relaydeck.gateway.notification_transitions import (
    HOLD_MS,
    MAX_TRACKED_AGENTS,
    NotificationTransitions,
)
from relaydeck.gateway.production import JournalBackend
from relaydeck.gateway.push import HINT_KEYS, ProviderOutcome, PushStore, StubProvider, pump_outbox
from relaydeck.journal import CommandJournal
from tests.security.helpers import ALL_CAPS, NOW, make_state
from tests.support.asgi_socket import AsgiSocket
from tests.support.sync.transport_fixtures import _wait


def source_snapshot(host: str = "home-mac") -> dict[str, Any]:
    snapshot: dict[str, Any] = json.loads(Path("tests/fixtures/gateway/snapshot.json").read_text())
    snapshot["host"]["id"] = host
    for group in ("workspaces", "tabs", "panes", "agents"):
        for row in snapshot[group]:
            row["host_id"] = host
    return snapshot


@pytest.mark.parametrize("transition", ["blocked", "done"])
def test_connector_transition_authorizes_recipients_and_worker_sends_only_generic_hint(
    transition: str,
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        state, _ = make_state()
        clock = [NOW]
        state.now_ms = lambda: clock[0]
        backend = JournalBackend(
            CommandJournal(tmp_path / "journal.db"), now_ms=state.now_ms, record_inputs=True
        )
        state.backend = backend
        state.push.close()
        state.push = PushStore(clock=state.now_ms)
        provider = StubProvider()
        state.push_provider = provider
        token = state.hosts.enroll("home-mac", account="acct-1")
        for account, device in [
            ("acct-1", "active"),
            ("acct-1", "revoked"),
            ("acct-2", "foreign"),
            ("acct-1", "no-auth"),
        ]:
            if device != "no-auth":
                state.sessions.create(account, ALL_CAPS, {"home-mac"}, NOW, device)
            state.push.register_device(account, device, "synthetic-" + device, "android", NOW)
        # A missed subscription cleanup must not weaken the live authentication boundary.
        state.sessions.revoke_device("acct-1", "revoked")
        snapshot = source_snapshot()
        snapshot["agents"][0].update(
            status="working", title="PRIVATE_TITLE", revision=1, state_change_seq=1
        )
        epoch = snapshot["host"]["epoch"]
        agent = snapshot["agents"][0]
        app = create_app(state)
        async with app.router.lifespan_context(app):
            socket = await AsgiSocket(app, "/v1/connectors", token=token).open()
            await socket.recv_text()  # hello
            try:
                await socket.send_text(
                    json.dumps({"kind": "snapshot", "host_id": "home-mac", "snapshot": snapshot})
                )
                await _wait(lambda: bool(state.notifications.statuses))
                assert not state.notifications.pending and state.push.pending_count() == 0
                event = {
                    "event_id": "synthetic-source-transition",
                    "host_epoch": epoch,
                    "type": "pane.upserted",
                    "resource_id": agent["pane_id"],
                    "payload": {"agent_status": transition},
                }
                # Real LiveSource refreshes the snapshot before sending this event.
                snapshot["agents"][0]["status"] = transition
                await socket.send_text(
                    json.dumps({"kind": "snapshot", "host_id": "home-mac", "snapshot": snapshot})
                )
                for _ in range(3):
                    await socket.send_text(
                        json.dumps({"kind": "event", "host_id": "home-mac", "event": event})
                    )
                await _wait(lambda: len(state.notifications.pending) == 1)
                assert len(backend.event_logs["home-mac"]) == 1
                await asyncio.sleep(0.02)
                assert provider.sends == []
                clock[0] += HOLD_MS + 1
                await _wait(lambda: len(provider.sends) == 1)
                delivery_token, payload = provider.sends[0]
                assert delivery_token == "synthetic-active"
                assert payload.keys() == HINT_KEYS
                assert payload["category"] == (
                    "attention" if transition == "blocked" else "completion"
                )
                assert not any(
                    value in json.dumps(payload)
                    for value in (
                        "PRIVATE_TITLE",
                        agent["terminal_id"],
                        "home-mac",
                        "acct-1",
                        "synthetic-source-transition",
                        "synthetic-active",
                    )
                )
                await socket.send_text(
                    json.dumps({"kind": "event", "host_id": "home-mac", "event": event})
                )
                await asyncio.sleep(0.3)
                assert len(provider.sends) == 1
                assert (
                    backend.input_logs == {}
                    and backend.journal.status("synthetic-source-transition") is None
                )
            finally:
                await socket.close()
                assert not state.notifications.pending
        state.push.close()
        backend.close()

    asyncio.run(scenario())


def test_first_sync_flaps_epoch_removal_and_bound_fail_closed() -> None:
    notifications = NotificationTransitions()
    snapshot = source_snapshot()
    snapshot["agents"][0]["status"] = "blocked"
    notifications.snapshot("acct-1", "home-mac", snapshot, NOW)
    assert notifications.pending == {}
    snapshot["agents"][0]["status"] = "working"
    notifications.snapshot("acct-1", "home-mac", snapshot, NOW)
    snapshot["agents"][0]["status"] = "done"
    notifications.snapshot("acct-1", "home-mac", snapshot, NOW + 1)
    emitted: list[Any] = []
    notifications.flush(NOW + HOLD_MS, lambda *args: emitted.append(args))
    assert emitted == []
    snapshot["agents"][0]["status"] = "working"
    notifications.snapshot("acct-1", "home-mac", snapshot, NOW + 2)
    notifications.flush(NOW + HOLD_MS + 2, lambda *args: emitted.append(args))
    assert emitted == []
    snapshot["agents"][0]["status"] = "done"
    snapshot["host"]["epoch"] = "new-source-epoch"
    notifications.snapshot("acct-1", "home-mac", snapshot, NOW + 3)
    assert notifications.pending == {}
    original = snapshot["agents"][0]
    snapshot["agents"] = [
        dict(original, terminal_id=f"agent-{i}", status="working")
        for i in range(MAX_TRACKED_AGENTS + 100)
    ]
    notifications.snapshot("acct-1", "home-mac", snapshot, NOW + 4)
    assert len(notifications.statuses) <= MAX_TRACKED_AGENTS
    snapshot["agents"] = []
    notifications.snapshot("acct-1", "home-mac", snapshot, NOW + 5)
    assert notifications.statuses == {} and notifications.pending == {}


@pytest.mark.parametrize("revoke", ["device", "host"])
def test_queued_delivery_rechecks_authority_after_enqueue(revoke: str) -> None:
    state, _ = make_state()
    state.hosts.enroll("home-mac", account="acct-1")
    state.sessions.create("acct-1", ALL_CAPS, {"home-mac"}, NOW, "d1")
    state.push.register_device("acct-1", "d1", "synthetic-token", "android", NOW)
    row = state.push.notify(
        account="acct-1",
        account_hosts={"home-mac"},
        host_id="home-mac",
        session_id="synthetic-agent",
        event_id="synthetic-event",
        category="completion",
        now=NOW,
    )[0]
    assert state.push_delivery_allowed(row)
    if revoke == "device":
        state.sessions.revoke_device("acct-1", "d1")
    else:
        state.hosts.revoke("home-mac")
    from relaydeck.gateway.push import pump_outbox

    provider = StubProvider()
    result = pump_outbox(state.push, provider, NOW, authorized=state.push_delivery_allowed)
    assert result["dropped"] == 1 and provider.sends == []
    state.push.close()


@pytest.mark.parametrize(
    "invalid", ["epoch", "host", "payload", "revoked-token", "foreign-account"]
)
def test_untrusted_connector_messages_cannot_enqueue(invalid: str, tmp_path: Path) -> None:
    async def scenario() -> None:
        state, _ = make_state()
        state.backend = JournalBackend(CommandJournal(tmp_path / "journal.db"), now_ms=state.now_ms)
        token = state.hosts.enroll("home-mac", account="acct-1")
        snapshot = source_snapshot()
        socket = await AsgiSocket(create_app(state), "/v1/connectors", token=token).open()
        await socket.recv_text()
        await socket.send_text(
            json.dumps({"kind": "snapshot", "host_id": "home-mac", "snapshot": snapshot})
        )
        await _wait(lambda: bool(state.notifications.statuses))
        event = {
            "event_id": "same-event",
            "host_epoch": snapshot["host"]["epoch"],
            "type": "pane.upserted",
            "resource_id": "w1:p1",
            "payload": {"agent_status": "blocked"},
        }
        if invalid == "epoch":
            event["host_epoch"] = "wrong-epoch"
        elif invalid == "payload":
            event["payload"] = {"agent_status": ["blocked"]}
        elif invalid in {"revoked-token", "foreign-account"}:
            state.hosts.enroll(
                "home-mac", account="acct-2" if invalid == "foreign-account" else "acct-1"
            )
        await socket.send_text(
            json.dumps(
                {
                    "kind": "event",
                    "host_id": "other-host" if invalid == "host" else "home-mac",
                    "event": event,
                }
            )
        )
        message = await socket.receive()
        assert message["type"] == "websocket.close"
        assert not state.notifications.pending and state.push.pending_count() == 0
        await socket.close()
        state.push.close()
        state.backend.close()

    asyncio.run(scenario())


def test_source_host_scoped_dedupe_durable_retry_expiry_and_replay(tmp_path: Path) -> None:
    async def scenario() -> None:
        state, _ = make_state()
        clock = [NOW]
        state.now_ms = lambda: clock[0]
        state.backend = JournalBackend(CommandJournal(tmp_path / "journal.db"), now_ms=state.now_ms)
        state.push.close()
        path = str(tmp_path / "push.db")
        state.push = PushStore(path, clock=state.now_ms)
        state.sessions.create("acct-1", ALL_CAPS, {"home-mac", "other-mac"}, NOW, "d1")
        state.push.register_device("acct-1", "d1", "synthetic-token", "android", NOW)
        sockets = []
        for host in ("home-mac", "other-mac"):
            token = state.hosts.enroll(host, account="acct-1")
            socket = await AsgiSocket(create_app(state), "/v1/connectors", token=token).open()
            sockets.append(socket)
            await socket.recv_text()
            snapshot = source_snapshot(host)
            await socket.send_text(
                json.dumps({"kind": "snapshot", "host_id": host, "snapshot": snapshot})
            )
            await _wait(lambda host=host: (host, "term-1") in state.notifications.statuses)
            event = {
                "event_id": "same-event",
                "host_epoch": snapshot["host"]["epoch"],
                "type": "pane.upserted",
                "resource_id": "w1:p1",
                "payload": {"agent_status": "done"},
            }
            await socket.send_text(json.dumps({"kind": "event", "host_id": host, "event": event}))
            await _wait(lambda host=host: (host, "term-1") in state.notifications.pending)
            await socket.send_text(json.dumps({"kind": "event", "host_id": host, "event": event}))
        clock[0] += HOLD_MS + 1
        state.notifications.flush(clock[0], state.emit_notification)
        assert state.push.pending_count() == 2
        state.push.close()
        state.push = PushStore(
            path, clock=state.now_ms
        )  # Durable outbox survives owner replacement.
        retry = StubProvider(default=ProviderOutcome.RETRYABLE)
        assert (
            pump_outbox(state.push, retry, clock[0], authorized=state.push_delivery_allowed)[
                "retried"
            ]
            == 2
        )
        rows = state.push.due(clock[0] + 60_000)
        assert len(rows) == 2
        clock[0] += 60_000
        provider = StubProvider()
        assert (
            pump_outbox(state.push, provider, clock[0], authorized=state.push_delivery_allowed)[
                "sent"
            ]
            == 2
        )
        assert (
            pump_outbox(state.push, provider, clock[0], authorized=state.push_delivery_allowed)[
                "sent"
            ]
            == 0
        )
        assert len(provider.sends) == 2
        assert provider.sends[0][1]["event_ref"] != provider.sends[1][1]["event_ref"]
        for socket in sockets:
            await socket.close()
        # Existing sent IDs retain dedupe after restart; expired pending hints never dispatch.
        expired = state.push.notify(
            account="acct-1",
            account_hosts={"home-mac"},
            host_id="home-mac",
            session_id="term-1",
            event_id="ttl-event",
            category="attention",
            now=clock[0],
            ttl_ms=1,
        )
        assert expired
        assert (
            pump_outbox(state.push, provider, clock[0] + 2, authorized=state.push_delivery_allowed)[
                "dropped"
            ]
            == 1
        )
        assert len(provider.sends) == 2
        state.push.close()
        state.backend.close()

    asyncio.run(scenario())


def test_enqueue_failure_is_bounded_retained_and_redacted() -> None:
    notifications = NotificationTransitions()
    snapshot = source_snapshot()
    notifications.snapshot("acct-1", "home-mac", snapshot, NOW)
    event = {
        "event_id": "event",
        "host_epoch": snapshot["host"]["epoch"],
        "type": "pane.upserted",
        "resource_id": "w1:p1",
        "payload": {"agent_status": "blocked"},
    }
    notifications.event("acct-1", "home-mac", event, NOW)
    calls = []

    def failed(*args: Any) -> None:
        calls.append(args)
        raise OSError("PRIVATE_STORAGE_DETAIL")

    for elapsed in (HOLD_MS, HOLD_MS + 250, HOLD_MS + 750, HOLD_MS + 10000):
        notifications.flush(NOW + elapsed, failed)
    assert len(calls) == 3 and notifications.pending == {}
    assert notifications.error == "notification_enqueue_exhausted"


def test_provider_started_before_revoke_cannot_be_recalled() -> None:
    state, _ = make_state()
    state.hosts.enroll("home-mac", account="acct-1")
    state.sessions.create("acct-1", ALL_CAPS, {"home-mac"}, NOW, "d1")
    state.push.register_device("acct-1", "d1", "synthetic-token", "android", NOW)
    state.push.notify(
        account="acct-1",
        account_hosts={"home-mac"},
        host_id="home-mac",
        session_id="term-1",
        event_id="race",
        category="attention",
        now=NOW,
    )

    class RacingProvider(StubProvider):
        def send(self, token: str, payload: dict[str, str]) -> ProviderOutcome:
            state.sessions.revoke_device("acct-1", "d1")
            state.push.revoke_device("acct-1", "d1", NOW)
            return super().send(token, payload)

    provider = RacingProvider()
    assert (
        pump_outbox(state.push, provider, NOW, authorized=state.push_delivery_allowed)["dropped"]
        == 1
    )
    assert len(provider.sends) == 1  # Provider acceptance is irreversible; database is revoked.
    state.push.close()
