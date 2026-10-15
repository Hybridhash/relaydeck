"""security rules 1, 3 and 4 at the store and pump level, with synthetic data."""

from __future__ import annotations

import json
import logging
import os
import stat
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from relaydeck.gateway import webpush, webpush_store
from relaydeck.gateway.push import ProviderOutcome, ProviderPort
from relaydeck.gateway.webpush_store import WebPushStore

NOW = 1_700_000_000_000
ACCOUNT = "acct-one"
DEVICE = "dev-phone"
OTHER_DEVICE = "dev-laptop"
ENDPOINT = "https://fcm.googleapis.com/fcm/send/synthetic-endpoint-1"
ENDPOINT_TWO = "https://web.push.apple.com/synthetic-endpoint-2"


class Recorder(ProviderPort):
    def __init__(self, outcome: ProviderOutcome = ProviderOutcome.SENT) -> None:
        self.outcome = outcome
        self.calls: list[tuple[dict[str, Any], dict[str, str]]] = []

    def send(self, token: str, payload: dict[str, str]) -> ProviderOutcome:
        self.calls.append((json.loads(token), dict(payload)))
        return self.outcome


def _keys() -> tuple[str, str]:
    priv = ec.generate_private_key(ec.SECP256R1())
    pub = priv.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    return webpush.b64u(pub), webpush.b64u(b"\x07" * 16)


@pytest.fixture
def state(tmp_path: Path) -> Path:
    directory = tmp_path / "state"
    directory.mkdir(mode=0o700)
    return directory


@pytest.fixture
def store(state: Path) -> WebPushStore:
    db = WebPushStore(state, clock=lambda: NOW)
    yield db
    db.close()


def _subscribe(store: WebPushStore, device: str = DEVICE, endpoint: str = ENDPOINT) -> int:
    p256dh, auth = _keys()
    return store.put_subscription(ACCOUNT, device, endpoint, p256dh, auth, NOW)


def test_the_database_is_private_and_separate_from_the_android_push_store(
    store: WebPushStore, state: Path
) -> None:
    assert store.path == state / "webpush" / "webpush.db"
    assert stat.S_IMODE(os.stat(store.path).st_mode) == 0o600
    assert not (state / "push.db").exists()


def test_a_resubscribe_gets_a_new_generation_and_drops_older_queued_work(
    store: WebPushStore,
) -> None:
    first = _subscribe(store)
    assert store.enqueue(ACCOUNT, DEVICE, "event-1", "attention", now=NOW) is not None
    second = _subscribe(store, endpoint=ENDPOINT_TWO)
    assert second == first + 1
    assert store.due(NOW + 1) == []


def test_each_event_is_queued_once_per_device(store: WebPushStore) -> None:
    _subscribe(store)
    assert store.enqueue(ACCOUNT, DEVICE, "event-1", "completion", now=NOW) is not None
    assert store.enqueue(ACCOUNT, DEVICE, "event-1", "completion", now=NOW) is None


def test_no_subscription_means_nothing_is_queued(store: WebPushStore) -> None:
    assert store.enqueue(ACCOUNT, DEVICE, "event-1", "attention", now=NOW) is None


def test_a_route_token_works_only_for_its_own_device_and_account(store: WebPushStore) -> None:
    _subscribe(store)
    _subscribe(store, device=OTHER_DEVICE, endpoint=ENDPOINT_TWO)
    route = store.enqueue(ACCOUNT, DEVICE, "event-1", "attention", now=NOW)
    assert route is not None
    assert store.resolve_route(ACCOUNT, DEVICE, route, NOW) == {
        "category": "attention",
        "host_id": "",
        "session_id": "",
    }
    assert store.resolve_route(ACCOUNT, OTHER_DEVICE, route, NOW) is None  # cross-device denial
    assert store.resolve_route("acct-two", DEVICE, route, NOW) is None  # cross-account denial


def test_a_route_token_expires_and_dies_with_its_generation(store: WebPushStore) -> None:
    _subscribe(store)
    route = store.enqueue(ACCOUNT, DEVICE, "event-1", "attention", now=NOW)
    assert route is not None
    assert store.resolve_route(ACCOUNT, DEVICE, route, NOW + 25 * 60 * 60 * 1000) is None
    _subscribe(store, endpoint=ENDPOINT_TWO)
    assert store.resolve_route(ACCOUNT, DEVICE, route, NOW) is None


def test_a_malformed_route_token_is_refused_before_lookup(store: WebPushStore) -> None:
    _subscribe(store)
    assert store.resolve_route(ACCOUNT, DEVICE, "'; DROP TABLE x;--", NOW) is None
    assert store.resolve_route(ACCOUNT, DEVICE, "x" * 500, NOW) is None


def test_revoking_a_device_deletes_its_queued_work_and_routes(store: WebPushStore) -> None:
    _subscribe(store)
    route = store.enqueue(ACCOUNT, DEVICE, "event-1", "attention", now=NOW)
    assert route is not None
    assert store.delete_subscription(ACCOUNT, DEVICE) is True
    assert store.due(NOW + 1) == []
    assert store.resolve_route(ACCOUNT, DEVICE, route, NOW) is None


