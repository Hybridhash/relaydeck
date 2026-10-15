"""Every size bound enforced at its real boundary.

64 KiB JSON bodies, 1 MiB snapshots, 16 KiB input chunks, 64 KiB
tunnel/output frames, 4 KiB journal results, the 200-row inventory
page, and the per-account command rate limit. Each bound is driven
through the production path (HTTP route, WS handler, or journal call
the tunnel reaches), including the exact-boundary accept case.
"""

from __future__ import annotations

import json
import struct
from pathlib import Path
from typing import Any

import pytest
from starlette.websockets import WebSocketDisconnect

from relaydeck.gateway import terminal_wire
from relaydeck.gateway.app import GatewayState, create_app
from relaydeck.gateway.production import JournalBackend
from relaydeck.journal import CommandJournal
from tests.security.helpers import (
    ALL_CAPS,
    attach,
    auth,
    command_body,
    lease,
    make_client,
    mint_token,
    snapshot_doc,
    ticket,
)


def test_json_body_cap_64k() -> None:
    """Bodies over 64 KiB are refused with 413 before parsing; a body
    just under the cap still reaches normal validation."""
    from fastapi.testclient import TestClient

    client, state, _ = make_client()
    token = mint_token(state)
    huge = "x" * (64 * 1024 + 1)
    r = client.post("/v1/terminal-tickets", headers=auth(token), json={"lease_id": huge})
    assert r.status_code == 413
    assert r.json()["code"] == "too_large"
    near = "x" * (60 * 1024)
    r = client.post("/v1/terminal-tickets", headers=auth(token), json={"lease_id": near})
    assert r.status_code == 400  # schema validation, not the body cap
    assert r.json()["code"] == "forbidden"
    _ = TestClient  # anchor: HTTP boundary is the TestClient path


def test_json_body_cap_covers_command_submit() -> None:
    client, state, _ = make_client()
    token = mint_token(state)
    body = command_body("cmd-sec-f1")
    body["payload"] = {"text": "y" * (128 * 1024)}
    r = client.post("/v1/hosts/home-mac/commands", headers=auth(token), json=body)
    assert r.status_code == 413
    assert r.json()["code"] == "too_large"


def test_snapshot_cap_1m() -> None:
    """A backend snapshot serializing past 1 MiB fails closed instead
    of flowing to the client; normal snapshots are unaffected."""
    client, state, backend = make_client()
    token = mint_token(state)
    assert client.get("/v1/hosts/home-mac/snapshot", headers=auth(token)).status_code == 200
    big = snapshot_doc("sec-big")
    agent = dict(big["agents"][0])
    agent["title"] = "t" * 256
    big["agents"] = [dict(agent, terminal_id=f"term-{i}") for i in range(6000)]
    backend.snapshots["home-mac"] = big
    r = client.get("/v1/hosts/home-mac/snapshot", headers=auth(token))
    assert r.status_code == 413
    assert r.json()["code"] == "too_large"


def test_agents_page_bound_200() -> None:
    client, state, backend = make_client()
    token = mint_token(state)
    big = snapshot_doc("sec-page")
    big["agents"] = [dict(big["agents"][0], terminal_id=f"term-{i}") for i in range(500)]
    backend.snapshots["home-mac"] = big
    r = client.get("/v1/hosts/home-mac/agents", headers=auth(token), params={"limit": "500"})
    assert r.status_code == 200
    assert len(r.json()["agents"]) == 200
    r = client.get("/v1/hosts/home-mac/agents", headers=auth(token))
    assert len(r.json()["agents"]) == 50  # default page


def test_input_chunk_16k_ws_boundary() -> None:
    """Exactly 16 KiB of text is accepted; one byte more kills the socket."""
    client, state, backend = make_client()
    token = mint_token(state)
    att = attach(client, token)
    ls = lease(client, token, att["attachment_id"])
    generation = int(ls["generation"])
    tick = ticket(client, token, ls["lease_id"])
    with client.websocket_connect(f"/v1/terminal/{ls['lease_id']}?ticket={tick}") as ws:
        ws.send_bytes(terminal_wire.encode_text(ls["lease_id"], generation, 1, b"a" * 16384))
        ws.send_text(json.dumps({"kind": "bye"}))
    assert backend.input_logs[ls["lease_id"]][0][3] == b"a" * 16384

    # Closing a control socket releases its lease. Each independent boundary
    # attack needs fresh authorization rather than resurrecting that lease.
    assert backend.open_lease(ls["lease_id"]) is None
    ls = lease(client, token, att["attachment_id"])
    generation = int(ls["generation"])
    tick2 = ticket(client, token, ls["lease_id"])
    raw = _raw_input_frame(ls["lease_id"], generation, 2, terminal_wire.KIND_TEXT, b"b" * 16385)
    with client.websocket_connect(f"/v1/terminal/{ls['lease_id']}?ticket={tick2}") as ws:
        ws.send_bytes(raw)
        with pytest.raises(WebSocketDisconnect):
            ws.receive_text()
    assert backend.input_logs.get(ls["lease_id"], []) == []  # oversize stored nothing
    assert sum(len(rows) for rows in backend.input_logs.values()) == 1


