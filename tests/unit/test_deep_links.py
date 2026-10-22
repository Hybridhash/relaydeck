"""deep-link mint/resolve contract.

Mint on notification (opaque, single-purpose, expiring) + authenticated
resolution. Logged-out, revoked, forged, dead, expired, and
foreign-account links fail closed with no content. Real-device proof
(foreground/background/killed, Doze) is environment-blocked: no
Flutter/Dart, JDK, Android SDK, or Firebase project here.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from relaydeck.gateway.app import GatewayState, create_app
from relaydeck.gateway.backend import FakeBackend
from relaydeck.gateway.push import LINK_PURPOSE_OPEN, PushStore

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "gateway"
SECRET = b"deep-link-test-secret-32bytes!!!!"
HOSTS = frozenset({"home-mac"})


class Clock:
    def __init__(self, now: int = 1_000_000) -> None:
        self.now = now

    def __call__(self) -> int:
        return self.now


def _state(clock: Clock | None = None) -> tuple[GatewayState, Clock]:
    clock = clock or Clock()
    backend = FakeBackend(now_ms=clock)
    doc: dict[str, Any] = json.loads((FIXTURES / "snapshot.json").read_text())
    doc["cursor"] = "snap-cursor-1"
    backend.snapshots["home-mac"] = doc
    state = GatewayState(backend=backend, cursor_secret=SECRET, now_ms=clock, push=PushStore())
    return state, clock


def _token(
    state: GatewayState,
    account: str = "acct-1",
    caps: frozenset[str] = frozenset(),
    device_id: str = "install-1",
) -> str:
    access, _ = state.sessions.create(account, caps, HOSTS, state.now_ms(), device_id)
    return access


def _notify(state: GatewayState, event_id: str = "ev-1", **kwargs: Any) -> dict[str, Any]:
    state.push.register_device("acct-1", "install-1", "fcm-tok-1", "android", 1_000_000)
    rows = state.push.notify(
        account="acct-1",
        account_hosts=HOSTS,
        host_id="home-mac",
        session_id="term-1",
        event_id=event_id,
        category="attention",
        now=1_000_000,
        **kwargs,
    )
    assert len(rows) == 1
    return rows[0]


def test_mint_resolve_round_trip() -> None:
    state, _ = _state()
    row = _notify(state)
    resolved = state.push.resolve_link(row["link_token"], "acct-1", 1_000_000)
    assert resolved is not None
    assert resolved["notification_id"] == row["notification_id"]
    assert resolved["host_id"] == "home-mac"
    assert resolved["session_id"] == "term-1"
    assert resolved["route"] == {"host_id": "home-mac", "session_id": "term-1"}
    assert resolved["category"] == "attention"


def test_http_resolve_by_token() -> None:
    state, _ = _state()
    client = TestClient(create_app(state))
    row = _notify(state)
    headers = {"Authorization": f"Bearer {_token(state)}"}
    r = client.post("/v1/deep-links/resolve", headers=headers, json={"token": row["link_token"]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["host_id"] == "home-mac"
    assert body["session_id"] == "term-1"
    assert body["notification_id"] == row["notification_id"]


def test_http_resolve_by_notification_id() -> None:
    state, _ = _state()
    client = TestClient(create_app(state))
    row = _notify(state)
    headers = {"Authorization": f"Bearer {_token(state)}"}
    r = client.post(
        "/v1/deep-links/resolve",
        headers=headers,
        json={"notification_id": row["notification_id"]},
    )
    assert r.status_code == 200, r.text
    assert r.json()["host_id"] == "home-mac"


def test_resolved_ids_drive_followup_fetch() -> None:
    state, _ = _state()
    client = TestClient(create_app(state))
    row = _notify(state)
    token = _token(state, caps=frozenset({"sessions.read"}))
    headers = {"Authorization": f"Bearer {token}"}
    resolved = client.post(
        "/v1/deep-links/resolve", headers=headers, json={"token": row["link_token"]}
    ).json()
    snap = client.get(f"/v1/hosts/{resolved['host_id']}/snapshot", headers=headers)
    assert snap.status_code == 200


def test_expired_link_fails_closed() -> None:
    state, clock = _state()
    client = TestClient(create_app(state))
    row = _notify(state, ttl_ms=60_000)
    headers = {"Authorization": f"Bearer {_token(state)}"}
    ok = client.post("/v1/deep-links/resolve", headers=headers, json={"token": row["link_token"]})
    assert ok.status_code == 200
    clock.now += 61_000
    # New session: the old access token (15 min TTL) is still live, so the
    # 404 below proves link expiry, not session expiry.
    headers = {"Authorization": f"Bearer {_token(state)}"}
    r = client.post("/v1/deep-links/resolve", headers=headers, json={"token": row["link_token"]})
    assert r.status_code == 404
    assert r.json()["code"] == "not_found"


def test_forged_and_dead_links_carry_no_content() -> None:
    state, _ = _state()
    client = TestClient(create_app(state))
    row = _notify(state)
    headers = {"Authorization": f"Bearer {_token(state)}"}
    forged = "rdl_" + "A" * 43
    dead = "rdl_" + "z" * 43
    assert forged != row["link_token"]
    for token in (forged, dead, "not-even-a-token"):
        r = client.post("/v1/deep-links/resolve", headers=headers, json={"token": token})
        assert r.status_code == 404, token
        assert set(r.json()) == {"code", "message", "retryable"}
        blob = json.dumps(r.json())
        assert "home-mac" not in blob and "term-1" not in blob and "ev-1" not in blob


def test_logged_out_cannot_resolve() -> None:
    state, _ = _state()
    client = TestClient(create_app(state))
    row = _notify(state)
    assert (
        client.post("/v1/deep-links/resolve", json={"token": row["link_token"]}).status_code == 401
    )
    token = _token(state)
    headers = {"Authorization": f"Bearer {token}"}
    assert client.post("/v1/logout", headers=headers).status_code == 200
    r = client.post("/v1/deep-links/resolve", headers=headers, json={"token": row["link_token"]})
    assert r.status_code == 401


def test_foreign_account_cannot_resolve() -> None:
    state, _ = _state()
    client = TestClient(create_app(state))
    row = _notify(state)
    other = _token(state, account="acct-2")
    r = client.post(
        "/v1/deep-links/resolve",
        headers={"Authorization": f"Bearer {other}"},
        json={"token": row["link_token"]},
    )
    assert r.status_code == 404
    r = client.post(
        "/v1/deep-links/resolve",
        headers={"Authorization": f"Bearer {other}"},
        json={"notification_id": row["notification_id"]},
    )
    assert r.status_code == 404


def test_revoked_device_links_fail() -> None:
    state, _ = _state()
    client = TestClient(create_app(state))
    row = _notify(state)
    headers = {"Authorization": f"Bearer {_token(state)}"}
    assert (
        client.post(
            "/v1/deep-links/resolve", headers=headers, json={"token": row["link_token"]}
        ).status_code
        == 200
    )
    assert client.delete("/v1/devices/install-1", headers=headers).status_code == 200
    assert (
        client.post(
            "/v1/deep-links/resolve", headers=headers, json={"token": row["link_token"]}
        ).status_code
        == 401
    )
    assert (
        client.post(
            "/v1/deep-links/resolve",
            headers=headers,
            json={"notification_id": row["notification_id"]},
        ).status_code
        == 401
    )


def test_replaced_device_links_fail() -> None:
    state, _ = _state()
    row = _notify(state)
    assert state.push.resolve_link(str(row["link_token"]), "acct-1", 1_000_000) is not None
    state.push.replace_device("acct-1", "install-1", "install-2", "fcm-tok-2", "android")
    assert state.push.resolve_link(str(row["link_token"]), "acct-1", 1_000_000) is None
    assert state.push.resolve_hint("acct-1", str(row["notification_id"]), 1_000_000) is None


def test_wrong_purpose_fails_closed() -> None:
    state, _ = _state()
    row = _notify(state)
    token = str(row["link_token"])
    assert state.push.resolve_link(token, "acct-1", 1_000_000) is not None
    # Fault injection: a link minted for another purpose must not open.
    with state.push._lock:  # noqa: SLF001 - white-box purpose tamper
        state.push._db.execute("UPDATE push_links SET purpose='other' WHERE token=?", (token,))
        state.push._commit()
    assert LINK_PURPOSE_OPEN == "open-event"
    assert state.push.resolve_link(token, "acct-1", 1_000_000) is None


def test_links_are_re_resolvable_not_single_use() -> None:
    state, _ = _state()
    row = _notify(state)
    token = str(row["link_token"])
    first = state.push.resolve_link(token, "acct-1", 1_000_000)
    second = state.push.resolve_link(token, "acct-1", 1_000_000)
    assert first is not None and second is not None
    assert first == second


def test_tokens_and_refs_are_opaque() -> None:
    state, _ = _state()
    adversarial = "SECRET-CANARY-ev-9f8e7d6c5b4a"
    state.push.register_device("acct-1", "install-1", "fcm-tok-1", "android", 1_000_000)
    rows = state.push.notify(
        account="acct-1",
        account_hosts=HOSTS,
        host_id="home-mac",
        session_id=f"term-{adversarial}",
        event_id=adversarial,
        category="attention",
        now=1_000_000,
    )
    # The internal row stores event_id server-side by design; the token,
    # refs, and wire payload -- everything that leaves the server -- must
    # not contain it.
    assert len(rows) == 1
    exposed = json.dumps(
        {
            "link_token": rows[0]["link_token"],
            "event_ref": rows[0]["event_ref"],
            "host_ref": rows[0]["host_ref"],
            "session_ref": rows[0]["session_ref"],
        }
    )
    assert adversarial not in exposed
    payload = state.push.hint_payload(str(rows[0]["notification_id"]))
    assert adversarial not in json.dumps(payload)


def test_malformed_resolve_bodies_fail() -> None:
    state, _ = _state()
    client = TestClient(create_app(state))
    row = _notify(state)
    headers = {"Authorization": f"Bearer {_token(state)}"}
    bad_bodies: list[Any] = [
        {},
        {"token": row["link_token"], "notification_id": row["notification_id"]},
        {"token": ""},
        {"notification_id": ""},
        {"token": 42},
        {"bogus": "x"},
        {"token": "rdl_" + "A" * 300},
        [],
    ]
    for body in bad_bodies:
        r = client.post("/v1/deep-links/resolve", headers=headers, json=body)
        assert r.status_code == 400, body
        assert r.json()["code"] == "forbidden"
