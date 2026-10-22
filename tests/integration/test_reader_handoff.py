"""Real gateway HTTP/WS + real agent/shell recovery; no TCP or live Herdr."""

import asyncio
from types import SimpleNamespace

import pytest

from relaydeck.client.features.sync.transport import TransportError
from relaydeck.contracts.wire import terminal as terminal_wire
from relaydeck.gateway.backend import BackendError
from relaydeck.gateway.leases import READER_HANDOFF_TTL_MS
from relaydeck.gateway.ring import VIEWER_QUEUE_MAX_BYTES
from tests.support.host_harness import run
from tests.support.terminal.lease_gateway import _attach, _client, _headers, _lease, _ticket, _token
from tests.support.terminal.reader_handoff import _actor, _wait


def _refuse_input_until_restore(backend):
    original = backend.write_input

    def unavailable(*_args):
        raise BackendError("host_offline", "synthetic unavailable host", True)

    backend.write_input = unavailable
    return original


@pytest.mark.parametrize("kind", ["agent", "shell"])
@pytest.mark.parametrize("cause", ["gap", "reset", "overflow"])
def test_real_session_recovers_reader_without_control_acquisition_resize_or_input_replay(
    kind, cause
):
    async def scenario():
        async with _actor(kind) as ctx:
            actor, lease = ctx.actor, ctx.lease
            controller = actor.controller
            generation, expires = (
                controller.generation,
                ctx.backend.open_lease(lease.lease_id).expires_ms,
            )
            if cause == "gap":
                # A fatal input refusal drives recovery; rate limiting stays live.
                _refuse_input_until_restore(ctx.backend)
                await controller._client.send_input(b"withheld")
            else:
                fanout = ctx.backend.fanout_for(lease.attachment_id)
                if cause == "overflow":
                    ctx.backend.publish_output(
                        lease.attachment_id, lease.epoch, b"x" * (VIEWER_QUEUE_MAX_BYTES + 1)
                    )
                else:
                    for queue in fanout._viewers.values():
                        queue.mark_reset(lease.epoch)
            await asyncio.wait_for(ctx.readers[0], 2)
            if cause != "gap":
                await _wait(lambda: ctx.backend.open_lease(lease.lease_id).resume_pending)
            else:
                controller.set_host_online(True)
            before = list(ctx.requests)
            outcome = await actor.drive_recovery()
            assert outcome in ("resumed", "reattached")
            assert actor.controller is controller and actor.lease is lease
            live = ctx.backend.open_lease(lease.lease_id)
            assert live.generation == generation and not live.resume_pending
            assert live.expires_ms == expires
            assert all(
                path == "/v1/terminal-tickets" for _method, path in ctx.requests[len(before) :]
            )
            assert ctx.backend.input_logs == {} and ctx.backend.resize_log == []
            if cause == "gap":
                assert controller._client.next_offset == len(b"initial\r\n")
                assert bytes(actor.output_log) == b"initial\r\n"
            ctx.readers.append(asyncio.create_task(actor.run()))
            ctx.backend.publish_output(lease.attachment_id, lease.epoch, b"after\r\n")
            await _wait(lambda: bytes(actor.output_log).endswith(b"after\r\n"))
            assert (
                controller._client.next_offset
                == ctx.backend.read_stream(lease.lease_id, "", 0).next_offset
            )
            assert ctx.backend.input_logs == {} and ctx.backend.resize_log == []
            if kind == "agent":
                assert actor.store.draft == "retained draft"
            await actor.disconnect()
            assert ctx.backend.open_lease(lease.lease_id) is None
            assert ctx.backend.epochs_for_host("home-mac", "acct-1")[0]["mode"] == "observe"

    run(scenario())


