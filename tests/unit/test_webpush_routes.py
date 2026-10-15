"""Session identity, CSRF, origin, caps."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import FastAPI
from fastapi.testclient import TestClient

from relaydeck.gateway import webpush, webpush_routes
from relaydeck.gateway.webpush_store import WebPushStore, topic_for

ORIGIN = "https://mac.example-tailnet.ts.net"
ACCOUNT = "acct-one"
PHONE = "dev-phone"
LAPTOP = "dev-laptop"
ENDPOINT = "https://fcm.googleapis.com/fcm/send/synthetic-endpoint-1"
NOW = 1_700_000_000.0


@pytest.fixture
def state(tmp_path: Path) -> Path:
    directory = tmp_path / "state"
    directory.mkdir(mode=0o700)
    return directory


class Preferences:
    """In-memory stand-in for the push store's per-device category choices."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], frozenset[str]] = {}

    def preferences(self, account: str, device_id: str) -> frozenset[str]:
        return self.rows.get((account, device_id), frozenset({"attention", "completion"}))

    def set_preferences(self, account: str, device_id: str, categories: Any, now: int) -> None:
        self.rows[(account, device_id)] = frozenset(categories)


class Sessions:
    """Stands in for the cookie session: the test chooses who the request belongs to."""

    def __init__(self) -> None:
        self.who: tuple[str, str] | None = None

    def __call__(self, request: Any) -> tuple[str, str] | None:
        return self.who


@pytest.fixture
def harness(state: Path) -> Iterator[tuple[TestClient, WebPushStore, Sessions, bytes]]:
    store = WebPushStore(state, clock=lambda: int(NOW * 1000))
    key = webpush.load_or_create_vapid(state)
    topic = webpush.load_or_create_topic_key(state)
    sessions = Sessions()
    app = FastAPI()
    app.include_router(
        webpush_routes.build_router(
            store=store,
            identity=sessions,
            origin=ORIGIN,
            vapid_key=key,
            topic_key=topic,
            settings=webpush_routes.WebPushSettings(),
            preferences=Preferences(),
            clock=lambda: NOW,
        )
    )
    client = TestClient(app, base_url=ORIGIN)
    yield client, store, sessions, topic
    store.close()


def _keys() -> tuple[str, str]:
    priv = ec.generate_private_key(ec.SECP256R1())
    pub = priv.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    return webpush.b64u(pub), webpush.b64u(b"\x05" * 16)


def _body(endpoint: str = ENDPOINT, **extra: Any) -> bytes:
    p256dh, auth = _keys()
    doc: dict[str, Any] = {"endpoint": endpoint, "keys": {"p256dh": p256dh, "auth": auth}}
    doc.update(extra)
    return json.dumps(doc).encode()


def _headers(client: TestClient, who: tuple[str, str], *, origin: str = ORIGIN) -> dict[str, str]:
    key_response = client.get("/v1/push/web/key")
    token = key_response.json()["csrf"]
    return {
        "Origin": origin,
        "X-RelayDeck-CSRF": token,
        "Content-Type": "application/json",
    }


def test_the_key_endpoint_needs_a_session_and_is_never_cached(
    harness: tuple[TestClient, WebPushStore, Sessions, bytes],
) -> None:
    client, _store, sessions, _topic = harness
    assert client.get("/v1/push/web/key").status_code == 401
    sessions.who = (ACCOUNT, PHONE)
    response = client.get("/v1/push/web/key")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["publicKey"]


def test_a_subscription_is_stored_for_the_session_device_only(
    harness: tuple[TestClient, WebPushStore, Sessions, bytes],
) -> None:
    client, store, sessions, _topic = harness
    sessions.who = (ACCOUNT, PHONE)
    response = client.put(
        "/v1/push/web/subscription", content=_body(), headers=_headers(client, (ACCOUNT, PHONE))
    )
    assert response.status_code == 201
    assert response.headers["cache-control"] == "no-store"
    assert store.subscription(ACCOUNT, PHONE) is not None
    assert store.subscription(ACCOUNT, LAPTOP) is None


def test_a_device_id_in_the_body_is_refused_and_cannot_pick_another_device(
    harness: tuple[TestClient, WebPushStore, Sessions, bytes],
) -> None:
    client, store, sessions, _topic = harness
    sessions.who = (ACCOUNT, PHONE)
    response = client.put(
        "/v1/push/web/subscription",
        content=_body(device_id=LAPTOP),
        headers=_headers(client, (ACCOUNT, PHONE)),
    )
    assert response.status_code == 400
    assert store.subscription(ACCOUNT, LAPTOP) is None


