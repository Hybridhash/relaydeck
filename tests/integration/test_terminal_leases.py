"""Ownership and leases over HTTP/WebSocket.

Real gateway app + FakeBackend through TestClient: first agent attach,
shell attach without takeover, explicit takeover, concurrent control
claims, viewer resize rejection, detach vs close, revocation/logout,
generation mismatch, expiry/renewal and input_seq dedupe. No writes to
any live Herdr daemon (attach clients here are never spawned; the
subprocess attach path is covered with stub binaries in the unit file).
"""

from __future__ import annotations

import json

from starlette.websockets import WebSocketDisconnect

from relaydeck.contracts.wire import terminal as terminal_wire
from relaydeck.gateway.backend import BackendError
from relaydeck.gateway.revocation import revoke_account_and_close
from relaydeck.gateway.terminal_epochs import release_lease_and_close
from tests.support.terminal.lease_gateway import _attach, _client, _headers, _lease, _ticket, _token

VIEW_CAPS = frozenset({"sessions.read", "terminal.view"})


class _StubSocket:
    def __init__(self) -> None:
        self.closes: list[tuple[int, str]] = []

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closes.append((code, reason))


# -- first attach ---------------------------------------------------------------


def test_first_shell_attach_then_control_roundtrip() -> None:
    client, state, _, _ = _client()
    token = _token(state)
    r = _attach(client, token)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["generation"] == 1
    assert body["epoch"] == "e1"
    assert body["takeover_authorized"] is True
    assert body["owner"]["device_id"] == "dev-1"
    att_id: str = body["attachment_id"]

    r = _lease(client, token, att_id, "control")
    assert r.status_code == 201, r.text
    lease = r.json()
    assert lease["mode"] == "control"
    assert lease["generation"] == 1

    r = _lease(client, token, att_id, "view")
    assert r.status_code == 201, r.text
    assert r.json()["mode"] == "view"


def test_agent_attach_requires_takeover_disclosure() -> None:
    client, state, _, _ = _client()
    token = _token(state)
    r = _attach(client, token, terminal="agent-1", kind="agent", takeover=False)
    assert r.status_code == 400
    assert r.json()["code"] == "forbidden"
    assert r.json()["details"] == {"reason": "agent_requires_takeover"}
    r = _attach(client, token, terminal="agent-1", kind="agent", takeover=True)
    assert r.status_code == 201, r.text
    assert r.json()["kind"] == "agent"


def test_attach_without_capability_is_forbidden() -> None:
    client, state, _, _ = _client()
    weak = _token(state, caps=frozenset({"sessions.read", "terminal.view"}))
    r = _attach(client, weak)
    assert r.status_code == 403
    assert r.json()["code"] == "unsupported_capability"


def test_malformed_attach_and_lease_bodies() -> None:
    client, state, _, _ = _client()
    token = _token(state)
    headers = _headers(token)
    for body in (
        {"kind": "exec", "takeover": False},
        {"kind": "shell", "takeover": "yes"},
        {"kind": "shell", "takeover": False, "cols": 0},
        {"kind": "shell", "takeover": False, "rows": 5000},
        {"kind": "shell", "takeover": False, "extra": 1},
    ):
        r = client.post(
            "/v1/hosts/home-mac/terminals/term-1/attachments", headers=headers, json=body
        )
        assert r.status_code == 400, body
    att_id = _attach(client, token).json()["attachment_id"]
    for body in ({"mode": "write"}, {"mode": "view", "x": 1}, {}):
        r = client.post(f"/v1/terminal-attachments/{att_id}/leases", headers=headers, json=body)
        assert r.status_code == 400, body


def test_view_needs_attachment_first() -> None:
    client, state, _, _ = _client()
    token = _token(state)
    r = _lease(client, token, "att-999", "view")
    assert r.status_code == 404
    assert r.json()["code"] == "not_found"
    # And inventory opens no PTY: snapshot/agents/hosts create nothing.
    headers = _headers(token)
    assert client.get("/v1/hosts", headers=headers).status_code == 200
    assert client.get("/v1/hosts/home-mac/snapshot", headers=headers).status_code == 200
    assert client.get("/v1/hosts/home-mac/agents", headers=headers).status_code == 200
    assert state.backend.get_attachment("att-1") is None


# -- conflict and takeover ----------------------------------------------------------