def _raw_input_frame(lease_id: str, generation: int, seq: int, marker: int, data: bytes) -> bytes:
    lease = lease_id.encode()
    return (
        struct.pack(">BH", terminal_wire.INPUT_V2, len(lease))
        + lease
        + struct.pack(">IIBI", generation, seq, marker, len(data))
        + data
    )


def test_terminal_wire_unit_vectors() -> None:
    assert terminal_wire.MAX_INPUT_CHUNK == 16 * 1024
    assert terminal_wire.MAX_OUTPUT_CHUNK == 64 * 1024
    with pytest.raises(ValueError):
        terminal_wire.encode_text("l", 1, 1, b"x" * (16 * 1024 + 1))
    with pytest.raises(ValueError):
        terminal_wire.encode_output("e", 0, b"x" * (64 * 1024 + 1))
    with pytest.raises(ValueError):  # v1 frames: no silent downgrade
        terminal_wire.decode_input(b"\x01" + b"\x00" * 20)
    with pytest.raises(ValueError):
        terminal_wire.decode_output(b"\x01" + b"\x00" * 20)
    with pytest.raises(ValueError):  # unknown input kind
        terminal_wire.decode_input(_raw_input_frame("l", 1, 1, 0x09, b"x"))
    with pytest.raises(ValueError):  # text must be UTF-8
        terminal_wire.decode_input(_raw_input_frame("l", 1, 1, 0x01, b"\xff\xff"))
    with pytest.raises(ValueError):  # unknown key name
        terminal_wire.encode_key("l", 1, 1, "not-a-key-name-xyz")
    with pytest.raises(ValueError):  # resize out of range
        terminal_wire.encode_resize("l", 1, 1, 0, 24)
    with pytest.raises(ValueError):
        terminal_wire.encode_resize("l", 1, 1, 1025, 24)


def test_terminal_ws_text_cap_64k() -> None:
    """Oversized control text closes the socket WITHOUT being parsed:
    no json.loads call ever sees more than 64 KiB on this path."""
    sizes: list[int] = []
    real_loads = json.loads

    def recording_loads(text: str, *args: Any, **kwargs: Any) -> Any:
        sizes.append(len(text))
        return real_loads(text, *args, **kwargs)

    client, state, _ = make_client()
    token = mint_token(state)
    att = attach(client, token)
    ls = lease(client, token, att["attachment_id"])
    tick = ticket(client, token, ls["lease_id"])
    with client.websocket_connect(f"/v1/terminal/{ls['lease_id']}?ticket={tick}") as ws:
        json.loads = recording_loads  # type: ignore[assignment]
        try:
            ws.send_text("{" + '"kind":"ack","pad":"' + "p" * (200 * 1024) + '"}')
            with pytest.raises(WebSocketDisconnect):
                ws.receive_text()
        finally:
            json.loads = real_loads
    # Post-fix the server parses nothing on this path (sizes stays
    # empty); pre-fix it parsed the 200 KiB string and failed below.
    assert max(sizes, default=0) <= 64 * 1024


def test_tunnel_text_cap_64k() -> None:
    client, state, _ = make_client()
    host_token = state.hosts.enroll("home-mac", "Home", "acct-1")
    with client.websocket_connect(f"/v1/connectors?token={host_token}") as ws:
        assert json.loads(ws.receive_text())["kind"] == "hello"
        ws.send_text("z" * (64 * 1024 + 1))
        with pytest.raises(WebSocketDisconnect):
            ws.receive_text()