def test_mutations_without_the_exact_origin_are_refused(
    harness: tuple[TestClient, WebPushStore, Sessions, bytes],
) -> None:
    client, store, sessions, _topic = harness
    sessions.who = (ACCOUNT, PHONE)
    good = _headers(client, (ACCOUNT, PHONE))
    for origin in ("https://evil.example", "http://mac.example-tailnet.ts.net", ""):
        headers = {**good, "Origin": origin}
        assert (
            client.put("/v1/push/web/subscription", content=_body(), headers=headers).status_code
            == 403
        )
    no_origin = {k: v for k, v in good.items() if k != "Origin"}
    assert (
        client.put("/v1/push/web/subscription", content=_body(), headers=no_origin).status_code
        == 403
    )
    assert store.subscription(ACCOUNT, PHONE) is None


def test_mutations_without_the_csrf_token_are_refused(
    harness: tuple[TestClient, WebPushStore, Sessions, bytes],
) -> None:
    client, store, sessions, _topic = harness
    sessions.who = (ACCOUNT, PHONE)
    missing = {"Origin": ORIGIN, "Content-Type": "application/json"}
    assert (
        client.put("/v1/push/web/subscription", content=_body(), headers=missing).status_code == 403
    )
    wrong = {**missing, "X-RelayDeck-CSRF": "guessed"}
    assert (
        client.put("/v1/push/web/subscription", content=_body(), headers=wrong).status_code == 403
    )
    assert client.delete("/v1/push/web/subscription", headers=wrong).status_code == 403
    assert store.subscription(ACCOUNT, PHONE) is None


def test_another_device_cannot_use_this_devices_csrf_token(
    harness: tuple[TestClient, WebPushStore, Sessions, bytes],
) -> None:
    client, store, sessions, _topic = harness
    sessions.who = (ACCOUNT, PHONE)
    phone_token = client.get("/v1/push/web/key").json()["csrf"]
    sessions.who = (ACCOUNT, LAPTOP)
    laptop_token = client.get("/v1/push/web/key").json()["csrf"]
    assert phone_token != laptop_token
    sessions.who = (ACCOUNT, LAPTOP)
    response = client.put(
        "/v1/push/web/subscription",
        content=_body(),
        headers={"Origin": ORIGIN, "X-RelayDeck-CSRF": phone_token},
    )
    assert response.status_code == 403
    assert store.subscription(ACCOUNT, LAPTOP) is None


def test_a_session_that_has_gone_is_refused_everywhere(
    harness: tuple[TestClient, WebPushStore, Sessions, bytes],
) -> None:
    client, store, sessions, _topic = harness
    sessions.who = (ACCOUNT, PHONE)
    client.put(
        "/v1/push/web/subscription", content=_body(), headers=_headers(client, (ACCOUNT, PHONE))
    )
    sessions.who = None  # logged out or revoked
    headers = {"Origin": ORIGIN, "X-RelayDeck-CSRF": "x"}
    assert client.get("/v1/push/web/key").status_code == 401
    assert (
        client.put("/v1/push/web/subscription", content=_body(), headers=headers).status_code == 401
    )
    assert client.delete("/v1/push/web/subscription", headers=headers).status_code == 401
    assert store.subscription(ACCOUNT, PHONE) is not None  # nothing changed without a session


def test_unsubscribe_removes_the_device_subscription_and_its_queued_work(
    harness: tuple[TestClient, WebPushStore, Sessions, bytes],
) -> None:
    client, store, sessions, _topic = harness
    sessions.who = (ACCOUNT, PHONE)
    client.put(
        "/v1/push/web/subscription", content=_body(), headers=_headers(client, (ACCOUNT, PHONE))
    )
    store.enqueue(ACCOUNT, PHONE, "event-1", "attention", now=int(NOW * 1000))
    response = client.delete(
        "/v1/push/web/subscription", headers=_headers(client, (ACCOUNT, PHONE))
    )
    assert response.status_code == 204
    assert store.subscription(ACCOUNT, PHONE) is None
    assert store.due(int(NOW * 1000) + 1) == []


def test_an_oversize_body_is_refused_before_parsing(
    harness: tuple[TestClient, WebPushStore, Sessions, bytes],
) -> None:
    client, store, sessions, _topic = harness
    sessions.who = (ACCOUNT, PHONE)
    padded = _body(extra="x" * 3000)
    response = client.put(
        "/v1/push/web/subscription", content=padded, headers=_headers(client, (ACCOUNT, PHONE))
    )
    assert response.status_code == 413
    assert store.subscription(ACCOUNT, PHONE) is None


def test_a_refused_endpoint_gets_a_generic_answer_that_does_not_echo_it(
    harness: tuple[TestClient, WebPushStore, Sessions, bytes],
) -> None:
    client, store, sessions, _topic = harness
    sessions.who = (ACCOUNT, PHONE)
    bad = "https://evilfcm.googleapis.com/fcm/send/abc"
    response = client.put(
        "/v1/push/web/subscription", content=_body(bad), headers=_headers(client, (ACCOUNT, PHONE))
    )
    assert response.status_code == 400
    assert response.json() == {"error": {"code": "invalid"}}
    assert bad not in response.text
    assert store.subscription(ACCOUNT, PHONE) is None