def test_a_stale_410_never_deletes_the_newer_generation(store: WebPushStore) -> None:
    old = _subscribe(store)
    store.enqueue(ACCOUNT, DEVICE, "event-old", "attention", now=NOW)
    new = _subscribe(store, endpoint=ENDPOINT_TWO)  # the device re-registered meanwhile
    assert store.delete_generation(ACCOUNT, DEVICE, old) is False
    current = store.subscription(ACCOUNT, DEVICE)
    assert current is not None and current.generation == new and current.endpoint == ENDPOINT_TWO


def test_a_current_410_deletes_that_generation(store: WebPushStore) -> None:
    generation = _subscribe(store)
    assert store.delete_generation(ACCOUNT, DEVICE, generation) is True
    assert store.subscription(ACCOUNT, DEVICE) is None


def test_the_lock_screen_text_is_generic_by_default(store: WebPushStore) -> None:
    _subscribe(store)
    store.enqueue(ACCOUNT, DEVICE, "event-1", "attention", now=NOW, label="home-mac")
    provider = Recorder()
    webpush_store.pump(store, provider, topic_key=b"k" * 32, now=NOW)
    _, payload = provider.calls[0]
    assert payload["b"] == "An agent needs attention"
    assert "home-mac" not in json.dumps(payload)


def test_lock_screen_detail_adds_only_the_label_when_switched_on(store: WebPushStore) -> None:
    _subscribe(store)
    store.enqueue(ACCOUNT, DEVICE, "event-1", "completion", now=NOW, label="home-mac")
    provider = Recorder()
    webpush_store.pump(store, provider, topic_key=b"k" * 32, now=NOW, lock_screen_detail=True)
    assert provider.calls[0][1]["b"] == "An agent finished · home-mac"


def test_the_payload_has_only_the_three_fixed_fields_and_never_terminal_text(
    store: WebPushStore,
) -> None:
    _subscribe(store)
    store.enqueue(ACCOUNT, DEVICE, "event-1", "attention", now=NOW, label="ls -la secret-path")
    provider = Recorder()
    webpush_store.pump(store, provider, topic_key=b"k" * 32, now=NOW)
    _, payload = provider.calls[0]
    assert set(payload) == {"t", "b", "r"}
    assert payload["t"] == "RelayDeck"
    assert "secret-path" not in json.dumps(payload)


def test_the_topic_is_domain_separated_and_never_shows_the_device_or_route(
    store: WebPushStore,
) -> None:
    key = b"t" * 32
    topic = webpush_store.topic_for(key, DEVICE, "wp_route")
    assert topic == webpush_store.topic_for(key, DEVICE, "wp_route")
    assert len(topic) <= 32
    assert DEVICE not in topic and "wp_route" not in topic
    assert topic != webpush_store.topic_for(key, OTHER_DEVICE, "wp_route")
    assert topic != webpush_store.topic_for(key, DEVICE, "wp_other")
    assert topic != webpush_store.topic_for(b"u" * 32, DEVICE, "wp_route")


def test_the_pump_sends_a_topic_that_matches_the_device_and_route(store: WebPushStore) -> None:
    _subscribe(store)
    route = store.enqueue(ACCOUNT, DEVICE, "event-1", "attention", now=NOW)
    provider = Recorder()
    webpush_store.pump(store, provider, topic_key=b"t" * 32, now=NOW)
    token, _ = provider.calls[0]
    assert token["topic"] == webpush_store.topic_for(b"t" * 32, DEVICE, str(route))
    assert token["urgency"] == "high"
    assert 0 < token["ttl"] <= 3600


def test_ttl_and_retry_lifetime_are_bounded(store: WebPushStore) -> None:
    _subscribe(store)
    store.enqueue(ACCOUNT, DEVICE, "event-1", "attention", now=NOW, ttl_ms=10 * 24 * 60 * 60 * 1000)
    row = store.due(NOW)[0]
    assert int(row["expires_ms"]) - NOW <= 60 * 60 * 1000  # the 1 h retry lifetime


def test_a_revoked_device_is_not_sent_to_and_its_subscription_is_deleted(
    store: WebPushStore,
) -> None:
    _subscribe(store)
    store.enqueue(ACCOUNT, DEVICE, "event-1", "attention", now=NOW)
    provider = Recorder()
    report = webpush_store.pump(
        store, provider, topic_key=b"k" * 32, now=NOW, authorized=lambda a, d: False
    )
    assert provider.calls == []
    assert report["dropped"] == 1
    assert store.subscription(ACCOUNT, DEVICE) is None


def test_a_permanent_provider_answer_removes_only_the_attempted_generation(
    store: WebPushStore,
) -> None:
    _subscribe(store)
    store.enqueue(ACCOUNT, DEVICE, "event-1", "attention", now=NOW)
    provider = Recorder(ProviderOutcome.PERMANENT)
    webpush_store.pump(store, provider, topic_key=b"k" * 32, now=NOW)
    assert store.subscription(ACCOUNT, DEVICE) is None
    assert store.due(NOW + 1) == []