@pytest.mark.parametrize("kind", ["agent", "shell"])
def test_failed_reopen_restores_recovery_flags_and_expires_without_reacquiring_control(kind):
    async def scenario():
        async with _actor(kind) as ctx:
            _refuse_input_until_restore(ctx.backend)
            await ctx.actor.controller._client.send_input(b"withheld")
            await asyncio.wait_for(ctx.readers[0], 2)
            controller = ctx.actor.controller
            controller.set_host_online(True)
            pending = controller.pending_resume
            assert pending is not None
            ctx.factory.fail_next = True
            with pytest.raises(TransportError, match="handshake rejected"):
                await ctx.actor.drive_recovery()
            assert controller.pending_resume == pending
            assert ctx.backend.open_lease(ctx.lease.lease_id) is None
            assert ctx.backend.epochs_for_host("home-mac", "acct-1")[0]["mode"] == "observe"
            assert ctx.backend.input_logs == {} and ctx.backend.resize_log == []

    run(scenario())


@pytest.mark.parametrize("kind", ["agent", "shell"])
def test_resumed_reader_preserves_input_sequence_without_replaying_failed_input(kind):
    async def scenario():
        async with _actor(kind) as ctx:
            controller = ctx.actor.controller
            await controller._client.send_input(b"before")
            await _wait(lambda: len(ctx.backend.input_logs.get(ctx.lease.lease_id, [])) == 1)
            original_write = _refuse_input_until_restore(ctx.backend)
            await controller._client.send_input(b"withheld")
            await asyncio.wait_for(ctx.readers[0], 2)
            controller.set_host_online(True)
            await ctx.actor.drive_recovery()
            assert [item[3] for item in ctx.backend.input_logs[ctx.lease.lease_id]] == [b"before"]
            ctx.backend.write_input = original_write
            await controller._client.send_input(b"after")
            await _wait(lambda: len(ctx.backend.input_logs[ctx.lease.lease_id]) == 2)
            assert [(item[1], item[3]) for item in ctx.backend.input_logs[ctx.lease.lease_id]] == [
                (1, b"before"),
                (3, b"after"),
            ]
            assert ctx.backend.resize_log == []

    run(scenario())


@pytest.mark.parametrize("kind", ["agent", "shell"])
def test_rate_limited_gap_keeps_real_session_reader_lease_and_draft(kind):
    async def scenario():
        async with _actor(kind) as ctx:
            actor, controller, lease = ctx.actor, ctx.actor.controller, ctx.lease
            requests = list(ctx.requests)
            limiter = ctx.state.input_bytes
            ctx.state.input_bytes = SimpleNamespace(check=lambda *_args, **_kwargs: False)
            await controller._client.send_input(b"dropped")
            await _wait(
                lambda: actor._snoop.last_control is not None
                and actor._snoop.last_control.get("detail") == "rate_limited"
            )
            assert not ctx.readers[0].done()
            assert controller.pending_resume is None and not controller.reattach_needed
            assert not controller.writer_revoked and not controller.ui_input_fenced
            assert controller.gap_detail == "" and controller.gap_recovery.attempt == 0
            ctx.backend.publish_output(lease.attachment_id, lease.epoch, b"after\r\n")
            await _wait(lambda: bytes(actor.output_log) == b"initial\r\nafter\r\n")
            ctx.state.input_bytes = limiter
            await controller._client.send_input(b"permitted")
            await _wait(lambda: len(ctx.backend.input_logs.get(lease.lease_id, [])) == 1)
            assert [(item[1], item[3]) for item in ctx.backend.input_logs[lease.lease_id]] == [
                (2, b"permitted")
            ]
            assert actor.controller is controller and actor.lease is lease
            live = ctx.backend.open_lease(lease.lease_id)
            assert live is not None and live.generation == controller.generation
            assert not live.resume_pending
            assert ctx.requests == requests and ctx.backend.resize_log == []
            if kind == "agent":
                assert actor.store.draft == "retained draft"

    run(scenario())