def test_registrations_are_rate_limited_per_device(
    harness: tuple[TestClient, WebPushStore, Sessions, bytes],
) -> None:
    client, _store, sessions, _topic = harness
    sessions.who = (ACCOUNT, PHONE)
    headers = _headers(client, (ACCOUNT, PHONE))
    statuses = [
        client.put("/v1/push/web/subscription", content=_body(), headers=headers).status_code
        for _ in range(webpush_routes.DEVICE_LIMIT + 1)
    ]
    assert statuses[-1] == 429
    assert statuses[:-1] == [201] * webpush_routes.DEVICE_LIMIT


def test_the_tap_check_returns_a_category_only_for_the_owning_device(
    harness: tuple[TestClient, WebPushStore, Sessions, bytes],
) -> None:
    client, store, sessions, _topic = harness
    sessions.who = (ACCOUNT, PHONE)
    client.put(
        "/v1/push/web/subscription", content=_body(), headers=_headers(client, (ACCOUNT, PHONE))
    )
    route = store.enqueue(
        ACCOUNT,
        PHONE,
        "event-1",
        "completion",
        now=int(NOW * 1000),
        host="home-mac",
        session="sess-1",
    )
    assert route is not None
    ok = client.post(
        "/v1/push/web/resolve",
        content=json.dumps({"route": route}).encode(),
        headers=_headers(client, (ACCOUNT, PHONE)),
    )
    assert ok.status_code == 200
    assert ok.json() == {
        "category": "completion",
        "host_id": "home-mac",
        "session_id": "sess-1",
    }
    sessions.who = (ACCOUNT, LAPTOP)
    other = client.post(
        "/v1/push/web/resolve",
        content=json.dumps({"route": route}).encode(),
        headers=_headers(client, (ACCOUNT, LAPTOP)),
    )
    assert other.status_code == 404


def test_the_tap_check_needs_csrf_and_a_session_too(
    harness: tuple[TestClient, WebPushStore, Sessions, bytes],
) -> None:
    client, _store, sessions, _topic = harness
    body = json.dumps({"route": "wp_anything"}).encode()
    assert client.post("/v1/push/web/resolve", content=body).status_code == 401
    sessions.who = (ACCOUNT, PHONE)
    assert client.post("/v1/push/web/resolve", content=body).status_code == 403


def test_the_public_key_response_carries_the_csrf_token_and_the_topic_is_not_exposed(
    harness: tuple[TestClient, WebPushStore, Sessions, bytes],
) -> None:
    client, _store, sessions, topic = harness
    sessions.who = (ACCOUNT, PHONE)
    body = client.get("/v1/push/web/key").text
    assert webpush.b64u(topic) not in body
    assert topic_for(topic, PHONE, "wp_x") not in body


def test_a_bearer_tap_check_needs_no_cookie_csrf_because_a_bearer_is_never_ambient(
    harness: tuple[TestClient, WebPushStore, Sessions, bytes],
) -> None:
    client, store, sessions, _topic = harness
    sessions.who = (ACCOUNT, PHONE)
    p256dh, auth = _keys()
    store.put_subscription(ACCOUNT, PHONE, ENDPOINT, p256dh, auth, int(NOW * 1000))
    route = store.enqueue(
        ACCOUNT, PHONE, "event-b", "attention", now=int(NOW * 1000), host="home-mac", session="s"
    )
    assert route is not None
    body = json.dumps({"route": route}).encode()
    bearer = client.post(
        "/v1/push/web/resolve", content=body, headers={"Authorization": "Bearer synthetic"}
    )
    assert bearer.status_code == 200
    assert bearer.json()["host_id"] == "home-mac"
    no_credentials = client.post("/v1/push/web/resolve", content=body)
    assert no_credentials.status_code == 403  # the cookie path still needs Origin and CSRF


def test_the_key_response_says_whether_this_device_is_subscribed_and_nothing_more(
    harness: tuple[TestClient, WebPushStore, Sessions, bytes],
) -> None:
    client, _store, sessions, _topic = harness
    sessions.who = (ACCOUNT, PHONE)
    assert client.get("/v1/push/web/key").json()["subscribed"] is False
    client.put(
        "/v1/push/web/subscription", content=_body(), headers=_headers(client, (ACCOUNT, PHONE))
    )
    doc = client.get("/v1/push/web/key").json()
    assert doc["subscribed"] is True
    assert set(doc) == {"publicKey", "enabled", "subscribed", "csrf", "choice"}
    assert ENDPOINT not in client.get("/v1/push/web/key").text
    client.delete("/v1/push/web/subscription", headers={"Authorization": "Bearer synthetic"})
    assert client.get("/v1/push/web/key").json()["subscribed"] is False
