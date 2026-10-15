"""Real gateway ASGI/socket registry logs lease transitions without content."""

import pytest

from tests.support.terminal.lease_gateway import _attach, _client, _headers, _lease, _ticket, _token


def test_gateway_issue_renew_refusal_socket_close_and_release(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("INFO", logger="relaydeck.lease_lifecycle")
    client, state, backend, _clock = _client()
    token = _token(state)
    attachment = _attach(client, token, kind="agent", takeover=True).json()
    lease = _lease(client, token, attachment["attachment_id"], "control").json()
    lease_id = lease["lease_id"]
    assert (
        client.post(
            f"/v1/terminal-leases/{lease_id}/renew", json={}, headers=_headers(token)
        ).status_code
        == 200
    )
    ticket = _ticket(client, token, lease_id)
    backend.publish_output(
        attachment["attachment_id"], lease["epoch"], b"private synthetic contents"
    )
    with client.websocket_connect(f"/v1/terminal/{lease_id}?ticket={ticket}") as ws:
        ws.receive_bytes()
        response = client.delete(f"/v1/terminal-leases/{lease_id}", headers=_headers(token))
        assert response.status_code == 200
        assert ws.receive()["type"] == "websocket.close"
    assert (
        client.post(
            f"/v1/terminal-leases/{lease_id}/renew", json={}, headers=_headers(token)
        ).status_code
        == 410
    )
    messages = [r.getMessage() for r in caplog.records if r.name == "relaydeck.lease_lifecycle"]
    for event in (
        "lease_issue",
        "lease_renew",
        "lease_release",
        "renew_failure",
        "socket_open",
        "socket_close",
    ):
        assert any(
            f"event={event}" in message and f"lease={lease_id}" in message for message in messages
        )
    assert any("reason=detached" in m and "source=server" in m for m in messages)
    assert any("event=renew_failure" in m and "source=server_refusal" in m for m in messages)
    assert all(
        token not in m
        and ticket not in m
        and "home-mac" not in m
        and "term-1" not in m
        and "private synthetic contents" not in m
        for m in messages
    )
    assert not state.sockets._diagnostics and backend.input_logs == {} and backend.resize_log == []