def test_tunnel_output_within_text_cap_keeps_tunnel() -> None:
    """A well-formed terminal.output frame for an unknown terminal is a
    semantic miss: the frame drops but the host tunnel survives (one
    stale stream must not kill the whole host link)."""
    import base64

    client, state, _ = make_client()
    host_token = state.hosts.enroll("home-mac", "Home", "acct-1")
    with client.websocket_connect(f"/v1/connectors?token={host_token}") as ws:
        assert json.loads(ws.receive_text())["kind"] == "hello"
        ws.send_text(
            json.dumps(
                {
                    "kind": "terminal.output",
                    "host_id": "home-mac",
                    "terminal_id": "unknown-term",
                    "epoch": "e1",
                    "data_b64": base64.b64encode(b"v" * 1024).decode(),
                }
            )
        )
        ws.send_text(json.dumps({"kind": "ping"}))
        assert json.loads(ws.receive_text()) == {"kind": "pong"}
        ws.send_text(json.dumps({"kind": "bye"}))


def _journal_client(tmp_path: Path) -> Any:
    from fastapi.testclient import TestClient

    journal = CommandJournal(tmp_path / "gw-sec.db")
    backend = JournalBackend(
        journal,
        now_ms=lambda: 1_000_000,
        snapshots={"home-mac": snapshot_doc("sec-j")},
        record_inputs=True,
    )
    state = GatewayState(backend=backend, cursor_secret=b"s" * 32, now_ms=lambda: 1_000_000)
    return TestClient(create_app(state)), state, backend


def test_journal_result_cap_4k_at_tunnel(tmp_path: Path) -> None:
    """An oversized command.result over the tunnel is refused with a
    clean 1008 close -- never an unhandled exception -- and records no
    outcome."""
    client, state, backend = _journal_client(tmp_path)
    token = mint_token(state)
    r = client.post(
        "/v1/hosts/home-mac/commands", headers=auth(token), json=command_body("cmd-sec-f2")
    )
    assert r.status_code == 202, r.text
    host_token = state.hosts.enroll("home-mac", "Home", "acct-1")
    with client.websocket_connect(f"/v1/connectors?token={host_token}") as ws:
        assert json.loads(ws.receive_text())["kind"] == "hello"
        dispatch = json.loads(ws.receive_text())
        assert dispatch["kind"] == "command.dispatch"
        ws.send_text(
            json.dumps(
                {
                    "kind": "command.result",
                    "command_id": "cmd-sec-f2",
                    "host_id": "home-mac",
                    "status": "succeeded",
                    "result_digest": "d" * 64,
                    "error_code": "",
                    "result": {"pad": "q" * 5000},
                }
            )
        )
        with pytest.raises(WebSocketDisconnect) as exc_info:
            ws.receive_text()
        assert exc_info.value.code == 1008
    status = backend.command_status("cmd-sec-f2")
    assert status is not None and status["status"] == "dispatched"


def test_journal_result_cap_4k_direct_file(tmp_path: Path) -> None:
    journal = CommandJournal(tmp_path / "direct.db")
    try:
        journal.submit(
            command_id="cmd-sec-f3",
            principal="acct-1",
            host_id="home-mac",
            kind="agent.prompt",
            target={"agent_id": "a"},
            payload={"text": "hi"},
            now=1_000_000,
        )
        assert journal.mark_dispatched("cmd-sec-f3", now=1_000_000) is True
        with pytest.raises(ValueError):
            journal.mark_result(
                "cmd-sec-f3", "succeeded", result={"pad": "q" * 5000}, now=1_000_000
            )
        assert (
            journal.mark_result("cmd-sec-f3", "succeeded", result={"type": "ok"}, now=1_000_000)
            is True
        )
    finally:
        journal.close()


def test_command_rate_limit_per_account() -> None:
    """20 commands/minute per account: the 21st in the window is 429."""
    client, state, _ = make_client()
    token = mint_token(state, "acct-1")
    for i in range(20):
        r = client.post(
            "/v1/hosts/home-mac/commands",
            headers=auth(token),
            json=command_body(f"cmd-sec-rl{i:02d}"),
        )
        assert r.status_code == 202, (i, r.text)
    r = client.post(
        "/v1/hosts/home-mac/commands", headers=auth(token), json=command_body("cmd-sec-rl20")
    )
    assert r.status_code == 429
    assert r.json()["code"] == "rate_limited"
    # Another account still has its own budget.
    other = mint_token(state, "acct-2")
    r = client.post(
        "/v1/hosts/home-mac/commands", headers=auth(other), json=command_body("cmd-sec-rl99")
    )
    assert r.status_code == 202


