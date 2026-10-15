"""real Starlette state machine, concurrent gateway socket close."""

import asyncio
import json
from urllib.parse import urlencode

import pytest
from starlette.websockets import WebSocket, WebSocketState

from tests.support.terminal.sockets import _grant, _state


@pytest.mark.parametrize("poll_timeout", [False, True])
@pytest.mark.parametrize("detail", ["stale_epoch", "overflow", "reset", "lease_expired"])
def test_external_close_during_terminal_push_keeps_first_reason_and_cleans_up(detail, poll_timeout):
    async def scenario():
        state, backend, endpoint = _state()
        attachment, _ = backend.create_attachment(
            "home-mac", "term-1", "agent", "acct-1", "dev-1", takeover=True
        )
        lease = backend.acquire_control(attachment.attachment_id, "acct-1", "dev-1")
        ticket = state.tickets.issue(_grant(lease.lease_id), state.now_ms())
        frames = []
        first = True

        async def send(message):
            frames.append(message)

        async def receive():
            nonlocal first
            if first:
                first = False
                return {"type": "websocket.connect"}
            queue = backend.fanout_for(lease.attachment_id).viewer(str(id(socket)))
            assert queue is not None
            if detail == "stale_epoch":
                queue.mark_reset("new-epoch")
            elif detail == "overflow":
                queue.overflowed = True
            elif detail == "reset":
                queue.push(0, b"undeliverable")
            else:
                backend.release_lease(lease.lease_id, lease.owner, lease.device_id)
            await state.sockets.notify_lease(
                lease.lease_id, json.dumps({"kind": "reset", "detail": detail})
            )
            await state.sockets.close_lease(lease.lease_id, "stale_revision")
            assert socket.application_state == WebSocketState.DISCONNECTED
            if poll_timeout:
                raise TimeoutError
            return {"type": "websocket.receive", "text": json.dumps({"kind": "ack", "applied": 0})}

        socket = WebSocket(
            {"type": "websocket", "query_string": urlencode({"ticket": ticket}).encode()},
            receive,
            send,
        )
        # A legal ignored ack reaches the non-timeout push path.
        await asyncio.wait_for(endpoint(socket, lease.lease_id), 1)
        assert [frame for frame in frames if frame["type"] == "websocket.close"] == [
            {"type": "websocket.close", "code": 1008, "reason": "stale_revision"}
        ]
        assert json.loads(next(frame["text"] for frame in frames if "text" in frame)) == {
            "kind": "reset",
            "detail": detail,
        }
        assert len(state.sockets) == 0
        assert backend.fanout_for(lease.attachment_id).viewer_count == 0
        assert not state.sockets._send_locks

    asyncio.run(scenario())
