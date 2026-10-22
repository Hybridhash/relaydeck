"""Host-key/trust mismatch mapped to real mechanisms.

There is no SSH transport in this system (see test_ssh_host_keys_na):
the trust anchors that exist are the enrollment key binding, the
host-token binding, and the per-message tunnel host checks. Each is
attacked here. Connector tokens are additionally accepted over the
Authorization header so deployments can keep them out of access logs
(query strings are logged by default); the query form stays for
compatibility and is covered identically.
"""

from __future__ import annotations

import base64
import json

import pytest
from starlette.websockets import WebSocketDisconnect

from relaydeck.connector.herdr import DEFAULT_SOCKET
from relaydeck.connector.pubkey import _origin_ok as qr_origin_ok
from tests.security.helpers import make_client


def test_enrollment_key_binding_rejects_mismatch() -> None:
    """Covered at the HTTP boundary in test_enrollment_replay
    (wrong-key and proof/host/id binding); this pins the primitive:
    a proof verifies only for its exact (invitation, host, key)."""
    from relaydeck.connector.pubkey import generate, sign_proof, verify_proof

    seed_a, pub_a = generate()
    _, pub_b = generate()
    sig = sign_proof(seed_a, "enr_1", "host-1")
    assert verify_proof(pub_a, "enr_1", "host-1", sig) is True
    assert verify_proof(pub_a, "enr_1", "host-2", sig) is False
    assert verify_proof(pub_a, "enr_2", "host-1", sig) is False
    assert verify_proof(pub_b, "enr_1", "host-1", sig) is False
    assert verify_proof(pub_a, "enr_1", "host-1", b"\x00" * 64) is False


def test_host_token_rotation_kills_old_token() -> None:
    client, state, _ = make_client()
    first = state.hosts.enroll("home-mac", "Home", "acct-1")
    with client.websocket_connect(f"/v1/connectors?token={first}") as ws:
        assert json.loads(ws.receive_text())["kind"] == "hello"
        ws.send_text(json.dumps({"kind": "bye"}))
    second = state.hosts.enroll("home-mac", "Home", "acct-1")
    assert second != first
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f"/v1/connectors?token={first}"):
            pass
    with client.websocket_connect(f"/v1/connectors?token={second}") as ws:
        assert json.loads(ws.receive_text())["kind"] == "hello"
        ws.send_text(json.dumps({"kind": "bye"}))


def test_connector_token_accepted_via_authorization_header() -> None:
    """Header auth keeps the long-lived host token out of access logs
    (query strings are logged by default). Bogus headers fail closed."""
    client, state, _ = make_client()
    host_token = state.hosts.enroll("home-mac", "Home", "acct-1")
    with client.websocket_connect(
        "/v1/connectors", headers={"Authorization": f"Bearer {host_token}"}
    ) as ws:
        assert json.loads(ws.receive_text()) == {
            "kind": "hello",
            "host_id": "home-mac",
            "snapshot_chunks": True,
            "epochs": [],
        }
        ws.send_text(json.dumps({"kind": "bye"}))
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/v1/connectors", headers={"Authorization": "Bearer bogus"}):
            pass
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(
            "/v1/connectors", headers={"Authorization": "bogus-scheme abc"}
        ):
            pass


def test_tunnel_messages_bound_to_token_host() -> None:
    """Per-message host checks: a tunnel authenticated as host A cannot
    report acks, results, or output for host B (each kills the tunnel)."""
    client, state, _ = make_client()
    token_a = state.hosts.enroll("host-a", "A", "acct-1")
    state.hosts.enroll("host-b", "B", "acct-1")
    bad_docs = [
        {"kind": "command.ack", "command_id": "cmd-sec-t1", "host_id": "host-b"},
        {
            "kind": "command.result",
            "command_id": "cmd-sec-t1",
            "host_id": "host-b",
            "status": "succeeded",
            "result_digest": "",
            "error_code": "",
        },
        {
            "kind": "terminal.output",
            "host_id": "host-b",
            "terminal_id": "t",
            "epoch": "e1",
            "data_b64": base64.b64encode(b"x").decode(),
        },
        {"kind": "command.ack", "command_id": "cmd-sec-t1"},  # missing host
        {"kind": "totally-unknown"},  # unknown kinds fail closed
    ]
    for doc in bad_docs:
        with client.websocket_connect(f"/v1/connectors?token={token_a}") as ws:
            assert json.loads(ws.receive_text())["kind"] == "hello"
            ws.send_text(json.dumps(doc))
            with pytest.raises(WebSocketDisconnect):
                ws.receive_text()