def test_shell_conflict_names_holder_then_takeover() -> None:
    client, state, backend, _ = _client()
    first = _token(state, device="dev-1")
    second = _token(state, device="dev-2")
    att_id = _attach(client, first).json()["attachment_id"]
    control_id = _lease(client, first, att_id, "control").json()["lease_id"]

    r = _attach(client, second)
    assert r.status_code == 409
    assert r.json()["code"] == "lease_conflict"
    assert r.json()["details"] == {"owner": {"device_id": "dev-1", "label": "dev-1"}}

    r = _attach(client, second, takeover=True)
    assert r.status_code == 200, r.text
    assert r.json()["generation"] == 2
    assert r.json()["epoch"] == "e2"
    assert r.json()["attachment_id"] == att_id
    # Old lease is dead: no new tickets, no input.
    assert backend.open_lease(control_id) is None
    r = client.post("/v1/terminal-tickets", headers=_headers(first), json={"lease_id": control_id})
    assert r.json()["code"] == "lease_expired"
    # The taker owns the new generation.
    fresh = _lease(client, second, att_id, "control").json()
    assert fresh["generation"] == 2


def test_cross_account_attach_is_not_found() -> None:
    client, state, backend, _ = _client()
    mine = _token(state, account="acct-1", device="dev-1")
    other = _token(state, account="acct-2", device="dev-9")
    att_id = _attach(client, mine).json()["attachment_id"]
    control_id = _lease(client, mine, att_id, "control").json()["lease_id"]
    r = _attach(client, other)
    assert r.status_code == 404
    assert r.json()["code"] == "not_found"
    assert "dev-1" not in r.text
    # Even an explicit takeover is denied without disclosure, and the
    # original owner's stream is untouched.
    r = _attach(client, other, takeover=True)
    assert r.status_code == 404
    assert r.json()["code"] == "not_found"
    assert backend.open_lease(control_id) is not None
    attachment = backend.get_attachment(att_id)
    assert attachment is not None and attachment.generation == 1


def test_pretakeover_ticket_dies_with_takeover() -> None:
    client, state, _, _ = _client()
    first = _token(state, device="dev-1")
    second = _token(state, device="dev-2")
    att_id = _attach(client, first).json()["attachment_id"]
    control_id = _lease(client, first, att_id, "control").json()["lease_id"]
    ticket = _ticket(client, first, control_id)
    _attach(client, second, takeover=True)
    try:
        with client.websocket_connect(f"/v1/terminal/{control_id}?ticket={ticket}"):
            pass
        raise AssertionError("expected disconnect")
    except WebSocketDisconnect as exc:
        assert exc.code == 1008


# -- one control writer -----------------------------------------------------------------


def test_two_devices_cannot_control_concurrently() -> None:
    client, state, _, _ = _client()
    first = _token(state, device="dev-1")
    second = _token(state, device="dev-2")
    att_id = _attach(client, first).json()["attachment_id"]
    mine = _lease(client, first, att_id, "control").json()["lease_id"]

    r = _lease(client, second, att_id, "control")
    assert r.status_code == 409
    assert r.json()["code"] == "lease_conflict"
    assert r.json()["details"] == {"holder": {"device_id": "dev-1"}}

    # Detach frees control for the other device; the stream survives.
    r = client.delete(f"/v1/terminal-leases/{mine}", headers=_headers(first))
    assert r.status_code == 200
    assert r.json() == {"ok": True}
    r = _lease(client, second, att_id, "control")
    assert r.status_code == 201, r.text
    assert r.json()["generation"] == 1
    # And now the first device conflicts.
    r = _lease(client, first, att_id, "control")
    assert r.status_code == 409


def test_delete_unknown_or_foreign_lease_is_not_found() -> None:
    client, state, _, _ = _client()
    mine = _token(state, account="acct-1", device="dev-1")
    other = _token(state, account="acct-2", device="dev-9")
    att_id = _attach(client, mine).json()["attachment_id"]
    control_id = _lease(client, mine, att_id, "control").json()["lease_id"]
    r = client.delete("/v1/terminal-leases/lease-999", headers=_headers(mine))
    assert r.status_code == 404
    r = client.delete(f"/v1/terminal-leases/{control_id}", headers=_headers(other))
    assert r.status_code == 404
    # Still mine.
    r = client.delete(f"/v1/terminal-leases/{control_id}", headers=_headers(mine))
    assert r.status_code == 200


