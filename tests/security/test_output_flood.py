"""Output flood through the real WS + ring + viewer queues.

A bursty upstream cannot exhaust memory or wedge control: the ring
stays within 2 MiB, each viewer queue latches overflow at 1 MiB with
an explicit reset (never silent loss), other viewers are unaffected,
the input path keeps answering with rate_limited gaps instead of
dying, and dead sockets release their queues.
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest
from starlette.websockets import WebSocketDisconnect

from relaydeck.contracts.wire import terminal as terminal_wire
from relaydeck.gateway.ring import (
    RING_MAX_BYTES,
    VIEWER_QUEUE_MAX_BYTES,
    StreamFanout,
)
from tests.security.helpers import attach, auth, lease, make_client, mint_token, ticket
from tests.support.asgi_socket import AsgiSocket


def test_ring_bounded_at_2m() -> None:
    fanout = StreamFanout("e1", 0)
    now = 0
    for _ in range(48):  # 3 MiB published, retention reaps on append
        now += 1000
        fanout.publish(b"f" * (64 * 1024), now)
    assert fanout.ring.retained_bytes <= RING_MAX_BYTES
    assert fanout.ring.retained_bytes == RING_MAX_BYTES
    # A resume before the live edge is an explicit stale read, no bytes.
    read = fanout.ring.read("e1", 0)
    assert read.status == "stale_offset"
    assert read.chunks == ()


def test_viewer_overflow_latches_and_isolates() -> None:
    fanout = StreamFanout("e1", 0)
    slow = fanout.subscribe("slow")
    fast = fanout.subscribe("fast")
    for _ in range(20):  # 1.25 MiB; nobody drains `slow`
        fanout.publish(b"g" * (64 * 1024), 0)
        fast.pop_all()  # the fast viewer keeps up
    assert slow.overflowed is True
    assert slow.pending_bytes <= VIEWER_QUEUE_MAX_BYTES
    assert fast.overflowed is False  # slow viewer harms only itself
    assert fanout.ring.retained_bytes <= RING_MAX_BYTES


def test_flood_over_real_ws_ends_in_explicit_reset() -> None:
    """Flood live bytes at a connected viewer: it receives an explicit
    reset doc plus close (never silent loss presented as valid)."""
    client, state, backend = make_client()
    token = mint_token(state)
    att = attach(client, token)
    ls = lease(client, token, att["attachment_id"])
    tick = ticket(client, token, ls["lease_id"])
    fanout = backend.fanout_for(att["attachment_id"])
    assert fanout is not None
    with client.websocket_connect(f"/v1/terminal/{ls['lease_id']}?ticket={tick}") as ws:
        deadline = time.monotonic() + 10
        while fanout.viewer_count == 0 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert fanout.viewer_count == 1  # subscribed before the flood
        for _ in range(24):  # 1.5 MiB of live bytes at one viewer
            backend.publish_output(att["attachment_id"], ls["epoch"], b"h" * (64 * 1024))
        ws.send_text(json.dumps({"kind": "ack"}))  # trigger the push check
        reset = json.loads(ws.receive_text())
        # Reset uses the same lowercase detail field as gap/bye.
        assert reset == {"kind": "reset", "detail": "overflow"}
        with pytest.raises(WebSocketDisconnect):
            ws.receive_text()
    assert fanout.ring.retained_bytes <= RING_MAX_BYTES
    for queue in list(fanout._viewers.values()):
        assert queue.pending_bytes <= VIEWER_QUEUE_MAX_BYTES
    assert fanout.viewer_count == 0  # dead socket released its queue


def test_control_responsive_after_flood() -> None:
    """After one attachment floods, a second attachment's control path
    still accepts input on the same gateway."""
    client, state, backend = make_client()
    token = mint_token(state)
    flooded = attach(client, token, terminal_id="flood-term")
    for _ in range(40):  # 2.5 MiB straight into the ring
        backend.publish_output(flooded["attachment_id"], flooded["epoch"], b"z" * (64 * 1024))
    assert backend.fanout_for(flooded["attachment_id"]) is not None

    other = attach(client, token, terminal_id="calm-term")
    ctl = lease(client, token, other["attachment_id"], mode="control")
    tick = ticket(client, token, ctl["lease_id"])
    with client.websocket_connect(f"/v1/terminal/{ctl['lease_id']}?ticket={tick}") as ws:
        ws.send_bytes(
            terminal_wire.encode_text(ctl["lease_id"], int(ctl["generation"]), 1, b"echo ok")
        )
        ws.send_text(json.dumps({"kind": "bye"}))
    assert backend.input_logs[ctl["lease_id"]][0][3] == b"echo ok"
    # Revocation still works on the flooded attachment's leases.
    assert (
        client.delete(
            f"/v1/terminal-attachments/{flooded['attachment_id']}", headers=auth(token)
        ).status_code
        == 200
    )


def test_input_flood_rate_limited_socket_survives() -> None:
    """150 instant 1 KiB frames against a 100 KiB/s budget: 100 land,
    50 come back as rate_limited gaps, and the socket stays open."""
    client, state, backend = make_client()
    token = mint_token(state)
    att = attach(client, token)
    ls = lease(client, token, att["attachment_id"])
    tick = ticket(client, token, ls["lease_id"])

    async def scenario() -> None:
        ws = await AsgiSocket(client.app, f"/v1/terminal/{ls['lease_id']}?ticket={tick}").open()
        try:
            for seq in range(1, 151):
                await ws.send_bytes(
                    terminal_wire.encode_text(
                        ls["lease_id"], int(ls["generation"]), seq, b"i" * 1024
                    )
                )
            # receive() has a deadline and exposes closes: no unbounded TestClient
            # wait if the handler returns without sending a close frame.
            async with asyncio.timeout(3):
                for _ in range(50):
                    message = await ws.receive()
                    assert message["type"] == "websocket.send", message
                    assert json.loads(message["text"]) == {
                        "kind": "gap",
                        "detail": "rate_limited",
                    }
            assert not ws.task.done()
            assert len(backend.input_logs[ls["lease_id"]]) == 100
            assert backend.open_lease(ls["lease_id"]) is not None

            # Output still flows on the same socket, without reacquiring control.
            backend.publish_output(att["attachment_id"], ls["epoch"], b"still live")
            await ws.send_text(json.dumps({"kind": "ack"}))
            output = await ws.receive()
            assert terminal_wire.decode_output(output["bytes"]).data == b"still live"

            # A later allowed input uses a fresh sequence; dropped frames never replay.
            state.now_ms = lambda: 1_001_000
            await ws.send_bytes(
                terminal_wire.encode_text(ls["lease_id"], int(ls["generation"]), 151, b"after")
            )
            async with asyncio.timeout(1):
                while len(backend.input_logs[ls["lease_id"]]) < 101:
                    await asyncio.sleep(0)
            assert [row[1] for row in backend.input_logs[ls["lease_id"]]] == [
                *range(1, 101),
                151,
            ]
            await ws.send_text(json.dumps({"kind": "bye"}))
            await asyncio.wait_for(ws.task, 1)
        finally:
            await ws.close()

    asyncio.run(scenario())