def test_extension_protocol_bounds() -> None:
    from relaydeck_terminal.protocol import (
        MAX_WRITE_CHUNK,
        VIEWER_QUEUE_LIMIT,
        decode_packets,
        encode_write,
    )

    from relaydeck.gateway.ring import VIEWER_QUEUE_MAX_BYTES

    assert MAX_WRITE_CHUNK == 64 * 1024
    assert VIEWER_QUEUE_LIMIT == VIEWER_QUEUE_MAX_BYTES == 1024 * 1024
    encode_write(b"w" * (64 * 1024))
    with pytest.raises(ValueError):
        encode_write(b"w" * (64 * 1024 + 1))
    forged = bytearray(struct.pack(">BI", 0x01, 64 * 1024 + 1) + b"w")
    with pytest.raises(ValueError):
        decode_packets(forged)


def test_body_cap_holds_on_combined_web_app() -> None:
    """serve.py mounts gateway routes onto the combined origin: the
    64 KiB cap travels with them (no middleware gap)."""
    from fastapi.testclient import TestClient

    from relaydeck.client.serve import build_web_app
    from relaydeck.gateway.app import create_app
    from tests.security.helpers import NOW, make_state

    gw_state, _ = make_state(NOW)
    gateway_app = create_app(gw_state)

    def _stub_main(page: object) -> None:
        _ = page

    app, _ = build_web_app(
        gateway_state=gw_state,
        gateway_app=gateway_app,
        flet_main=_stub_main,
        public_origin="https://gw.example",
        cookie_secret=b"c" * 32,
    )
    web = TestClient(app, raise_server_exceptions=False)
    r = web.post("/v1/terminal-tickets", json={"lease_id": "x" * (70 * 1024)})
    assert r.status_code == 413
    assert r.json()["code"] == "too_large"


def test_body_cap_preserves_sse_streaming() -> None:
    """Regression: the body-cap middleware replays one buffered body
    and then delegates to the live channel, so infinite SSE streams
    (whose disconnect listener reads past the body) keep flowing."""
    import asyncio
    from collections.abc import MutableMapping

    from relaydeck.gateway.cursors import encode_cursor
    from tests.security.helpers import NOW, SECRET, make_state

    state, backend = make_state(NOW)
    backend.event_logs["home-mac"] = [
        {
            "protocol": "relaydeck.gateway.v1",
            "event_id": "ev-9",
            "host_id": "home-mac",
            "host_epoch": "epoch-7",
            "sequence": 9,
            "cursor": "c9",
            "type": "agent.upserted",
            "resource_id": "term-1",
            "resource_revision": 11,
            "observed_at": "2026-10-03T06:20:00Z",
            "payload": {"status": "working"},
        }
    ]
    access, _ = state.sessions.create("acct-1", ALL_CAPS, frozenset({"home-mac"}), NOW)
    cursor = encode_cursor(SECRET, {"host_id": "home-mac", "sequence": 0})
    app = create_app(state)

    async def scenario() -> None:
        messages: list[dict[str, Any]] = []
        bodies: list[bytes] = []
        streamed = asyncio.Event()
        delegated = asyncio.Event()
        disconnect = asyncio.Event()
        received_body = False

        async def receive() -> dict[str, Any]:
            nonlocal received_body
            if not received_body:
                received_body = True
                return {"type": "http.request", "body": b"", "more_body": False}
            delegated.set()
            await disconnect.wait()
            return {"type": "http.disconnect"}

        async def send(message: MutableMapping[str, Any]) -> None:
            messages.append(dict(message))
            if message["type"] == "http.response.body":
                bodies.append(message.get("body", b""))
                if b"ev-9" in b"".join(bodies):
                    streamed.set()

        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/v1/events",
            "raw_path": b"/v1/events",
            "query_string": f"host_id=home-mac&cursor={cursor}".encode(),
            "headers": [(b"authorization", f"Bearer {access}".encode())],
            "server": ("testserver", 80),
            "client": ("127.0.0.1", 1),
        }
        request = asyncio.create_task(app(scope, receive, send))
        try:
            async with asyncio.timeout(2):
                await streamed.wait()
                await delegated.wait()
            assert not request.done(), "SSE must stay open until the live channel disconnects"
            assert next(m for m in messages if m["type"] == "http.response.start")["status"] == 200
            text = b"".join(bodies)
            assert b'"sequence": 9' in text or b'"sequence":9' in text
        finally:
            disconnect.set()
            try:
                async with asyncio.timeout(2):
                    await request
            finally:
                request.cancel()
                await asyncio.gather(request, return_exceptions=True)

    asyncio.run(scenario())