def test_release_by_other_device_is_not_found_no_steal() -> None:
    client, state, _, _ = _client()
    first = _token(state, device="dev-1")
    second = _token(state, device="dev-2")
    att_id = _attach(client, first).json()["attachment_id"]
    control_id = _lease(client, first, att_id, "control").json()["lease_id"]
    r = client.delete(f"/v1/terminal-leases/{control_id}", headers=_headers(second))
    assert r.status_code == 404
    assert r.json()["code"] == "not_found"
    r = _lease(client, second, att_id, "control")
    assert r.status_code == 409
    assert r.json()["code"] == "lease_conflict"
    assert r.json()["details"] == {"holder": {"device_id": "dev-1"}}
    # A still controls: its own lease renews.
    r = client.post(f"/v1/terminal-leases/{control_id}/renew", headers=_headers(first), json={})
    assert r.status_code == 200


def test_release_and_renew_on_unlisted_host_are_not_found() -> None:
    client, state, backend, _ = _client()
    token = _token(state, device="dev-1")
    attachment, _ = backend.create_attachment(
        "field-mac", "term-9", "shell", "acct-1", "dev-1", takeover=False, owner_label="dev-1"
    )
    control = backend.acquire_control(attachment.attachment_id, "acct-1", "dev-1")
    r = client.delete(f"/v1/terminal-leases/{control.lease_id}", headers=_headers(token))
    assert r.status_code == 404
    assert r.json()["code"] == "not_found"
    r = client.post(
        f"/v1/terminal-leases/{control.lease_id}/renew", headers=_headers(token), json={}
    )
    assert r.status_code == 404
    assert r.json()["code"] == "not_found"
    # The lease itself is untouched.
    assert backend.open_lease(control.lease_id) is not None


def test_release_severs_socket_and_keeps_stream() -> None:
    import asyncio

    async def scenario() -> None:
        client, state, backend, _ = _client()
        token = _token(state, device="dev-1")
        att_id = _attach(client, token).json()["attachment_id"]
        control_id = _lease(client, token, att_id, "control").json()["lease_id"]
        stub = _StubSocket()
        state.sockets.register("acct-1", control_id, stub, "dev-1")
        await release_lease_and_close(state, control_id, "acct-1", "dev-1")
        assert stub.closes == [(1008, "detached")]
        assert backend.open_lease(control_id) is None
        # The stream survives: another device takes control at gen 1.
        fresh = backend.acquire_control(att_id, "acct-1", "dev-2")
        assert fresh.generation == 1

    asyncio.run(scenario())


# -- input over the wire ------------------------------------------------------------------


def test_control_input_seq_dedupe_and_stale_gap() -> None:
    client, state, backend, _ = _client()
    token = _token(state, device="dev-1")
    att_id = _attach(client, token).json()["attachment_id"]
    control_id = _lease(client, token, att_id, "control").json()["lease_id"]
    backend.publish_output(att_id, "e1", b"$ ")
    ticket = _ticket(client, token, control_id)
    with client.websocket_connect(f"/v1/terminal/{control_id}?ticket={ticket}") as ws:
        first = terminal_wire.decode_output(ws.receive_bytes())
        assert (first.epoch, first.offset, first.data) == ("e1", 0, b"$ ")
        ws.send_bytes(terminal_wire.encode_text(control_id, 1, 1, b"ls\n"))
        ws.send_bytes(terminal_wire.encode_text(control_id, 1, 1, b"ls\n"))  # dup
        ws.send_bytes(terminal_wire.encode_text(control_id, 0, 2, b"late"))
        control = json.loads(ws.receive_text())
        assert control == {"kind": "gap", "detail": "stale_revision"}
        ws.send_text(json.dumps({"kind": "bye"}))
    assert backend.input_logs[control_id] == [(1, 1, "text", b"ls\n")]


def test_viewer_resize_rejected_over_wire() -> None:
    client, state, _, _ = _client()
    owner = _token(state, device="dev-1")
    viewer = _token(state, device="dev-2")  # full caps, but a view lease
    att_id = _attach(client, owner).json()["attachment_id"]
    view_id = _lease(client, viewer, att_id, "view").json()["lease_id"]
    ticket = _ticket(client, viewer, view_id)
    with client.websocket_connect(f"/v1/terminal/{view_id}?ticket={ticket}") as ws:
        ws.send_bytes(terminal_wire.encode_resize(view_id, 1, 1, 100, 30))
        control = json.loads(ws.receive_text())
        assert control == {
            "kind": "bye",
            "detail": "lease_conflict",
            "generation": 1,
            "input_seq": 1,
        }
        try:
            ws.receive_text()
            raise AssertionError("expected disconnect")
        except WebSocketDisconnect as exc:
            assert exc.code == 1008