@pytest.mark.parametrize("kind", ["agent", "shell"])
@pytest.mark.parametrize("end", ["release", "revoke", "replace", "expire"])
def test_fenced_resume_ticket_cannot_resurrect_ended_or_replaced_lease(kind, end):
    client, state, backend, clock = _client()
    token = _token(state)
    att = _attach(client, token, kind=kind, takeover=kind == "agent").json()
    lease = _lease(client, token, att["attachment_id"], "control").json()
    lid = lease["lease_id"]
    with client.websocket_connect(f"/v1/terminal/{lid}?ticket={_ticket(client, token, lid)}"):
        ticket = _ticket(client, token, lid)
    assert backend.open_lease(lid).resume_pending
    if end == "release":
        assert (
            client.delete(f"/v1/terminal-leases/{lid}", headers=_headers(token)).status_code == 200
        )
    elif end == "revoke":
        backend.revoke_leases_device("acct-1", "dev-1")
    elif end == "replace":
        other = _token(state, device="dev-2")
        replacement = _attach(client, other, kind=kind, takeover=True).json()
        fresh = _lease(client, other, replacement["attachment_id"], "control").json()
    else:
        clock.now += READER_HANDOFF_TTL_MS + 1
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect) as error:
        with client.websocket_connect(
            f"/v1/terminal/{lid}?ticket={ticket}&epoch={lease['epoch']}&offset=0"
        ):
            pass
    assert error.value.reason == "lease_expired"
    assert backend.open_lease(lid) is None
    assert backend.input_logs == {} and backend.resize_log == []
    if end == "replace":
        assert backend.open_lease(fresh["lease_id"]) is not None
        assert backend.epochs_for_host("home-mac", "acct-1")[0]["mode"] == "control"
    else:
        assert backend.epochs_for_host("home-mac", "acct-1")[0]["mode"] == "observe"


def test_handoff_is_holder_only_cursor_validated_and_not_extended_by_failed_retries_or_renewal():
    client, state, backend, clock = _client()
    token, other = _token(state), _token(state, device="dev-2")
    att = _attach(client, token, kind="agent", takeover=True).json()
    lease = _lease(client, token, att["attachment_id"], "control").json()
    lid = lease["lease_id"]
    backend.publish_output(att["attachment_id"], lease["epoch"], b"abcdef")
    with client.websocket_connect(f"/v1/terminal/{lid}?ticket={_ticket(client, token, lid)}") as ws:
        ws.receive_bytes()
        ticket = _ticket(client, token, lid)
        foreign = _ticket(client, other, lid)
    until = backend.open_lease(lid).expires_ms
    for candidate, epoch, offset in ((foreign, lease["epoch"], 0), (ticket, lease["epoch"], 99)):
        from starlette.websockets import WebSocketDisconnect

        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(
                f"/v1/terminal/{lid}?ticket={candidate}&epoch={epoch}&offset={offset}"
            ) as ws:
                assert ws.receive_json() == {"kind": "reset", "detail": "stale_offset"}
                ws.receive_text()
        assert backend.open_lease(lid).expires_ms == until
    clock.now += 1_000
    assert backend.renew_lease(lid, "acct-1", "dev-1").expires_ms == until
    with pytest.raises(BackendError, match="no active writer"):
        backend.write_input(lid, lease["generation"], 1, "text", b"never")
    assert backend.input_logs == {} and backend.resize_log == []
    good = _ticket(client, token, lid)
    with client.websocket_connect(
        f"/v1/terminal/{lid}?ticket={good}&epoch={lease['epoch']}&offset=3"
    ) as ws:
        chunk = terminal_wire.decode_output(ws.receive_bytes())
        assert chunk.offset == 3 and chunk.data == b"def"
        assert not backend.open_lease(lid).resume_pending
    assert backend.open_lease(lid) is None


def test_foreign_reader_cannot_keep_abandoned_control_writer_alive():
    client, state, backend, _clock = _client()
    owner, other = _token(state), _token(state, device="dev-2")
    att = _attach(client, owner, kind="agent", takeover=True).json()
    lease = _lease(client, owner, att["attachment_id"], "control").json()
    lid = lease["lease_id"]
    other_context = client.websocket_connect(
        f"/v1/terminal/{lid}?ticket={_ticket(client, other, lid)}"
    )
    with client.websocket_connect(f"/v1/terminal/{lid}?ticket={_ticket(client, owner, lid)}"):
        other_context.__enter__()
    try:
        assert backend.open_lease(lid) is None
        assert backend.epochs_for_host("home-mac", "acct-1")[0]["mode"] == "observe"
    finally:
        other_context.__exit__(None, None, None)