def test_a_retryable_failure_backs_off_and_stays_within_the_lifetime(store: WebPushStore) -> None:
    _subscribe(store)
    store.enqueue(ACCOUNT, DEVICE, "event-1", "attention", now=NOW)
    provider = Recorder(ProviderOutcome.RETRYABLE)
    report = webpush_store.pump(store, provider, topic_key=b"k" * 32, now=NOW)
    assert report["retried"] == 1
    assert store.due(NOW + 1) == []  # backed off
    assert len(store.due(NOW + 60_000)) == 1


def test_sending_switched_off_sends_nothing(store: WebPushStore) -> None:
    _subscribe(store)
    store.enqueue(ACCOUNT, DEVICE, "event-1", "attention", now=NOW)
    store.set_sending(False)
    provider = Recorder()
    webpush_store.pump(store, provider, topic_key=b"k" * 32, now=NOW)
    assert provider.calls == []


def test_rotation_disables_sending_removes_everything_and_turns_sending_back_on(
    store: WebPushStore, state: Path
) -> None:
    before = webpush.public_key_b64u(webpush.load_or_create_vapid(state))
    _subscribe(store)
    store.enqueue(ACCOUNT, DEVICE, "event-1", "attention", now=NOW)
    result = webpush_store.rotate(state, store)
    assert result == {"subscriptions_removed": 1}
    assert store.subscription(ACCOUNT, DEVICE) is None
    assert store.due(NOW + 1) == []
    assert store.sending_enabled() is True
    assert webpush.public_key_b64u(webpush.load_or_create_vapid(state)) != before


def test_no_secrets_are_logged_while_storing_and_sending(
    store: WebPushStore, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    p256dh, auth = _keys()
    store.put_subscription(ACCOUNT, DEVICE, ENDPOINT, p256dh, auth, NOW)
    store.enqueue(ACCOUNT, DEVICE, "event-1", "attention", now=NOW)
    webpush_store.pump(store, Recorder(), topic_key=b"k" * 32, now=NOW)
    assert caplog.records == []
    assert ENDPOINT not in caplog.text and auth not in caplog.text and p256dh not in caplog.text


def test_a_late_410_for_a_deleted_subscription_leaves_the_re_registered_one_intact(
    store: WebPushStore,
) -> None:
    """Delete, register again, then the old subscription's delayed failure arrives: the numbers
    never repeat, so the new subscription survives."""
    old = _subscribe(store)
    store.enqueue(ACCOUNT, DEVICE, "event-old", "attention", now=NOW)
    assert store.delete_subscription(ACCOUNT, DEVICE) is True
    new = _subscribe(store, endpoint=ENDPOINT_TWO)
    assert new > old
    assert store.delete_generation(ACCOUNT, DEVICE, old) is False
    current = store.subscription(ACCOUNT, DEVICE)
    assert current is not None and current.generation == new and current.endpoint == ENDPOINT_TWO


def test_a_late_failure_through_the_pump_path_spares_the_replacement(store: WebPushStore) -> None:
    class Slow(ProviderPort):
        """While the provider call is in flight the device is revoked and registers again."""

        def send(self, token: str, payload: dict[str, str]) -> ProviderOutcome:
            store.delete_subscription(ACCOUNT, DEVICE)
            _subscribe(store, endpoint=ENDPOINT_TWO)
            return ProviderOutcome.PERMANENT

    _subscribe(store)
    store.enqueue(ACCOUNT, DEVICE, "event-1", "attention", now=NOW)
    webpush_store.pump(store, Slow(), topic_key=b"k" * 32, now=NOW)
    current = store.subscription(ACCOUNT, DEVICE)
    assert current is not None and current.endpoint == ENDPOINT_TWO


def test_key_rotation_does_not_reset_the_generation_counter(
    store: WebPushStore, state: Path
) -> None:
    before = _subscribe(store)
    webpush_store.rotate(state, store)
    assert store.subscription(ACCOUNT, DEVICE) is None
    after = _subscribe(store)
    assert after > before
    assert store.delete_generation(ACCOUNT, DEVICE, before) is False
    assert store.subscription(ACCOUNT, DEVICE) is not None


def test_the_counter_survives_a_restart_and_continues_an_older_database(state: Path) -> None:
    first = WebPushStore(state, clock=lambda: NOW)
    p256dh, auth = _keys()
    one = first.put_subscription(ACCOUNT, DEVICE, ENDPOINT, p256dh, auth, NOW)
    first.delete_subscription(ACCOUNT, DEVICE)
    first.close()
    second = WebPushStore(state, clock=lambda: NOW)
    try:
        assert second.put_subscription(ACCOUNT, DEVICE, ENDPOINT_TWO, p256dh, auth, NOW) > one
        # A database written before the counter existed: the live row's generation still wins.
        second._db.execute("DELETE FROM wp_generations")
        live = second.subscription(ACCOUNT, DEVICE)
        assert live is not None
        assert second.put_subscription(ACCOUNT, DEVICE, ENDPOINT, p256dh, auth, NOW) == (
            live.generation + 1
        )
    finally:
        second.close()
