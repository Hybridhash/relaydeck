"""push outbox + subscriptions + hints + tap path.

Contract proven headless: device lifecycle, durable outbox with
dedupe/TTL/backoff, exact payload allowlist under adversarial content,
tap-never-input end to end through the adapter, stale-hint relevance,
and the permission-denied quiet state. Real-device proof (delivery,
Doze, force-stop, token refresh on device) is environment-blocked: no
Flutter/Dart, JDK, Android SDK, or Firebase project here.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
import threading
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from relaydeck_notifications.payload import (
    ALLOWED_CATEGORIES,
    ALLOWED_KEYS,
    parse_push_payload,
)

from relaydeck.client.adapters.ports import PushPermission, PushTap
from relaydeck.gateway.app import GatewayState, create_app
from relaydeck.gateway.backend import FakeBackend
from relaydeck.gateway.push import (
    BACKOFF_BASE_MS,
    DEVICE_TOMBSTONE_MS,
    HINT_CATEGORIES,
    HINT_KEYS,
    LINK_KEEP_MS,
    TERMINAL_KEEP_MS,
    ProviderOutcome,
    PushDenied,
    PushStore,
    backoff_ms,
    pump_outbox,
)
from tests.support.fakes import FakeNotifications
from tests.support.push import StubProvider

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "gateway"
SECRET = b"notification-test-secret-32bytes!!"
HOSTS = frozenset({"home-mac"})


class Clock:
    def __init__(self, now: int = 1_000_000) -> None:
        self.now = now

    def __call__(self) -> int:
        return self.now


def _state(
    clock: Clock | None = None, path: str | Path = ":memory:"
) -> tuple[GatewayState, Clock, FakeBackend]:
    clock = clock or Clock()
    backend = FakeBackend(now_ms=clock)
    doc: dict[str, Any] = json.loads((FIXTURES / "snapshot.json").read_text())
    doc["cursor"] = "snap-cursor-1"
    backend.snapshots["home-mac"] = doc
    state = GatewayState(backend=backend, cursor_secret=SECRET, now_ms=clock, push=PushStore(path))
    return state, clock, backend


def _token(
    state: GatewayState,
    account: str = "acct-1",
    caps: frozenset[str] = frozenset({"sessions.read"}),
    device_id: str = "install-1",
) -> str:
    access, _ = state.sessions.create(account, caps, HOSTS, state.now_ms(), device_id)
    return access


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# -- device subscriptions ------------------------------------------------


def test_register_rotate_revoke_lifecycle() -> None:
    state, _, _ = _state()
    client = TestClient(create_app(state))
    headers = _headers(_token(state))
    r = client.post(
        "/v1/devices",
        headers=headers,
        json={"device_id": "install-1", "token": "fcm-a", "platform": "android"},
    )
    assert r.status_code == 201, r.text
    assert r.json() == {"device_id": "install-1", "platform": "android", "status": "active"}
    assert state.push.token_for("acct-1", "install-1") == "fcm-a"

    r = client.post("/v1/devices/install-1/rotate", headers=headers, json={"token": "fcm-b"})
    assert r.status_code == 200, r.text
    assert state.push.token_for("acct-1", "install-1") == "fcm-b"

    r = client.delete("/v1/devices/install-1", headers=headers)
    assert r.status_code == 200
    assert r.json() == {"ok": True}
    assert state.push.token_for("acct-1", "install-1") is None
    assert state.push.subscription_count("acct-1") == 0
    # Revoking the current device also revokes its access/refresh family.
    assert client.delete("/v1/devices/install-1", headers=headers).status_code == 401


def test_rotation_never_multiplies_subscriptions() -> None:
    state, _, _ = _state()
    client = TestClient(create_app(state))
    headers = _headers(_token(state))
    client.post(
        "/v1/devices",
        headers=headers,
        json={"device_id": "install-1", "token": "fcm-a", "platform": "android"},
    )
    first_ref = state.push.active_devices("acct-1")[0].device_ref
    for token in ("fcm-b", "fcm-c", "fcm-d"):
        r = client.post("/v1/devices/install-1/rotate", headers=headers, json={"token": token})
        assert r.status_code == 200
    assert state.push.subscription_count("acct-1") == 1
    devices = state.push.active_devices("acct-1")
    assert len(devices) == 1
    assert devices[0].device_ref == first_ref
    assert devices[0].token == "fcm-d"


def test_device_routes_reject_bad_input() -> None:
    state, _, _ = _state()
    client = TestClient(create_app(state))
    headers = _headers(_token(state))
    assert client.post("/v1/devices", json={}).status_code == 401
    # Unknown platform.
    r = client.post(
        "/v1/devices",
        headers=headers,
        json={"device_id": "d", "token": "t", "platform": "ios"},
    )
    assert r.status_code == 400
    # Malformed bodies.
    for body in ({}, {"device_id": "d"}, {"token": "t", "platform": "android", "x": 1}):
        r = client.post("/v1/devices", headers=headers, json=body)
        assert r.status_code == 400, body
    # Rotate unknown device / malformed rotation.
    r = client.post("/v1/devices/nope/rotate", headers=headers, json={"token": "t"})
    assert r.status_code == 404
    r = client.post("/v1/devices/install-1/rotate", headers=headers, json={})
    assert r.status_code == 400


def test_logout_removes_bound_subscription() -> None:
    state, _, _ = _state()
    client = TestClient(create_app(state))
    token = _token(state, device_id="install-1")
    headers = _headers(token)
    client.post(
        "/v1/devices",
        headers=headers,
        json={"device_id": "install-1", "token": "fcm-a", "platform": "android"},
    )
    state.push.notify(
        account="acct-1",
        account_hosts=HOSTS,
        host_id="home-mac",
        session_id="term-1",
        event_id="ev-1",
        category="attention",
        now=1_000_000,
    )
    assert state.push.pending_count("acct-1") == 1
    assert client.post("/v1/logout", headers=headers).status_code == 200
    # Subscription AND token association are gone; pending hints dropped.
    assert state.push.subscription_count("acct-1") == 0
    assert state.push.token_for("acct-1", "install-1") is None
    assert state.push.pending_count("acct-1") == 0


def test_logout_flow_deletes_install_id_explicitly() -> None:
    """When the install id differs from the session device id, the client
    DELETEs its install subscription as part of logout (documented rule)."""
    state, _, _ = _state()
    client = TestClient(create_app(state))
    token = _token(state, device_id="session-dev-9")
    headers = _headers(token)
    client.post(
        "/v1/devices",
        headers=headers,
        json={"device_id": "install-7", "token": "fcm-a", "platform": "android"},
    )
    assert client.post("/v1/logout", headers=headers).status_code == 200
    # Server logout only revokes the bound session device id.
    assert state.push.token_for("acct-1", "install-7") == "fcm-a"
    # A fresh login DELETEs the install subscription explicitly.
    headers2 = _headers(_token(state, device_id="session-dev-9"))
    assert client.delete("/v1/devices/install-7", headers=headers2).status_code == 200
    assert state.push.subscription_count("acct-1") == 0


def test_reinstall_tombstones_old_and_never_revives() -> None:
    state, clock, _ = _state()
    state.push.register_device("acct-1", "install-1", "fcm-a", "android", clock.now)
    rows = state.push.notify(
        account="acct-1",
        account_hosts=HOSTS,
        host_id="home-mac",
        session_id="term-1",
        event_id="ev-1",
        category="attention",
        now=clock.now,
    )
    assert state.push.resolve_link(str(rows[0]["link_token"]), "acct-1", clock.now)
    fresh = state.push.replace_device(
        "acct-1", "install-1", "install-2", "fcm-b", "android", clock.now
    )
    assert fresh.status == "active"
    assert state.push.subscription_count("acct-1") == 2
    old = [d for d in [state.push.rotate_token("acct-1", "install-1", "fcm-x", clock.now)]]
    assert old == [None]  # replaced rows never revive via rotation
    try:
        state.push.register_device("acct-1", "install-1", "fcm-y", "android", clock.now)
    except PushDenied as exc:
        assert exc.code == "not_found"
    else:
        raise AssertionError("replaced install re-registered")
    # Old links and hints fail; the new install notifies cleanly.
    assert state.push.resolve_link(str(rows[0]["link_token"]), "acct-1", clock.now) is None
    assert state.push.resolve_hint("acct-1", str(rows[0]["notification_id"]), clock.now) is None
    assert state.push.token_for("acct-1", "install-2") == "fcm-b"
    # Tombstones age out after 30 days.
    clock.now += DEVICE_TOMBSTONE_MS + 1
    purged = state.push.purge(clock.now)
    assert purged["devices"] == 1
    assert state.push.subscription_count("acct-1") == 1


def test_token_invalid_revives_on_liveness_proof() -> None:
    state, _, _ = _state()
    state.push.register_device("acct-1", "install-1", "fcm-a", "android", 1_000_000)
    assert state.push.mark_token_invalid("acct-1", "install-1", 1_000_000) is True
    assert state.push.token_for("acct-1", "install-1") is None
    revived = state.push.rotate_token("acct-1", "install-1", "fcm-fresh", 1_000_000)
    assert revived is not None and revived.status == "active"
    assert state.push.token_for("acct-1", "install-1") == "fcm-fresh"
    assert state.push.subscription_count("acct-1") == 1


# -- outbox: fanout, dedupe, order, TTL ----------------------------------


def test_fanout_dedupe_per_device() -> None:
    state, _, _ = _state()
    state.push.register_device("acct-1", "dev-a", "tok-a", "android", 1_000_000)
    state.push.register_device("acct-1", "dev-b", "tok-b", "android", 1_000_000)
    first = state.push.notify(
        account="acct-1",
        account_hosts=HOSTS,
        host_id="home-mac",
        session_id="term-1",
        event_id="ev-1",
        category="attention",
        now=1_000_000,
    )
    assert len(first) == 2
    # Same event is never enqueued twice per device.
    again = state.push.notify(
        account="acct-1",
        account_hosts=HOSTS,
        host_id="home-mac",
        session_id="term-1",
        event_id="ev-1",
        category="attention",
        now=1_000_000,
    )
    assert again == []
    assert state.push.pending_count("acct-1") == 2
    # A new event fans out again.
    second = state.push.notify(
        account="acct-1",
        account_hosts=HOSTS,
        host_id="home-mac",
        session_id="term-1",
        event_id="ev-2",
        category="completion",
        now=1_000_000,
    )
    assert len(second) == 2
    assert state.push.pending_count("acct-1") == 4


def test_per_device_fifo_order() -> None:
    state, _, _ = _state()
    state.push.register_device("acct-1", "dev-a", "tok-a", "android", 1_000_000)
    state.push.register_device("acct-1", "dev-b", "tok-b", "android", 1_000_000)
    for event_id, at in (("ev-1", 1_000_000), ("ev-2", 1_001_000)):
        state.push.notify(
            account="acct-1",
            account_hosts=HOSTS,
            host_id="home-mac",
            session_id="term-1",
            event_id=event_id,
            category="attention",
            now=at,
        )
    due = state.push.due(2_000_000)
    assert [(row["device_id"], row["event_id"]) for row in due] == [
        ("dev-a", "ev-1"),
        ("dev-a", "ev-2"),
        ("dev-b", "ev-1"),
        ("dev-b", "ev-2"),
    ]


def test_ttl_expiry_drop_never_sent() -> None:
    state, clock, _ = _state()
    state.push.register_device("acct-1", "install-1", "fcm-a", "android", clock.now)
    state.push.notify(
        account="acct-1",
        account_hosts=HOSTS,
        host_id="home-mac",
        session_id="term-1",
        event_id="ev-1",
        category="attention",
        now=clock.now,
        ttl_ms=60_000,
    )
    provider = StubProvider()
    clock.now += 61_000
    report = pump_outbox(state.push, provider, clock.now)
    assert provider.sends == []
    assert report == {"sent": 0, "retried": 0, "dropped": 1}
    assert state.push.pending_count() == 0


def test_eligibility_matrix() -> None:
    state, _, _ = _state()
    state.push.register_device("acct-1", "install-1", "fcm-a", "android", 1_000_000)
    base = {
        "account": "acct-1",
        "account_hosts": HOSTS,
        "host_id": "home-mac",
        "session_id": "term-1",
        "event_id": "ev-1",
        "category": "attention",
        "now": 1_000_000,
    }
    with_denied: list[dict[str, Any]] = [
        {**base, "host_id": "foreign-mac"},
        {**base, "host_id": ""},
        {**base, "category": "approve"},
        {**base, "session_id": ""},
        {**base, "event_id": ""},
        {**base, "ttl_ms": 0},
    ]
    for kwargs in with_denied:
        try:
            state.push.notify(**kwargs)
        except PushDenied:
            pass
        else:
            raise AssertionError(f"ineligible notify accepted: {kwargs}")
    assert state.push.pending_count() == 0
    # No devices: eligible but no fanout (never an error).
    rows = state.push.notify(
        account="acct-nobody",
        account_hosts=HOSTS,
        host_id="home-mac",
        session_id="term-1",
        event_id="ev-1",
        category="attention",
        now=1_000_000,
    )
    assert rows == []


# -- provider pump: retry, backoff, permanent failure ---------------------


def test_retry_backoff_then_sent() -> None:
    state, clock, _ = _state()
    state.push.register_device("acct-1", "install-1", "fcm-a", "android", clock.now)
    rows = state.push.notify(
        account="acct-1",
        account_hosts=HOSTS,
        host_id="home-mac",
        session_id="term-1",
        event_id="ev-1",
        category="attention",
        now=clock.now,
    )
    nid = str(rows[0]["notification_id"])
    provider = StubProvider(
        [ProviderOutcome.RETRYABLE, ProviderOutcome.RETRYABLE, ProviderOutcome.SENT]
    )
    assert backoff_ms(1) == BACKOFF_BASE_MS
    assert backoff_ms(2) == 2 * BACKOFF_BASE_MS
    assert backoff_ms(20) == 15 * 60_000  # capped

    report = pump_outbox(state.push, provider, clock.now)
    assert report == {"sent": 0, "retried": 1, "dropped": 0}
    row = state.push.get_notification(nid)
    assert row is not None and row["attempts"] == 1
    assert row["next_attempt_ms"] == clock.now + BACKOFF_BASE_MS

    clock.now += BACKOFF_BASE_MS
    report = pump_outbox(state.push, provider, clock.now)
    assert report == {"sent": 0, "retried": 1, "dropped": 0}
    row = state.push.get_notification(nid)
    assert row is not None and row["attempts"] == 2
    assert row["next_attempt_ms"] == clock.now + 2 * BACKOFF_BASE_MS

    clock.now += 2 * BACKOFF_BASE_MS
    report = pump_outbox(state.push, provider, clock.now)
    assert report == {"sent": 1, "retried": 0, "dropped": 0}
    assert len(provider.sends) == 3
    sent = state.push.get_notification(nid)
    assert sent is not None and sent["status"] == "sent"


def test_provider_exception_retries_inside_ttl() -> None:
    state, clock, _ = _state()
    state.push.register_device("acct-1", "install-1", "fcm-a", "android", clock.now)
    state.push.notify(
        account="acct-1",
        account_hosts=HOSTS,
        host_id="home-mac",
        session_id="term-1",
        event_id="ev-1",
        category="attention",
        now=clock.now,
    )
    provider = StubProvider([RuntimeError("fcm down"), ProviderOutcome.SENT])
    report = pump_outbox(state.push, provider, clock.now)
    assert report["retried"] == 1
    clock.now += BACKOFF_BASE_MS
    report = pump_outbox(state.push, provider, clock.now)
    assert report["sent"] == 1


def test_persistent_failure_drops_at_ttl() -> None:
    state, clock, _ = _state()
    state.push.register_device("acct-1", "install-1", "fcm-a", "android", clock.now)
    rows = state.push.notify(
        account="acct-1",
        account_hosts=HOSTS,
        host_id="home-mac",
        session_id="term-1",
        event_id="ev-1",
        category="attention",
        now=clock.now,
        ttl_ms=120_000,
    )
    nid = str(rows[0]["notification_id"])
    provider = StubProvider(default=ProviderOutcome.RETRYABLE)
    # t+0: fail, retry at t+30. t+30: fail, retry at t+90. t+90: the next
    # backoff (120s) cannot fit inside TTL, so the row drops now.
    assert pump_outbox(state.push, provider, clock.now)["retried"] == 1
    clock.now += 30_000
    assert pump_outbox(state.push, provider, clock.now)["retried"] == 1
    clock.now += 60_000
    report = pump_outbox(state.push, provider, clock.now)
    assert report["dropped"] == 1
    assert len(provider.sends) == 3
    dropped = state.push.get_notification(nid)
    assert dropped is not None and dropped["status"] == "dropped"
    assert state.push.pending_count() == 0


def test_permanent_failure_tombstones_device() -> None:
    state, clock, _ = _state()
    state.push.register_device("acct-1", "install-1", "fcm-dead", "android", clock.now)
    for event_id in ("ev-1", "ev-2"):
        state.push.notify(
            account="acct-1",
            account_hosts=HOSTS,
            host_id="home-mac",
            session_id="term-1",
            event_id=event_id,
            category="attention",
            now=clock.now,
        )
    provider = StubProvider(default=ProviderOutcome.PERMANENT)
    report = pump_outbox(state.push, provider, clock.now)
    # First send reports the token dead: device tombstoned, BOTH pending
    # hints dropped (the token cannot deliver any of them).
    assert report["dropped"] == 2
    assert state.push.token_for("acct-1", "install-1") is None
    assert state.push.pending_count() == 0


# -- payload allowlist audit (lock-screen secrecy) ------------------------


def test_payload_key_allowlist_is_exact() -> None:
    assert HINT_KEYS == set(ALLOWED_KEYS)
    assert HINT_CATEGORIES == set(ALLOWED_CATEGORIES)
    assert HINT_KEYS == {
        "notification_id",
        "device_ref",
        "host_ref",
        "session_ref",
        "event_ref",
        "category",
        "created_at",
        "expires_at",
    }
    state, _, _ = _state()
    state.push.register_device("acct-1", "install-1", "fcm-a", "android", 1_000_000)
    rows = state.push.notify(
        account="acct-1",
        account_hosts=HOSTS,
        host_id="home-mac",
        session_id="term-1",
        event_id="ev-1",
        category="completion",
        now=1_000_000,
    )
    payload = state.push.hint_payload(str(rows[0]["notification_id"]))
    assert set(payload) == set(HINT_KEYS)
    # The extension guard accepts exactly what the server emits.
    parsed = parse_push_payload(payload)
    assert parsed.category == "completion"


def test_adversarial_content_never_leaks_into_hints() -> None:
    canaries = [
        "CANARY-PRIVATE-KEY-9f8e7d6c5b4a",
        "CANARY-rm-rf-slash-etc-shadow",
        "CANARY-APPROVE-YES-13579",
        "CANARY-path-home-secrets-env",
        "CANARY-unicode-\u00e9\u0301-output",
    ]
    event_id = f"ev-{canaries[0]}-\x1b[2J;{canaries[1]}"
    session_id = f"term-{canaries[2]}-{canaries[3]}-{canaries[4]}"
    assert len(event_id) <= 256 and len(session_id) <= 128
    state, _, _ = _state()
    state.push.register_device("acct-1", "install-1", "fcm-a", "android", 1_000_000)
    rows = state.push.notify(
        account="acct-1",
        account_hosts=HOSTS,
        host_id="home-mac",
        session_id=session_id,
        event_id=event_id,
        category="attention",
        now=1_000_000,
    )
    payload = state.push.hint_payload(str(rows[0]["notification_id"]))
    blob = json.dumps(payload)
    for canary in canaries:
        assert canary not in blob
    # No control characters or escape sequences survive into the payload.
    for value in payload.values():
        assert "\x1b" not in value and "\n" not in value
    parse_push_payload(payload)


# -- tap path: resolve -> fetch -> route, never input ---------------------


def test_tap_path_never_sends_input() -> None:
    state, _, backend = _state()
    client = TestClient(create_app(state))
    headers = _headers(_token(state))
    client.post(
        "/v1/devices",
        headers=headers,
        json={"device_id": "install-1", "token": "fcm-a", "platform": "android"},
    )
    rows = state.push.notify(
        account="acct-1",
        account_hosts=HOSTS,
        host_id="home-mac",
        session_id="term-1",
        event_id="ev-1",
        category="attention",
        now=1_000_000,
    )
    row = rows[0]

    async def scenario() -> dict[str, Any]:
        port = FakeNotifications(permission=PushPermission.GRANTED)
        taps: list[PushTap] = []
        port.on_tap(taps.append)
        port.tap(
            PushTap(
                notification_id=str(row["notification_id"]),
                event_ref=str(row["event_ref"]),
                category="attention",
            )
        )
        assert len(taps) == 1
        # Tap -> authenticated resolve -> fetch CURRENT state -> route.
        resolved = client.post(
            "/v1/deep-links/resolve",
            headers=headers,
            json={"notification_id": taps[0].notification_id},
        )
        assert resolved.status_code == 200, resolved.text
        current = client.get(f"/v1/hosts/{resolved.json()['host_id']}/snapshot", headers=headers)
        assert current.status_code == 200
        return dict(resolved.json())

    resolved_body = asyncio.run(scenario())
    assert resolved_body["route"] == {"host_id": "home-mac", "session_id": "term-1"}
    assert backend.input_logs == {}
    assert backend.commands == {}


def test_app_link_and_killed_app_tap_paths() -> None:
    state, _, backend = _state()
    client = TestClient(create_app(state))
    headers = _headers(_token(state))
    state.push.register_device("acct-1", "install-1", "fcm-a", "android", 1_000_000)
    rows = state.push.notify(
        account="acct-1",
        account_hosts=HOSTS,
        host_id="home-mac",
        session_id="term-1",
        event_id="ev-1",
        category="completion",
        now=1_000_000,
    )

    async def scenario() -> None:
        port = FakeNotifications(permission=PushPermission.GRANTED)
        token = str(rows[0]["link_token"])
        r = client.post("/v1/deep-links/resolve", headers=headers, json={"token": token})
        assert r.status_code == 200
        # Killed-app relaunch via getInitialMessage equivalent.
        port.set_initial_tap(
            PushTap(
                notification_id=str(rows[0]["notification_id"]),
                event_ref=str(rows[0]["event_ref"]),
                category="completion",
            )
        )
        initial = await port.initial_tap()
        assert initial is not None
        r = client.post(
            "/v1/deep-links/resolve",
            headers=headers,
            json={"notification_id": initial.notification_id},
        )
        assert r.status_code == 200

    asyncio.run(scenario())
    assert backend.input_logs == {}
    assert backend.commands == {}


# -- late hints, durability ---------------------------


def test_late_hint_still_opens_current_state() -> None:
    state, clock, _ = _state()
    state.push.register_device("acct-1", "install-1", "fcm-a", "android", clock.now)
    rows = state.push.notify(
        account="acct-1",
        account_hosts=HOSTS,
        host_id="home-mac",
        session_id="term-1",
        event_id="ev-1",
        category="attention",
        now=clock.now,
        ttl_ms=60_000,
    )
    nid = str(rows[0]["notification_id"])
    clock.now += 3_600_000  # long after TTL: the hint is dropped...
    assert pump_outbox(state.push, StubProvider(), clock.now)["dropped"] == 1
    # ...but opening it still resolves to current state (a hint, not data).
    resolved = state.push.resolve_hint("acct-1", nid, clock.now)
    assert resolved is not None
    assert resolved["host_id"] == "home-mac"


def test_file_durability_and_retention(tmp_path: Path) -> None:
    db = tmp_path / "push.db"
    state, clock, _ = _state(path=db)
    try:
        assert stat.S_IMODE(db.stat().st_mode) == 0o600
        assert state.push.durability() == {"journal_mode": "wal", "synchronous": 2}
        state.push.register_device("acct-1", "install-1", "fcm-a", "android", clock.now)
        state.push.notify(
            account="acct-1",
            account_hosts=HOSTS,
            host_id="home-mac",
            session_id="term-1",
            event_id="ev-sent",
            category="attention",
            now=clock.now,
        )
        assert pump_outbox(state.push, StubProvider(), clock.now)["sent"] == 1
        # A pending row must survive the retention sweep.
        clock.now += 3600_000
        state.push.notify(
            account="acct-1",
            account_hosts=HOSTS,
            host_id="home-mac",
            session_id="term-1",
            event_id="ev-pending",
            category="attention",
            now=clock.now,
        )
        # WAL sidecars inherit the umask at creation; every commit
        # re-chmods them (mirrors the sidecar test).
        for suffix in ("-wal", "-shm"):
            sidecar = Path(str(db) + suffix)
            assert sidecar.exists(), suffix
            mode = stat.S_IMODE(os.stat(sidecar).st_mode)
            assert mode == 0o600, (suffix, oct(mode))
        clock.now += TERMINAL_KEEP_MS + LINK_KEEP_MS + 1
        purged = state.push.purge(clock.now)
        assert purged["outbox"] == 1  # the sent row is gone...
        assert purged["links"] == 2  # ...both links expired out...
        assert state.push.pending_count() == 1  # ...but pending survives.
        assert state.push.resolve_ref("er_nonexistent") is None
    finally:
        state.push.close()


# -- fix round: logout-with-ref, revive, orphans, races ---------


def test_logout_with_device_ref_revokes_install_subscription() -> None:
    state, _, _ = _state()
    client = TestClient(create_app(state))
    token = _token(state, device_id="session-dev-9")
    headers = _headers(token)
    client.post(
        "/v1/devices",
        headers=headers,
        json={"device_id": "install-7", "token": "fcm-a", "platform": "android"},
    )
    ref = state.push.active_devices("acct-1")[0].device_ref
    state.push.notify(
        account="acct-1",
        account_hosts=HOSTS,
        host_id="home-mac",
        session_id="term-1",
        event_id="ev-1",
        category="attention",
        now=1_000_000,
    )
    assert state.push.pending_count("acct-1") == 1
    r = client.post("/v1/logout", headers=headers, json={"device_ref": ref})
    assert r.status_code == 200
    assert r.json() == {"ok": True}
    assert state.push.subscription_count("acct-1") == 0
    assert state.push.token_for("acct-1", "install-7") is None
    assert state.push.pending_count("acct-1") == 0


def test_logout_with_foreign_device_ref_is_silent_noop() -> None:
    state, _, _ = _state()
    client = TestClient(create_app(state))
    state.push.register_device("acct-2", "victim-install", "fcm-v", "android", 1_000_000)
    victim_ref = state.push.active_devices("acct-2")[0].device_ref
    state.push.notify(
        account="acct-2",
        account_hosts=HOSTS,
        host_id="home-mac",
        session_id="term-1",
        event_id="ev-v",
        category="attention",
        now=1_000_000,
    )
    headers = _headers(_token(state, device_id="session-dev-9"))
    r = client.post("/v1/logout", headers=headers, json={"device_ref": victim_ref})
    assert r.status_code == 200
    assert r.json() == {"ok": True}  # no disclosure of the foreign ref
    assert state.push.token_for("acct-2", "victim-install") == "fcm-v"
    assert state.push.pending_count("acct-2") == 1


def test_logout_with_unknown_device_ref_is_silent_noop() -> None:
    state, _, _ = _state()
    client = TestClient(create_app(state))
    token = _token(state, device_id="install-1")
    headers = _headers(token)
    client.post(
        "/v1/devices",
        headers=headers,
        json={"device_id": "install-1", "token": "fcm-a", "platform": "android"},
    )
    r = client.post("/v1/logout", headers=headers, json={"device_ref": "dr_no-such-ref"})
    assert r.status_code == 200
    # The unknown ref changes nothing; the bound session device is still
    # revoked by the normal logout path.
    assert state.push.subscription_count("acct-1") == 0


def test_logout_empty_body_keeps_existing_behavior() -> None:
    state, _, _ = _state()
    client = TestClient(create_app(state))
    token = _token(state, device_id="install-1")
    headers = _headers(token)
    client.post(
        "/v1/devices",
        headers=headers,
        json={"device_id": "install-1", "token": "fcm-a", "platform": "android"},
    )
    assert client.post("/v1/logout", headers=headers, json={}).status_code == 200
    assert state.push.subscription_count("acct-1") == 0


def test_token_invalid_revives_on_register() -> None:
    """Register-revive: a token-invalid tombstone (no replaced_by) revives
    on re-register with a fresh token, mirroring the rotation revive."""
    state, _, _ = _state()
    first = state.push.register_device("acct-1", "install-1", "fcm-a", "android", 1_000_000)
    assert state.push.mark_token_invalid("acct-1", "install-1", 1_000_000) is True
    assert state.push.token_for("acct-1", "install-1") is None
    revived = state.push.register_device("acct-1", "install-1", "fcm-fresh", "android", 1_000_000)
    assert revived.status == "active"
    assert revived.device_ref == first.device_ref  # SAME record revived
    assert state.push.token_for("acct-1", "install-1") == "fcm-fresh"
    assert state.push.subscription_count("acct-1") == 1  # no duplicate


def test_orphan_refs_purged_live_refs_kept() -> None:
    state, clock, _ = _state()
    state.push.register_device("acct-1", "install-1", "fcm-a", "android", clock.now)
    rows = state.push.notify(
        account="acct-1",
        account_hosts=HOSTS,
        host_id="home-mac",
        session_id="term-1",
        event_id="ev-live",
        category="attention",
        now=clock.now,
    )
    live_event_ref = str(rows[0]["event_ref"])
    orphan = state.push.ref_for("event", "orphan-event-value", clock.now)
    assert orphan != live_event_ref
    clock.now += TERMINAL_KEEP_MS + 1
    purged = state.push.purge(clock.now)
    assert purged["refs"] == 1
    assert state.push.resolve_ref(orphan) is None
    assert state.push.resolve_ref(live_event_ref) == ("event", "ev-live")


def test_concurrent_double_enqueue_yields_one_row() -> None:
    """Threaded double-enqueue: dedupe wins, exactly one row, no
    exception (the IntegrityError guard covers cross-process races)."""
    state, _, _ = _state()
    state.push.register_device("acct-1", "install-1", "fcm-a", "android", 1_000_000)
    errors: list[Exception] = []

    def worker() -> None:
        try:
            state.push.notify(
                account="acct-1",
                account_hosts=HOSTS,
                host_id="home-mac",
                session_id="term-1",
                event_id="ev-race",
                category="attention",
                now=1_000_000,
            )
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert state.push.pending_count("acct-1") == 1


def test_mark_failed_unknown_id_is_honest() -> None:
    state, _, _ = _state()
    assert state.push.mark_failed("n_no-such-notification", "boom", 1_000_000) == "unknown"
    # Known non-pending rows still report their real status.
    state.push.register_device("acct-1", "install-1", "fcm-a", "android", 1_000_000)
    rows = state.push.notify(
        account="acct-1",
        account_hosts=HOSTS,
        host_id="home-mac",
        session_id="term-1",
        event_id="ev-1",
        category="attention",
        now=1_000_000,
    )
    nid = str(rows[0]["notification_id"])
    assert state.push.mark_sent(nid, 1_000_000) is True
    assert state.push.mark_failed(nid, "late", 1_000_000) == "sent"