def test_single_live_tunnel_per_host() -> None:
    """A second tunnel for the same host replaces the first (bounded
    DoS surface); the displaced socket is severed."""
    client, state, _ = make_client()
    host_token = state.hosts.enroll("home-mac", "Home", "acct-1")
    with client.websocket_connect(f"/v1/connectors?token={host_token}") as first:
        assert json.loads(first.receive_text())["kind"] == "hello"
        with client.websocket_connect(f"/v1/connectors?token={host_token}") as second:
            assert json.loads(second.receive_text())["kind"] == "hello"
            try:
                first.send_text(json.dumps({"kind": "ping"}))
            except RuntimeError:
                second.send_text(json.dumps({"kind": "bye"}))
                return
            with pytest.raises(WebSocketDisconnect):
                first.receive_text()
            second.send_text(json.dumps({"kind": "bye"}))


def test_qr_origin_policy() -> None:
    """Enrollment QR references: https anywhere, http only on loopback."""
    assert qr_origin_ok("https://gw.example") is True
    assert qr_origin_ok("https://gw.example:8443/path") is True
    assert qr_origin_ok("http://localhost:8000") is True
    assert qr_origin_ok("http://127.0.0.1:8000") is True
    assert qr_origin_ok("http://192.168.1.5:8000") is False
    assert qr_origin_ok("http://gw.example") is False
    assert qr_origin_ok("ws://gw.example") is False
    assert qr_origin_ok("not a url %%") is False


def test_plaintext_endpoint_policy() -> None:
    """Client gateway endpoints: TLS by default, plaintext only for
    loopback or an explicitly allowlisted host (owner-authorized)."""
    from relaydeck.contracts.endpoints import is_trusted_endpoint

    assert is_trusted_endpoint("https://gw.example", frozenset()) is True
    assert is_trusted_endpoint("wss://gw.example/x", frozenset()) is True
    assert is_trusted_endpoint("http://127.0.0.1:8000", frozenset()) is True
    assert is_trusted_endpoint("http://localhost:8000", frozenset()) is True
    assert is_trusted_endpoint("http://office:8000", frozenset()) is False
    assert is_trusted_endpoint("http://office:8000", frozenset({"office"})) is True
    assert is_trusted_endpoint("ftp://gw.example", frozenset()) is False


def test_ssh_host_keys_na() -> None:
    """names 'changed SSH host keys'. N/A with justification:
    no SSH transport exists in this system. The connector reaches Herdr
    over a local Unix-domain socket (no network, no host keys), the
    gateway reaches the connector over authenticated HTTPS/WSS with
    enrollment-bound keys + host tokens, and clients reach the gateway
    over HTTPS/WSS with OIDC/PKCE sessions. The corresponding tested
    mechanisms are enrollment key binding, host-token binding, tunnel
    host checks, and QR/endpoint origin policy (the tests above)."""
    import relaydeck.connector.attach as attach_mod
    import relaydeck.connector.herdr as herdr_mod

    source = open(herdr_mod.__file__ or "").read()
    assert "open_unix_connection" in source  # Unix-socket transport only
    assert DEFAULT_SOCKET.endswith(".sock")
    scrubbed = source.lower().replace("herdr", "").replace("push", "")
    assert "ssh" not in scrubbed, "no SSH transport in the Herdr adapter"
    argv = attach_mod.attach_argv("herdr", target="agent-1", takeover=True)
    assert argv[:3] == ["herdr", "agent", "attach"]
