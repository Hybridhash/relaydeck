"""Notification preferences on the web route and the HTTP preference endpoints.

Real stores in a temp state dir; mocked provider; no network.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import FastAPI
from fastapi.testclient import TestClient

from relaydeck.companion.runtime import CompanionRuntime
from relaydeck.gateway import webpush, webpush_routes
from relaydeck.gateway.push import ProviderOutcome, PushStore
from relaydeck.gateway.webpush_store import WebPushStore, pump

ORIGIN = "https://mac.example-tailnet.ts.net"
ACCOUNT = "acct-one"
PHONE = "dev-phone"
LAPTOP = "dev-laptop"
ENDPOINT = "https://fcm.googleapis.com/fcm/send/synthetic-endpoint-1"
NOW = 1_700_000_000_000


def _keys() -> tuple[str, str]:
    priv = ec.generate_private_key(ec.SECP256R1())
    pub = priv.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    return webpush.b64u(pub), webpush.b64u(b"\x06" * 16)


class Recorder:
    def __init__(self) -> None:
        self.sends: list[dict[str, str]] = []

    def send(self, token: str, payload: dict[str, str]) -> ProviderOutcome:
        self.sends.append(dict(payload))
        return ProviderOutcome.SENT


@pytest.fixture
def state(tmp_path: Path) -> Path:
    directory = tmp_path / "state"
    directory.mkdir(mode=0o700)
    return directory


def _subscribe(store: WebPushStore, device: str) -> None:
    p256dh, auth = _keys()
    store.put_subscription(ACCOUNT, device, ENDPOINT, p256dh, auth, NOW)


# -- the enqueue and send paths -------------------------------------------------------------------


def test_the_web_enqueue_skips_a_category_the_device_turned_off(state: Path) -> None:
    store = WebPushStore(state, clock=lambda: NOW)
    _subscribe(store, PHONE)
    push = PushStore()
    push.set_preferences(ACCOUNT, PHONE, {"completion"}, NOW)
    fake = SimpleNamespace(
        webpush_store=store,
        gateway=SimpleNamespace(now_ms=lambda: NOW, push=push),
    )
    CompanionRuntime._webpush_enqueue(
        fake, ACCOUNT, frozenset({PHONE}), "ev-1", "attention", "h", "s"
    )
    assert store.due(NOW) == []  # blocked is off on this device: nothing queued
    CompanionRuntime._webpush_enqueue(
        fake, ACCOUNT, frozenset({PHONE}), "ev-2", "completion", "h", "s"
    )
    queued = store.due(NOW)
    assert [row["category"] for row in queued] == ["completion"]
    store.close()


def test_a_change_after_enqueue_drops_the_message_without_sending(state: Path) -> None:
    store = WebPushStore(state, clock=lambda: NOW)
    _subscribe(store, PHONE)
    store.enqueue(ACCOUNT, PHONE, "ev-1", "attention", now=NOW)
    push = PushStore()
    push.set_preferences(ACCOUNT, PHONE, {"completion"}, NOW)  # changed between enqueue and send
    provider = Recorder()
    report = pump(
        store,
        provider,
        topic_key=b"k" * 32,
        now=NOW,
        allowed=push.category_allowed,
    )
    assert provider.sends == []
    assert report["dropped"] == 1
    assert store.due(NOW) == []
    store.close()


def test_the_default_web_message_is_still_sent(state: Path) -> None:
    store = WebPushStore(state, clock=lambda: NOW)
    _subscribe(store, PHONE)
    store.enqueue(ACCOUNT, PHONE, "ev-1", "attention", now=NOW)
    provider = Recorder()
    report = pump(
        store, provider, topic_key=b"k" * 32, now=NOW, allowed=PushStore().category_allowed
    )
    assert report["sent"] == 1 and len(provider.sends) == 1
    store.close()


# -- the HTTP endpoints ----------------------------------------------------------------------


class Sessions:
    def __init__(self) -> None:
        self.who: tuple[str, str] | None = None

    def __call__(self, request: Any) -> tuple[str, str] | None:
        return self.who


@pytest.fixture
def harness(state: Path) -> Iterator[tuple[TestClient, Sessions, PushStore, bytes]]:
    store = WebPushStore(state, clock=lambda: NOW)
    key = webpush.load_or_create_vapid(state)
    topic = webpush.load_or_create_topic_key(state)
    sessions = Sessions()
    push = PushStore()
    app = FastAPI()
    app.include_router(
        webpush_routes.build_router(
            store=store,
            identity=sessions,
            origin=ORIGIN,
            vapid_key=key,
            topic_key=topic,
            settings=webpush_routes.WebPushSettings(),
            preferences=push,
            clock=lambda: NOW / 1000,
        )
    )
    client = TestClient(app, base_url=ORIGIN)
    yield client, sessions, push, topic
    store.close()


def _csrf(client: TestClient, who: tuple[str, str]) -> dict[str, str]:
    token = client.get("/v1/push/web/key").json()["csrf"]
    return {"Origin": ORIGIN, "X-RelayDeck-CSRF": token, "Content-Type": "application/json"}


def test_the_default_choice_is_all_and_the_wire_uses_blocked_and_done(
    harness: tuple[TestClient, Sessions, PushStore, bytes],
) -> None:
    client, sessions, _push, _topic = harness
    sessions.who = (ACCOUNT, PHONE)
    response = client.get("/v1/push/prefs")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {"categories": ["blocked", "done"]}


@pytest.mark.parametrize(
    ("body", "stored"),
    [
        (["blocked"], {"attention"}),
        (["done"], {"completion"}),
        (["blocked", "done"], {"attention", "completion"}),
        ([], set()),
    ],
)
def test_each_choice_is_saved_and_read_back(
    harness: tuple[TestClient, Sessions, PushStore, bytes],
    body: list[str],
    stored: set[str],
) -> None:
    client, sessions, push, _topic = harness
    sessions.who = (ACCOUNT, PHONE)
    response = client.put(
        "/v1/push/prefs",
        content=json.dumps({"categories": body}).encode(),
        headers=_csrf(client, (ACCOUNT, PHONE)),
    )
    assert response.status_code == 200
    assert push.preferences(ACCOUNT, PHONE) == frozenset(stored)
    assert client.get("/v1/push/prefs").json() == {"categories": sorted(body)}


@pytest.mark.parametrize(
    "body",
    [
        {"categories": ["attention"]},  # the internal name is not a UI choice
        {"categories": ["blocked", "blocked"]},
        {"categories": ["blocked", "secret"]},
        {"categories": "blocked"},
        {"categories": [7]},
        {"categories": [], "extra": 1},
        {"other": []},
        ["blocked"],
    ],
)
def test_unknown_or_malformed_choices_are_refused(
    harness: tuple[TestClient, Sessions, PushStore, bytes], body: Any
) -> None:
    client, sessions, push, _topic = harness
    sessions.who = (ACCOUNT, PHONE)
    before = push.preferences(ACCOUNT, PHONE)
    response = client.put(
        "/v1/push/prefs", content=json.dumps(body).encode(), headers=_csrf(client, (ACCOUNT, PHONE))
    )
    assert response.status_code == 400
    assert push.preferences(ACCOUNT, PHONE) == before


def test_an_oversize_body_is_refused_before_parsing(
    harness: tuple[TestClient, Sessions, PushStore, bytes],
) -> None:
    client, sessions, _push, _topic = harness
    sessions.who = (ACCOUNT, PHONE)
    response = client.put(
        "/v1/push/prefs", content=b" " * 2000, headers=_csrf(client, (ACCOUNT, PHONE))
    )
    assert response.status_code == 413


def test_the_choice_is_for_this_device_only(
    harness: tuple[TestClient, Sessions, PushStore, bytes],
) -> None:
    client, sessions, push, _topic = harness
    sessions.who = (ACCOUNT, LAPTOP)
    client.put(
        "/v1/push/prefs", content=b'{"categories": []}', headers=_csrf(client, (ACCOUNT, LAPTOP))
    )
    sessions.who = (ACCOUNT, PHONE)
    assert client.get("/v1/push/prefs").json() == {"categories": ["blocked", "done"]}
    assert push.preferences(ACCOUNT, LAPTOP) == frozenset()


def test_a_cookie_write_needs_the_origin_and_the_csrf_token(
    harness: tuple[TestClient, Sessions, PushStore, bytes],
) -> None:
    client, sessions, push, _topic = harness
    sessions.who = (ACCOUNT, PHONE)
    body = b'{"categories": []}'
    assert client.put("/v1/push/prefs", content=body).status_code == 403
    assert (
        client.put(
            "/v1/push/prefs",
            content=body,
            headers={"Origin": "https://evil.example", "X-RelayDeck-CSRF": "x"},
        ).status_code
        == 403
    )
    assert (
        client.put(
            "/v1/push/prefs",
            content=body,
            headers={"Origin": ORIGIN, "X-RelayDeck-CSRF": "guessed"},
        ).status_code
        == 403
    )
    assert push.preferences(ACCOUNT, PHONE) == frozenset({"attention", "completion"})


def test_a_bearer_write_needs_no_cookie_csrf(
    harness: tuple[TestClient, Sessions, PushStore, bytes],
) -> None:
    client, sessions, push, _topic = harness
    sessions.who = (ACCOUNT, PHONE)
    response = client.put(
        "/v1/push/prefs",
        content=b'{"categories": ["done"]}',
        headers={"Authorization": "Bearer synthetic", "Content-Type": "application/json"},
    )
    assert response.status_code == 200
    assert push.preferences(ACCOUNT, PHONE) == frozenset({"completion"})


def test_without_a_session_nothing_is_read_or_written(
    harness: tuple[TestClient, Sessions, PushStore, bytes],
) -> None:
    client, _sessions, push, _topic = harness
    assert client.get("/v1/push/prefs").status_code == 401
    assert client.put("/v1/push/prefs", content=b'{"categories": []}').status_code == 401
    assert push.preferences(ACCOUNT, PHONE) == frozenset({"attention", "completion"})