def test_viewer_text_rejected_over_wire() -> None:
    client, state, _, _ = _client()
    owner = _token(state, device="dev-1")
    viewer = _token(state, device="dev-2")
    att_id = _attach(client, owner).json()["attachment_id"]
    view_id = _lease(client, viewer, att_id, "view").json()["lease_id"]
    ticket = _ticket(client, viewer, view_id)
    with client.websocket_connect(f"/v1/terminal/{view_id}?ticket={ticket}") as ws:
        ws.send_bytes(terminal_wire.encode_text(view_id, 1, 1, b"x"))
        control = json.loads(ws.receive_text())
        assert control == {
            "kind": "bye",
            "detail": "lease_conflict",
            "generation": 1,
            "input_seq": 1,
        }


def test_view_only_session_observes_stream() -> None:
    client, state, backend, _ = _client()
    owner = _token(state, device="dev-1")
    watcher = _token(state, device="dev-2", caps=VIEW_CAPS)
    att_id = _attach(client, owner).json()["attachment_id"]
    view_id = _lease(client, watcher, att_id, "view").json()["lease_id"]
    backend.publish_output(att_id, "e1", b"hello")
    ticket = _ticket(client, watcher, view_id)
    with client.websocket_connect(f"/v1/terminal/{view_id}?ticket={ticket}") as ws:
        chunk = terminal_wire.decode_output(ws.receive_bytes())
        assert chunk.data == b"hello"
        ws.send_text(json.dumps({"kind": "bye"}))


# -- expiry and renewal ---------------------------------------------------------------------


def test_control_expiry_and_foreground_renewal() -> None:
    client, state, backend, clock = _client()
    token = _token(state, device="dev-1")
    other = _token(state, device="dev-2")
    att_id = _attach(client, token).json()["attachment_id"]
    control_id = _lease(client, token, att_id, "control").json()["lease_id"]

    clock.now += 59_000
    r = client.post(f"/v1/terminal-leases/{control_id}/renew", headers=_headers(token), json={})
    assert r.status_code == 200, r.text
    first_expiry: str = r.json()["expires_at"]

    r = client.post(f"/v1/terminal-leases/{control_id}/renew", headers=_headers(other), json={})
    assert r.status_code == 404  # only the holder renews

    clock.now += 59_000
    r = client.post(f"/v1/terminal-leases/{control_id}/renew", headers=_headers(token), json={})
    assert r.status_code == 200
    assert r.json()["expires_at"] != first_expiry

    clock.now += 60_001  # past the renewed expiry, no renewal
    try:
        backend.write_input(control_id, 1, 1, "text", b"x")
        raise AssertionError("expected lease_expired")
    except BackendError as exc:
        assert exc.code == "lease_expired"
    r = client.post(f"/v1/terminal-leases/{control_id}/renew", headers=_headers(token), json={})
    assert r.status_code == 410
    assert r.json()["code"] == "lease_expired"


def test_renew_rejects_junk_body() -> None:
    client, state, _, _ = _client()
    token = _token(state, device="dev-1")
    att_id = _attach(client, token).json()["attachment_id"]
    control_id = _lease(client, token, att_id, "control").json()["lease_id"]
    r = client.post(
        f"/v1/terminal-leases/{control_id}/renew",
        headers=_headers(token),
        json={"expires_at": "whenever"},
    )
    assert r.status_code == 400


# -- logout and revocation ----------------------------------------------------------------------
# NOTE: pause-policy invalidation (backgrounded writers) is deferred to
# and intentionally uncovered here; see leases.py.


def test_logout_invalidates_device_leases() -> None:
    client, state, backend, _ = _client()
    token = _token(state, device="dev-1")
    att_id = _attach(client, token).json()["attachment_id"]
    control_id = _lease(client, token, att_id, "control").json()["lease_id"]
    assert backend.open_lease(control_id) is not None
    r = client.post("/v1/logout", headers=_headers(token))
    assert r.status_code == 200
    assert backend.open_lease(control_id) is None
    r = client.post("/v1/terminal-tickets", headers=_headers(token), json={"lease_id": control_id})
    assert r.json()["code"] in ("unauthenticated", "lease_expired")


def test_revoke_account_kills_leases_and_sockets() -> None:
    import asyncio

    async def scenario() -> None:
        client, state, backend, _ = _client()
        token = _token(state, device="dev-1")
        att_id = _attach(client, token).json()["attachment_id"]
        control_id = _lease(client, token, att_id, "control").json()["lease_id"]
        stub = _StubSocket()
        state.sockets.register("acct-1", control_id, stub, "dev-1")
        closed = await revoke_account_and_close(state, "acct-1")
        assert closed == 1
        assert stub.closes == [(1008, "unauthenticated")]
        assert backend.open_lease(control_id) is None

    asyncio.run(scenario())


