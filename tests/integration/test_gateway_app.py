"""gateway HTTP/WebSocket behavior over a fake backend."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from relaydeck.contracts.schemas import validate
from relaydeck.contracts.wire import terminal as terminal_wire
from relaydeck.gateway.app import GatewayState, create_app
from relaydeck.gateway.backend import FakeBackend
from relaydeck.gateway.cursors import encode_cursor
from relaydeck.gateway.identity import pkce_challenge

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "gateway"
SECRET = b"integration-test-secret-32bytes!!"
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


def _snapshot_doc(cursor: str) -> dict[str, Any]:
    doc: dict[str, Any] = json.loads((FIXTURES / "snapshot.json").read_text())
    doc["cursor"] = cursor
    return doc


def _client(now: int = 1_000_000) -> tuple[TestClient, GatewayState, FakeBackend]:
    backend = FakeBackend(now_ms=lambda: now)
    backend.snapshots["home-mac"] = _snapshot_doc("snap-cursor-1")
    backend.event_logs["home-mac"] = [
        {
            "protocol": "relaydeck.gateway.v1",
            "event_id": f"ev-{i}",
            "host_id": "home-mac",
            "host_epoch": "epoch-7",
            "sequence": i,
            "cursor": f"c{i}",
            "type": "agent.upserted",
            "resource_id": "term-1",
            "resource_revision": 10 + i,
            "observed_at": "2026-10-03T06:20:00Z",
            "payload": {"status": "working"},
        }
        for i in (1, 2, 3)
    ]
    state = GatewayState(backend=backend, cursor_secret=SECRET, now_ms=lambda: now)
    return TestClient(create_app(state)), state, backend


def _token(state: GatewayState, caps: frozenset[str] = ALL_CAPS) -> str:
    access, _ = state.sessions.create("acct-1", caps, frozenset({"home-mac"}), 1_000_000)
    return access


def _ticket(client: Any, token: str, lease_id: str) -> str:
    r = client.post(
        "/v1/terminal-tickets",
        headers={"Authorization": f"Bearer {token}"},
        json={"lease_id": lease_id},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    validate("relaydeck/gateway/v1/terminal-ticket", body)
    ticket: str = body["ticket"]
    return ticket


def test_healthz() -> None:
    client, _, _ = _client()
    assert client.get("/healthz").json() == {
        "ok": True,
        "version": "1.0.0",
        "push": {"configured": False, "delivery_error": "", "enqueue_error": ""},
    }


def test_snapshot_requires_auth_and_capability() -> None:
    client, state, _ = _client()
    assert client.get("/v1/hosts/home-mac/snapshot").status_code == 401
    weak = _token(state, frozenset({"terminal.view"}))
    r = client.get("/v1/hosts/home-mac/snapshot", headers={"Authorization": f"Bearer {weak}"})
    assert r.status_code == 403
    assert r.json()["code"] == "unsupported_capability"


def test_snapshot_ok_and_valid() -> None:
    client, state, _ = _client()
    token = _token(state)
    r = client.get("/v1/hosts/home-mac/snapshot", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    validate("relaydeck/gateway/v1/snapshot", r.json())


def test_unknown_host_maps() -> None:
    client, state, _ = _client()
    token = _token(state)
    r = client.get("/v1/hosts/nope/snapshot", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 404
    assert r.json()["code"] == "not_found"


def test_stream_resume_with_cursor() -> None:
    # The stream is infinite (live journal + heartbeats). Neither this
    # TestClient nor ASGITransport streams an endless body (both buffer
    # it), so the live read runs against a real loopback server.
    import httpx

    from tests.support.live_server import LiveGateway

    _, state, backend = _client()
    token = _token(state)
    cursor = encode_cursor(SECRET, {"host_id": "home-mac", "sequence": 2})
    with LiveGateway(create_app(state)) as live:
        with httpx.Client(base_url=live.base_url, timeout=10.0) as http:
            with http.stream(
                "GET",
                f"/v1/events?host_id=home-mac&cursor={cursor}",
                headers={"Authorization": f"Bearer {token}"},
            ) as r:
                assert r.status_code == 200
                assert "text/event-stream" in r.headers["content-type"]
                reader = _FrameReader(r.iter_text())
                bodies = reader.take(1)
                assert [b["sequence"] for b in bodies] == [3]
                for body in bodies:
                    validate("relaydeck/gateway/v1/metadata-event", body)
                template = dict(backend.event_logs["home-mac"][0])
                template.pop("sequence", None)
                live_event = backend.publish("home-mac", template)
                bodies = reader.take(1)
                assert [b["sequence"] for b in bodies] == [live_event["sequence"]]
                assert len(backend._followers.get("home-mac", [])) == 1
    assert len(backend._followers.get("home-mac", [])) == 0


class _FrameReader:
    """Incremental SSE data-frame reader over one response iterator."""

    def __init__(self, chunks: Any) -> None:
        self._chunks = chunks
        self._buf = ""

    def take(self, count: int) -> list[dict[str, Any]]:
        bodies: list[dict[str, Any]] = []
        for chunk in self._chunks:
            self._buf += chunk
            while "\n\n" in self._buf and len(bodies) < count:
                frame, self._buf = self._buf.split("\n\n", 1)
                for line in frame.splitlines():
                    if line.startswith("data: "):
                        bodies.append(json.loads(line[6:]))
            if len(bodies) >= count:
                break
        assert len(bodies) == count
        return bodies


def test_stream_bad_cursor_fails_closed() -> None:
    client, state, _ = _client()
    token = _token(state)
    r = client.get(
        "/v1/events?host_id=home-mac&cursor=forged",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 409
    assert r.json()["code"] == "stale_epoch"
    assert r.json()["details"] == {"reason": "cursor"}
    other = encode_cursor(SECRET, {"host_id": "other", "sequence": 1})
    r = client.get(
        f"/v1/events?host_id=home-mac&cursor={other}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.json()["code"] == "stale_epoch"


def test_commands_submit_dedupe_conflict() -> None:
    client, state, _ = _client()
    token = _token(state)
    headers = {"Authorization": f"Bearer {token}"}
    body = json.loads((FIXTURES / "command-submit.json").read_text())
    body["expires_at"] = "1970-01-01T00:17:40Z"  # now + 100s on the test clock
    r = client.post("/v1/hosts/home-mac/commands", headers=headers, json=body)
    assert r.status_code == 202
    validate("relaydeck/gateway/v1/command-record", r.json())
    assert r.json()["target"] == {"agent_id": "term-1"}
    r = client.post("/v1/hosts/home-mac/commands", headers=headers, json=body)
    assert r.status_code == 202  # idempotent redelivery
    bad = dict(body, payload={"text": "different"})
    r = client.post("/v1/hosts/home-mac/commands", headers=headers, json=bad)
    assert r.status_code == 409
    assert r.json()["code"] == "command_conflict"
    bad = dict(body, target={"agent_id": "term-9"})
    r = client.post("/v1/hosts/home-mac/commands", headers=headers, json=bad)
    assert r.json()["code"] == "command_conflict"
    r = client.get(f"/v1/hosts/home-mac/commands/{body['command_id']}", headers=headers)
    assert r.status_code == 200
    r = client.get("/v1/hosts/home-mac/commands/missing", headers=headers)
    assert r.json()["code"] == "not_found"


def test_commands_policy_denials() -> None:
    client, state, _ = _client()
    weak = _token(state, frozenset({"sessions.read"}))
    headers = {"Authorization": f"Bearer {weak}"}
    body = json.loads((FIXTURES / "command-submit.json").read_text())
    body["expires_at"] = "1970-01-01T00:17:40Z"
    r = client.post("/v1/hosts/home-mac/commands", headers=headers, json=body)
    assert r.json()["code"] == "unsupported_capability"
    token = _token(state)
    headers = {"Authorization": f"Bearer {token}"}
    bad = dict(body, kind="agent.approve")
    r = client.post("/v1/hosts/home-mac/commands", headers=headers, json=bad)
    assert r.status_code == 400
    bad = dict(body, payload={"text": "x" * 20000})
    r = client.post("/v1/hosts/home-mac/commands", headers=headers, json=bad)
    assert r.json()["code"] == "too_large"


def test_token_exchange_pkce_and_refresh() -> None:
    client, state, _ = _client()
    verifier = "v" * 64
    state.pending_codes["code-abc-12345"] = {
        "challenge": pkce_challenge(verifier),
        "account": "acct-1",
        "capabilities": ALL_CAPS,
        "hosts": frozenset({"home-mac"}),
    }
    r = client.post(
        "/v1/token",
        json={"grant": "authorization_code", "code": "code-abc-12345", "verifier": verifier},
    )
    assert r.status_code == 200
    validate("relaydeck/gateway/v1/token-response", r.json())
    # Single-use code.
    r = client.post(
        "/v1/token",
        json={"grant": "authorization_code", "code": "code-abc-12345", "verifier": verifier},
    )
    assert r.status_code == 401
    refresh = client.post(
        "/v1/token",
        json={"grant": "authorization_code", "code": "code-abc-12345", "verifier": verifier},
    )
    assert refresh.status_code == 401


def test_token_refresh_rotation() -> None:
    client, state, _ = _client()
    access, refresh = state.sessions.create("acct-1", ALL_CAPS, frozenset({"home-mac"}), 1_000_000)
    assert access
    r = client.post("/v1/token", json={"grant": "refresh_token", "refresh_token": refresh})
    assert r.status_code == 200
    r = client.post("/v1/token", json={"grant": "refresh_token", "refresh_token": refresh})
    assert r.status_code == 401  # rotated refresh cannot be reused


def test_terminal_socket_replay_and_input() -> None:
    client, state, backend = _client()
    token = _token(state)
    lease = backend.issue_lease("home-mac", "term-1", "acct-1", 5)
    backend.publish_output(lease.attachment_id, lease.epoch, b"hello ")
    backend.publish_output(lease.attachment_id, lease.epoch, b"world")
    ticket = _ticket(client, token, lease.lease_id)
    assert ticket.startswith("rdt_")
    with client.websocket_connect(f"/v1/terminal/{lease.lease_id}?ticket={ticket}") as ws:
        first = terminal_wire.decode_output(ws.receive_bytes())
        second = terminal_wire.decode_output(ws.receive_bytes())
        assert (first.epoch, first.offset, first.data) == ("e1", 0, b"hello ")
        assert (second.epoch, second.offset, second.data) == ("e1", 6, b"world")
        ws.send_bytes(terminal_wire.encode_text(lease.lease_id, 5, 1, b"x"))
        ws.send_bytes(terminal_wire.encode_key(lease.lease_id, 5, 2, "Enter"))
        ws.send_bytes(terminal_wire.encode_resize(lease.lease_id, 5, 3, 100, 30))
        ws.send_bytes(terminal_wire.encode_text(lease.lease_id, 5, 2, b"dup"))
        ws.send_text(json.dumps({"kind": "bye"}))
    assert backend.input_logs[lease.lease_id] == [
        (5, 1, "text", b"x"),
        (5, 2, "key", b"Enter"),
        (5, 3, "resize", b"\x00d\x00\x1e"),
    ]


def test_terminal_socket_rejects_bad_lease_and_token() -> None:
    from starlette.websockets import WebSocketDisconnect

    client, state, _ = _client()
    token = _token(state)
    try:
        with client.websocket_connect(f"/v1/terminal/nope?ticket={token}"):
            pass
        raise AssertionError("expected disconnect")
    except WebSocketDisconnect as exc:
        assert exc.code == 1008
    try:
        with client.websocket_connect("/v1/terminal/x?ticket=rdt_bogus"):
            pass
        raise AssertionError("expected disconnect")
    except WebSocketDisconnect as exc:
        assert exc.code == 1008


def test_terminal_socket_stale_revision_gaps() -> None:
    client, state, backend = _client()
    token = _token(state)
    lease = backend.issue_lease("home-mac", "term-1", "acct-1", 9)
    ticket = _ticket(client, token, lease.lease_id)
    with client.websocket_connect(f"/v1/terminal/{lease.lease_id}?ticket={ticket}") as ws:
        ws.send_bytes(terminal_wire.encode_text(lease.lease_id, 8, 1, b"late"))
        control = json.loads(ws.receive_text())
        assert control == {
            "kind": "gap",
            "detail": "stale_revision",
            "generation": 8,
            "input_seq": 1,
        }
        ws.send_bytes(terminal_wire.encode_text(lease.lease_id, 9, 1, b"ok"))
        ws.send_text(json.dumps({"kind": "bye"}))
    # A gap ends the reader: even a valid generation arriving afterward is
    # not input authority. Recovery must establish a separately validated reader.
    assert backend.input_logs.get(lease.lease_id, []) == []
    assert backend.resize_log == []


def test_terminal_socket_view_only_cannot_write() -> None:
    from starlette.websockets import WebSocketDisconnect

    client, state, backend = _client()
    token = _token(state, frozenset({"sessions.read", "terminal.view"}))
    lease = backend.issue_lease("home-mac", "term-1", "acct-1", 1)
    ticket = _ticket(client, token, lease.lease_id)
    try:
        with client.websocket_connect(f"/v1/terminal/{lease.lease_id}?ticket={ticket}") as ws:
            ws.send_bytes(terminal_wire.encode_text(lease.lease_id, 1, 1, b"x"))
            ws.receive_text()
        raise AssertionError("expected disconnect")
    except WebSocketDisconnect as exc:
        assert exc.code == 1008


def test_terminal_socket_bad_frame_closes() -> None:
    from starlette.websockets import WebSocketDisconnect

    client, state, backend = _client()
    token = _token(state)
    lease = backend.issue_lease("home-mac", "term-1", "acct-1", 1)
    ticket = _ticket(client, token, lease.lease_id)
    try:
        with client.websocket_connect(f"/v1/terminal/{lease.lease_id}?ticket={ticket}") as ws:
            ws.send_bytes(b"\x99garbage")
            ws.receive_text()
        raise AssertionError("expected disconnect")
    except WebSocketDisconnect as exc:
        assert exc.code == 1008


def _connector_keys() -> Any:
    import base64

    from relaydeck.connector.enrollment import ConnectorKeys
    from relaydeck.connector.pubkey import generate

    seed, public = generate()
    return ConnectorKeys(base64.b64encode(seed).decode(), public)


def _redeem_body(keys: Any, invitation_id: str, code: str, host_id: str) -> dict[str, Any]:
    import base64

    from relaydeck.connector.pubkey import sign_proof

    seed = base64.b64decode(keys.seed_b64)
    proof = sign_proof(seed, invitation_id, host_id)
    return {
        "code": code,
        "host_id": host_id,
        "pubkey": keys.public_openssh,
        "proof": base64.urlsafe_b64encode(proof).decode().rstrip("="),
    }


def _invite(client: Any, token: str, pubkey: str) -> dict[str, Any]:
    r = client.post(
        "/v1/enrollments",
        headers={"Authorization": f"Bearer {token}"},
        json={"host_label": "Home Mac", "pubkey": pubkey},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    validate("relaydeck/gateway/v1/enrollment", body)
    enrollment: dict[str, Any] = body
    return enrollment


def test_enroll_ceremony_and_connector_handshake() -> None:
    from starlette.websockets import WebSocketDisconnect

    from relaydeck.connector.pubkey import parse_qr_payload

    client, state, _ = _client()
    token = _token(state)
    keys = _connector_keys()
    invitation = _invite(client, token, keys.public_openssh)
    assert invitation["fingerprint"] == keys.fingerprint
    qr = parse_qr_payload(invitation["qr"])
    assert (qr["id"], qr["code"]) == (invitation["id"], invitation["code"])

    r = client.post(
        f"/v1/enrollments/{invitation['id']}/redeem",
        json=_redeem_body(keys, invitation["id"], invitation["code"], "home-mac"),
    )
    assert r.status_code == 200, r.text
    validate("relaydeck/gateway/v1/enrollment-result", r.json())
    host_token = r.json()["host_token"]
    assert host_token.startswith("rdh_")
    assert r.json()["fingerprint"] == keys.fingerprint
    record = state.hosts.record_for_host("home-mac")
    assert record is not None and record.account == "acct-1"
    assert record.fingerprint == keys.fingerprint

    # Invitation is single-use (copied QR loses the race).
    r = client.post(
        f"/v1/enrollments/{invitation['id']}/redeem",
        json=_redeem_body(keys, invitation["id"], invitation["code"], "home-mac"),
    )
    assert r.json()["code"] == "not_found"

    # Connector handshake with the host token.
    with client.websocket_connect(f"/v1/connectors?token={host_token}") as ws:
        hello = json.loads(ws.receive_text())
        assert hello == {
            "kind": "hello",
            "host_id": "home-mac",
            "snapshot_chunks": True,
            "epochs": [],
        }
        ws.send_text(json.dumps({"kind": "ping"}))
        assert json.loads(ws.receive_text()) == {"kind": "pong"}
        ws.send_text(json.dumps({"kind": "bye"}))
    # Revoked hosts cannot connect.
    assert state.hosts.revoke("home-mac") is True
    try:
        with client.websocket_connect(f"/v1/connectors?token={host_token}"):
            pass
        raise AssertionError("expected disconnect")
    except WebSocketDisconnect as exc:
        assert exc.code == 1008


def test_enroll_failures() -> None:
    client, state, _ = _client()
    token = _token(state)
    weak = _token(state, frozenset({"sessions.read"}))
    keys = _connector_keys()

    # Creating invitations needs hosts.enroll.
    r = client.post(
        "/v1/enrollments",
        headers={"Authorization": f"Bearer {weak}"},
        json={"host_label": "H", "pubkey": keys.public_openssh},
    )
    assert r.json()["code"] == "unsupported_capability"
    # Malformed pubkeys fail closed.
    r = client.post(
        "/v1/enrollments",
        headers={"Authorization": f"Bearer {token}"},
        json={"host_label": "H", "pubkey": "ssh-rsa AAAA"},
    )
    assert r.status_code == 400

    invitation = _invite(client, token, keys.public_openssh)
    url = f"/v1/enrollments/{invitation['id']}/redeem"
    good = _redeem_body(keys, invitation["id"], invitation["code"], "home-mac")

    # Wrong code guesses fail indistinguishably; five burn the invitation.
    bad = dict(good, code="WRONGCODE123")
    for _ in range(5):
        r = client.post(url, json=bad)
        assert r.json()["code"] == "not_found"
    r = client.post(url, json=good)
    assert r.json()["code"] == "not_found"

    # A different key cannot claim this invitation.
    other = _connector_keys()
    invitation2 = _invite(client, token, keys.public_openssh)
    url2 = f"/v1/enrollments/{invitation2['id']}/redeem"
    swapped = _redeem_body(other, invitation2["id"], invitation2["code"], "home-mac")
    r = client.post(url2, json=swapped)
    assert r.status_code == 400
    assert r.json()["details"] == {"reason": "fingerprint"}

    # A proof bound to another host does not verify for this one.
    forged = _redeem_body(keys, invitation2["id"], invitation2["code"], "evil-mac")
    forged["host_id"] = "home-mac"
    r = client.post(url2, json=forged)
    assert r.status_code == 400
    assert r.json()["details"] == {"reason": "proof"}
    # Corrupted signatures fail the same way.
    corrupt = _redeem_body(keys, invitation2["id"], invitation2["code"], "home-mac")
    corrupt["proof"] = "A" * 86
    r = client.post(url2, json=corrupt)
    assert r.json()["details"] == {"reason": "proof"}

    # Expired invitations are gone.
    stale = state.enrollments.create("acct-1", "Old", keys.public_openssh, keys.fingerprint, 0)
    r = client.post(
        f"/v1/enrollments/{stale.id}/redeem",
        json=_redeem_body(keys, stale.id, stale.code, "home-mac"),
    )
    assert r.json()["code"] == "not_found"


def _submit_body() -> dict[str, Any]:
    body: dict[str, Any] = json.loads((FIXTURES / "command-submit.json").read_text())
    body["expires_at"] = "1970-01-01T00:17:40Z"  # now + 100s on the test clock
    return body


def test_me_returns_principal_and_protocol() -> None:
    client, state, _ = _client()
    token = _token(state)
    r = client.get("/v1/me", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    validate("relaydeck/gateway/v1/me", r.json())
    assert r.json()["account"] == "acct-1"
    assert r.json()["device_id"].startswith("dev_")
    assert r.json()["protocol_version"] == "relaydeck.gateway.v1"
    assert client.get("/v1/me").status_code == 401


def test_hosts_lists_authorized_with_compat() -> None:
    client, state, backend = _client()
    token = _token(state)
    r = client.get("/v1/hosts", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    validate("relaydeck/gateway/v1/hosts", r.json())
    (entry,) = r.json()["hosts"]
    assert entry["host_id"] == "home-mac"
    assert entry["epoch"] == "epoch-7"
    assert entry["reachable"] is True
    assert entry["upstream"] == {"protocol": 22, "version": "0.9.1", "compatible": True}
    backend.offline_hosts.add("home-mac")
    r = client.get("/v1/hosts", headers={"Authorization": f"Bearer {token}"})
    (entry,) = r.json()["hosts"]
    assert entry["reachable"] is False
    assert entry["upstream"]["compatible"] is False


def test_agents_pagination_round_trip() -> None:
    client, state, backend = _client()
    token = _token(state)
    headers = {"Authorization": f"Bearer {token}"}
    agents = backend.snapshots["home-mac"]["agents"]
    while len(agents) < 3:
        clone = dict(agents[0])
        clone["terminal_id"] = f"term-{len(agents) + 1}"
        agents.append(clone)
    r = client.get("/v1/hosts/home-mac/agents?limit=2", headers=headers)
    assert r.status_code == 200
    validate("relaydeck/gateway/v1/agents-page", r.json())
    assert [a["terminal_id"] for a in r.json()["agents"]] == ["term-1", "term-2"]
    page_token = r.json()["next_page_token"]
    r = client.get(f"/v1/hosts/home-mac/agents?limit=2&page_token={page_token}", headers=headers)
    assert [a["terminal_id"] for a in r.json()["agents"]] == ["term-3"]
    assert "next_page_token" not in r.json()
    r = client.get("/v1/hosts/home-mac/agents?page_token=forged", headers=headers)
    assert r.json()["code"] == "stale_epoch"
    r = client.get("/v1/hosts/home-mac/agents?limit=bogus", headers=headers)
    assert r.status_code == 400


def test_submit_rejects_stale_epoch_expired_and_host_mismatch() -> None:
    client, state, _ = _client()
    token = _token(state)
    headers = {"Authorization": f"Bearer {token}"}
    stale = _submit_body()
    stale["host_epoch"] = "epoch-6"
    r = client.post("/v1/hosts/home-mac/commands", headers=headers, json=stale)
    assert r.status_code == 409
    assert r.json()["code"] == "stale_epoch"
    expired = _submit_body()
    expired["expires_at"] = "1970-01-01T00:16:39Z"  # before now
    r = client.post("/v1/hosts/home-mac/commands", headers=headers, json=expired)
    assert r.status_code == 400
    assert r.json()["details"] == {"reason": "expired"}
    far = _submit_body()
    far["expires_at"] = "1970-01-01T02:00:00Z"  # beyond the 5-minute bound
    r = client.post("/v1/hosts/home-mac/commands", headers=headers, json=far)
    assert r.json()["details"] == {"reason": "expiry_bound"}
    mismatch = _submit_body()
    mismatch["host_id"] = "other"
    r = client.post("/v1/hosts/home-mac/commands", headers=headers, json=mismatch)
    assert r.status_code == 400
    assert r.json()["details"] == {"reason": "host_mismatch"}
    missing_epoch = _submit_body()
    del missing_epoch["host_epoch"]
    r = client.post("/v1/hosts/home-mac/commands", headers=headers, json=missing_epoch)
    assert r.status_code == 400


def test_command_status_scoped_to_path_host() -> None:
    client, state, _ = _client()
    access, _ = state.sessions.create(
        "acct-1", ALL_CAPS, frozenset({"home-mac", "other"}), 1_000_000
    )
    headers = {"Authorization": f"Bearer {access}"}
    body = _submit_body()
    r = client.post("/v1/hosts/home-mac/commands", headers=headers, json=body)
    assert r.status_code == 202
    r = client.get(f"/v1/hosts/other/commands/{body['command_id']}", headers=headers)
    assert r.status_code == 404
    assert r.json()["code"] == "not_found"


def test_metadata_event_per_type_rules() -> None:
    from relaydeck.contracts.schemas import SchemaError, validate_metadata_event

    base = json.loads((FIXTURES / "metadata-event.json").read_text())
    validate_metadata_event(base)
    missing_resource = dict(base)
    del missing_resource["resource_id"]
    try:
        validate_metadata_event(missing_resource)
    except SchemaError:
        pass
    else:
        raise AssertionError("expected resource_id rejection")
    updated = dict(base, type="command.updated", resource_id="cmd-1", payload={"status": "bogus"})
    try:
        validate_metadata_event(updated)
    except SchemaError:
        pass
    else:
        raise AssertionError("expected status rejection")
    caps = dict(base, type="capabilities.changed", payload={"capabilities": ["sessions.read"]})
    validate_metadata_event(caps)
    resync = dict(base, type="resync.required")
    validate_metadata_event(resync)


def test_terminal_tickets_single_use_lease_bound_and_expiring() -> None:
    from starlette.websockets import WebSocketDisconnect

    from relaydeck.gateway.identity import TICKET_TTL_MS, TicketGrant

    client, state, backend = _client()
    token = _token(state)
    headers = {"Authorization": f"Bearer {token}"}
    lease = backend.issue_lease("home-mac", "term-1", "acct-1", 1)
    other = backend.issue_lease("home-mac", "term-2", "acct-1", 1)

    # Unknown lease cannot mint.
    r = client.post("/v1/terminal-tickets", headers=headers, json={"lease_id": "nope"})
    assert r.json()["code"] == "lease_expired"
    # View-only sessions may mint (viewing is their purpose).
    weak = _token(state, frozenset({"sessions.read", "terminal.view"}))
    r = client.post(
        "/v1/terminal-tickets",
        headers={"Authorization": f"Bearer {weak}"},
        json={"lease_id": lease.lease_id},
    )
    assert r.status_code == 200

    ticket = _ticket(client, token, lease.lease_id)
    # Cross-lease redeem fails and consumes the ticket.
    assert state.tickets.redeem(ticket, other.lease_id, 1_000_000) is None
    assert state.tickets.redeem(ticket, lease.lease_id, 1_000_000) is None

    fresh = _ticket(client, token, lease.lease_id)
    with client.websocket_connect(f"/v1/terminal/{lease.lease_id}?ticket={fresh}") as ws:
        ws.send_text(json.dumps({"kind": "bye"}))
    # Replayed after redeem: rejected.
    try:
        with client.websocket_connect(f"/v1/terminal/{lease.lease_id}?ticket={fresh}"):
            pass
        raise AssertionError("expected disconnect")
    except WebSocketDisconnect as exc:
        assert exc.code == 1008

    # Expired tickets do not redeem.
    grant = TicketGrant(
        "acct-1", "dev-1", ALL_CAPS, frozenset({"home-mac"}), lease.lease_id, "home-mac"
    )
    stale = state.tickets.issue(grant, 1_000_000)
    assert state.tickets.redeem(stale, lease.lease_id, 1_000_000 + TICKET_TTL_MS + 1) is None
