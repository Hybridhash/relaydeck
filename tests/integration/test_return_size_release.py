"""Real ASGI lease actions update connector modes without remote PTY writes."""

import asyncio

from relaydeck.gateway.app import reconcile_terminal_routes
from tests.support.terminal.lease_gateway import _attach, _client, _headers, _lease, _ticket, _token


def test_explicit_release_and_tab_socket_close_switch_to_observe() -> None:
    for socket_close in (False, True):
        client, state, backend, _clock = _client()
        token = _token(state)
        att = _attach(client, token, kind="agent", takeover=True).json()
        lease = _lease(client, token, att["attachment_id"], "control").json()
        assert backend.epochs_for_host("home-mac", "acct-1")[0]["mode"] == "control"
        if socket_close:
            with client.websocket_connect(
                f"/v1/terminal/{lease['lease_id']}?ticket="
                f"{_ticket(client, token, lease['lease_id'])}"
            ):
                pass
        else:
            response = client.delete(
                f"/v1/terminal-leases/{lease['lease_id']}", headers=_headers(token)
            )
            assert response.status_code == 200
        assert backend.epochs_for_host("home-mac", "acct-1")[0]["mode"] == "observe"
        assert backend.open_lease(lease["lease_id"]) is None
        assert backend.get_attachment(att["attachment_id"]).closed is False
        assert backend.resize_log == [] and backend.input_logs == {}


def test_expiry_is_published_even_without_terminal_socket(monkeypatch) -> None:
    async def scenario() -> None:
        _client_obj, state, backend, clock = _client()
        from relaydeck.gateway.hosts import HostRecord

        record = HostRecord("home-mac", "Mac", "test", "acct-1")
        monkeypatch.setattr(state.hosts, "hosts", lambda: (record,))
        monkeypatch.setattr(state.hosts, "record_for_host", lambda _: record)
        token = _token(state)
        att = backend.create_attachment(
            "home-mac", "term-1", "agent", "acct-1", "dev-1", takeover=True, owner_label="Phone"
        )[0]
        backend.acquire_control(att.attachment_id, "acct-1", "dev-1")
        modes = []

        async def notify(_host, _text, **_kwargs):
            modes.append(backend.epochs_for_host("home-mac", "acct-1")[0]["mode"])
            return 1

        monkeypatch.setattr(state.sockets, "notify_host", notify)

        async def sleep(_duration):
            if len(modes) == 1:
                clock.now += 60_001
            else:
                raise asyncio.CancelledError

        monkeypatch.setattr("relaydeck.gateway.app.asyncio.sleep", sleep)
        try:
            await reconcile_terminal_routes(state)
        except asyncio.CancelledError:
            pass
        assert token and modes == ["control", "observe"]
        assert backend.input_logs == {} and backend.resize_log == []

    asyncio.run(scenario())


def test_foreign_device_socket_close_cannot_release_holder() -> None:
    client, state, backend, _clock = _client()
    owner, other = _token(state), _token(state, device="dev-2")
    att = _attach(client, owner, kind="agent", takeover=True).json()
    lease = _lease(client, owner, att["attachment_id"], "control").json()
    ticket = _ticket(client, other, lease["lease_id"])
    with client.websocket_connect(f"/v1/terminal/{lease['lease_id']}?ticket={ticket}"):
        pass
    assert backend.open_lease(lease["lease_id"]) is not None
    assert backend.epochs_for_host("home-mac", "acct-1")[0]["mode"] == "control"