# -- close vs detach (backend contract) ------------------------------------------


def test_close_ends_stream_detach_does_not() -> None:
    client, state, backend, _ = _client()
    token = _token(state, device="dev-1")
    att_id = _attach(client, token).json()["attachment_id"]
    control = backend.acquire_control(att_id, "acct-1", "dev-1")
    view = backend.issue_view(att_id, "acct-1", "dev-2")
    backend.release_lease(control.lease_id, "acct-1", "dev-1")
    assert backend.open_lease(view.lease_id) is not None  # stream survives
    dead = backend.close_attachment(att_id, "acct-1")
    assert set(dead) == {view.lease_id}
    assert backend.open_lease(view.lease_id) is None


# -- explicit remote close over HTTP -------------------------------------------------


def test_close_attachment_ends_stream_for_all_leases() -> None:
    client, state, backend, _ = _client()
    owner = _token(state, device="dev-1")
    viewer = _token(state, device="dev-2")
    att_id = _attach(client, owner).json()["attachment_id"]
    control_id = _lease(client, owner, att_id, "control").json()["lease_id"]
    view_id = _lease(client, viewer, att_id, "view").json()["lease_id"]
    r = client.delete(f"/v1/terminal-attachments/{att_id}", headers=_headers(owner))
    assert r.status_code == 200
    assert r.json() == {"ok": True}
    # Dead leases fail: no new tickets, no further control.
    for token, lease_id in ((owner, control_id), (viewer, view_id)):
        r = client.post(
            "/v1/terminal-tickets", headers=_headers(token), json={"lease_id": lease_id}
        )
        assert r.json()["code"] == "lease_expired"
    r = _lease(client, owner, att_id, "control")
    assert r.json()["code"] == "lease_expired"
    assert backend.open_lease(control_id) is None


def test_close_attachment_severs_sockets() -> None:
    import asyncio

    async def scenario() -> None:
        client, state, backend, _ = _client()
        owner = _token(state, device="dev-1")
        att_id = _attach(client, owner).json()["attachment_id"]
        control_id = _lease(client, owner, att_id, "control").json()["lease_id"]
        view_id = _lease(client, owner, att_id, "view").json()["lease_id"]
        control_sock = _StubSocket()
        view_sock = _StubSocket()
        state.sockets.register("acct-1", control_id, control_sock, "dev-1")
        state.sockets.register("acct-1", view_id, view_sock, "dev-1")
        r = client.delete(f"/v1/terminal-attachments/{att_id}", headers=_headers(owner))
        assert r.status_code == 200
        assert control_sock.closes == [(1008, "lease_expired")]
        assert view_sock.closes == [(1008, "lease_expired")]
        assert backend.open_lease(control_id) is None

    asyncio.run(scenario())


def test_close_attachment_by_second_device_succeeds() -> None:
    # Close is account-scoped: any own device may close (unlike release).
    client, state, backend, _ = _client()
    owner = _token(state, device="dev-1")
    second = _token(state, device="dev-2")
    att_id = _attach(client, owner).json()["attachment_id"]
    control_id = _lease(client, owner, att_id, "control").json()["lease_id"]
    r = client.delete("/v1/terminal-attachments/att-999", headers=_headers(owner))
    assert r.status_code == 404
    r = client.delete(f"/v1/terminal-attachments/{att_id}", headers=_headers(second))
    assert r.status_code == 200
    assert backend.open_lease(control_id) is None


def test_close_attachment_cross_account_is_not_found() -> None:
    client, state, backend, _ = _client()
    mine = _token(state, account="acct-1", device="dev-1")
    other = _token(state, account="acct-2", device="dev-9")
    att_id = _attach(client, mine).json()["attachment_id"]
    control_id = _lease(client, mine, att_id, "control").json()["lease_id"]
    r = client.delete(f"/v1/terminal-attachments/{att_id}", headers=_headers(other))
    assert r.status_code == 404
    assert r.json()["code"] == "not_found"
    assert backend.open_lease(control_id) is not None


def test_close_attachment_needs_control_capability() -> None:
    client, state, backend, _ = _client()
    owner = _token(state, device="dev-1")
    watcher = _token(state, device="dev-2", caps=VIEW_CAPS)
    att_id = _attach(client, owner).json()["attachment_id"]
    control_id = _lease(client, owner, att_id, "control").json()["lease_id"]
    r = client.delete(f"/v1/terminal-attachments/{att_id}", headers=_headers(watcher))
    assert r.status_code == 403
    assert r.json()["code"] == "unsupported_capability"
    assert backend.open_lease(control_id) is not None
