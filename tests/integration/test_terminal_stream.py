"""Terminal output stream and recovery.

Ring retention and resume, ordered client assembly, split-sequence
buffering, parser-state preservation, slow-viewer bounds, reconnect
resume vs explicit reset, controller-only resize, and VT fixtures
compared as expected screen cells/text through the headless projection
oracle. No writes to any live Herdr daemon (attach sessions use stub
binaries; the upstream resize fact is pinned against the frozen
0.9.1 schema export).
"""

from __future__ import annotations

import asyncio
import json
import queue
import stat
import struct
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest
import websockets.asyncio.server as ws_server
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from relaydeck.client.features.sync.controller import TerminalStreamClient
from relaydeck.client.features.sync.transport import (
    TerminalSocket,
    TransportError,
    WebsocketTerminalSocket,
)
from relaydeck.client.features.terminal.controller import TerminalController
from relaydeck.client.features.terminal.projection import TerminalProjection
from relaydeck.client.features.terminal.stream import (
    ACTION_CONTINUE,
    ACTION_REATTACH,
    ACTION_RESUME,
    EMULATOR_FRESH,
    EMULATOR_PRESERVED,
    EVENT_DATA,
    EVENT_GAP,
    EVENT_RESET,
    INCOMPLETE_HISTORY,
    VIEWER_OVERFLOW_HISTORY,
    ResumeRequest,
    StreamEvent,
    TerminalByteStream,
    decide_recovery,
    decide_viewer_overflow,
)
from relaydeck.connector.attach import AttachError, AttachSession, apply_controller_resize
from relaydeck.gateway import terminal_wire
from relaydeck.gateway.app import GatewayState, create_app
from relaydeck.gateway.backend import BackendError, FakeBackend, ResizeRequest
from relaydeck.gateway.identity import TicketGrant
from relaydeck.gateway.ring import (
    READ_CHUNK_MAX,
    RING_MAX_AGE_MS,
    RING_MAX_BYTES,
    VIEWER_QUEUE_MAX_BYTES,
    RingStore,
)

VT = Path(__file__).resolve().parent.parent / "fixtures" / "vt"
GATEWAY_FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "gateway"
UPSTREAM = Path(__file__).resolve().parent.parent / "fixtures" / "upstream"
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
HOSTS = frozenset({"home-mac"})
NOW = 1_000_000


class _Clock:
    def __init__(self, now: int = NOW) -> None:
        self.now = now

    def __call__(self) -> int:
        return self.now


def _client() -> tuple[TestClient, GatewayState, FakeBackend, _Clock]:
    clock = _Clock()
    backend = FakeBackend(now_ms=clock)
    doc: dict[str, Any] = json.loads((GATEWAY_FIXTURES / "snapshot.json").read_text())
    doc["cursor"] = "snap-cursor-1"
    backend.snapshots["home-mac"] = doc
    state = GatewayState(backend=backend, cursor_secret=SECRET, now_ms=clock)
    return TestClient(create_app(state)), state, backend, clock


def _token(state: GatewayState, device: str = "dev-1") -> str:
    access, _ = state.sessions.create("acct-1", ALL_CAPS, HOSTS, NOW, device)
    return access


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _attach(client: TestClient, token: str, terminal: str = "term-1") -> Any:
    return client.post(
        f"/v1/hosts/home-mac/terminals/{terminal}/attachments",
        headers=_headers(token),
        json={"kind": "shell", "takeover": False, "cols": 80, "rows": 24},
    )


def _lease(client: TestClient, token: str, attachment_id: str, mode: str) -> Any:
    return client.post(
        f"/v1/terminal-attachments/{attachment_id}/leases",
        headers=_headers(token),
        json={"mode": mode},
    )


def _ticket(client: TestClient, token: str, lease_id: str) -> str:
    r = client.post("/v1/terminal-tickets", headers=_headers(token), json={"lease_id": lease_id})
    assert r.status_code == 200, r.text
    ticket: str = r.json()["ticket"]
    return ticket


def _receive_bytes_bounded(ws: Any, timeout_s: float = 15.0) -> bytes:
    """Blocking TestClient receive with a failure timeout: server bugs
    that send nothing (blank-stream regressions) fail the test instead
    of parking the suite on a bare `receive_bytes()` forever."""
    box: queue.Queue[Any] = queue.Queue()

    def _recv() -> None:
        try:
            box.put(("ok", ws.receive_bytes()))
        except Exception as exc:  # noqa: BLE001 - re-raised on the test thread
            box.put(("error", exc))

    worker = threading.Thread(target=_recv, daemon=True)
    worker.start()
    try:
        kind, payload = box.get(timeout=timeout_s)
    except queue.Empty:
        raise AssertionError(f"timed out after {timeout_s}s waiting for stream bytes") from None
    if kind == "error":
        raise payload
    assert isinstance(payload, bytes)
    return payload


class _ScriptedSocket(TerminalSocket):
    def __init__(self, inbound: list[tuple[str, Any]]) -> None:
        self.inbound = list(inbound)
        self.sent_bytes: list[bytes] = []
        self.sent_text: list[str] = []
        self.connected = False
        self.closed = False

    async def connect(self) -> None:
        self.connected = True

    async def send_bytes(self, data: bytes) -> None:
        self.sent_bytes.append(data)

    async def send_text(self, data: str) -> None:
        self.sent_text.append(data)

    async def receive(self) -> tuple[str, Any]:
        if not self.inbound:
            raise TransportError("host_offline", "eof")
        await asyncio.sleep(0)
        return self.inbound.pop(0)

    async def close(self) -> None:
        self.closed = True


def _project(*names: str) -> TerminalProjection:
    projection = TerminalProjection()
    for name in names:
        projection.feed((VT / name).read_bytes())
    projection.flush()
    return projection


# -- VT fixtures as screen cells ------------------------------------------------


def test_fixture_ascii_colors() -> None:
    want = json.loads((VT / "01_ascii_colors.json").read_text())["lines"]
    assert _project("01_ascii_colors.bin").lines == want


def test_fixture_cursor_addressing() -> None:
    want = json.loads((VT / "02_cursor_addressing.json").read_text())["lines"]
    assert _project("02_cursor_addressing.bin").lines == want


def test_fixture_erased_line() -> None:
    want = json.loads((VT / "03_erased_line.json").read_text())["lines"]
    assert _project("03_erased_line.bin").lines == want


def test_fixture_alt_screen_preserves_main() -> None:
    spec = json.loads((VT / "04_alt_screen.json").read_text())
    projection = TerminalProjection()
    projection.feed((VT / "04a_alt_enter_prefix.bin").read_bytes())
    assert projection.alt_active is False
    projection.feed((VT / "04b_alt_content.bin").read_bytes())
    assert projection.alt_active is True
    assert projection.lines == spec["lines_during_alt"]
    projection.feed((VT / "04c_alt_exit.bin").read_bytes())
    projection.flush()
    assert projection.alt_active is False
    assert projection.lines == spec["lines_after_exit"]


def test_fixture_wide_emoji() -> None:
    want = json.loads((VT / "05_wide_emoji.json").read_text())["lines"]
    assert _project("05_wide_emoji.bin").lines == want


def test_fixture_combining_stored_not_normalized() -> None:
    want = json.loads((VT / "06_combining.json").read_text())["lines"]
    got = _project("06_combining.bin").lines
    assert got == want
    assert got[0].encode("unicode_escape") == b"e\\u0301clair"


def test_fixture_bracketed_paste_mode_flips() -> None:
    projection = TerminalProjection()
    projection.feed((VT / "07a_bracketed_on.bin").read_bytes())
    assert projection.bracketed_paste is True
    projection.feed((VT / "07b_bracketed_off.bin").read_bytes())
    projection.flush()
    assert projection.bracketed_paste is False


def test_fixture_scrollback_suffix_and_history() -> None:
    want = json.loads((VT / "08_scrollback.json").read_text())["suffix_lines"]
    projection = _project("08_scrollback.bin")
    assert projection.lines == want
    assert projection.scrollback == [f"L{i:02d}" for i in range(1, 8)]


def test_split_delivery_identical_at_every_cut() -> None:
    raw = (VT / "09_split_sequences.bin").read_bytes()
    want = _project("09_split_sequences.bin").lines
    assert want == ["color red \U0001f600 tail"]
    for cut in range(len(raw) + 1):
        projection = TerminalProjection()
        projection.feed(raw[:cut])
        projection.feed(raw[cut:])
        projection.flush()
        assert projection.lines == want, f"cut at {cut}"


@pytest.mark.parametrize(
    "name",
    [
        "01_ascii_colors.bin",
        "02_cursor_addressing.bin",
        "03_erased_line.bin",
        "05_wide_emoji.bin",
        "06_combining.bin",
        "08_scrollback.bin",
    ],
)
def test_byte_by_byte_matches_whole(name: str) -> None:
    raw = (VT / name).read_bytes()
    whole = _project(name)
    incremental = TerminalProjection()
    for byte in raw:
        incremental.feed(bytes([byte]))
    incremental.flush()
    assert incremental.lines == whole.lines
    assert incremental.scrollback == whole.scrollback


def test_flush_drops_truncated_escape() -> None:
    projection = TerminalProjection()
    projection.feed(b"ab\x1b[3")
    assert projection.lines == ["ab"]
    projection.flush()
    assert projection.lines == ["ab"]
    # The same bytes completed across a chunk boundary parse normally.
    projection = TerminalProjection()
    projection.feed(b"ab\x1b[3")
    projection.feed(b"1mX")
    projection.flush()
    assert projection.lines == ["abX"]


# -- ring retention and resume --------------------------------------------------


def test_ring_resume_exact_continuation() -> None:
    store = RingStore()
    fanout = store.rotate("att-1", "e1", NOW)
    fanout.publish(b"boot\r\n", NOW)
    fanout.publish(b"$ ", NOW)
    ring = fanout.ring
    assert (ring.base_offset, ring.end_offset) == (0, 8)
    read = ring.read("e1", 0)
    assert read.status == "ok"
    assert b"".join(data for _, data in read.chunks) == b"boot\r\n$ "
    assert [offset for offset, _ in read.chunks] == [0, 6]
    assert ring.read("e1", 6).chunks == ((6, b"$ "),)
    assert ring.read("e1", 8).chunks == ()
    assert ring.read("e1", 8).status == "ok"


def test_ring_byte_bound_evicts_oldest() -> None:
    store = RingStore()
    fanout = store.rotate("att-1", "e1", NOW)
    segment = b"x" * 65536
    total = 0
    for _ in range(40):  # 2.5 MiB through a 2 MiB ring
        fanout.publish(segment, NOW)
        total += len(segment)
    ring = fanout.ring
    assert ring.end_offset == total
    assert ring.retained_bytes <= RING_MAX_BYTES
    assert ring.base_offset == total - ring.retained_bytes > 0
    stale = ring.read("e1", 0)
    assert stale.status == "stale_offset"
    assert stale.chunks == ()  # no bytes: never a forged tail
    live = ring.read("e1", ring.base_offset)
    assert live.status == "ok"
    assert b"".join(data for _, data in live.chunks) == b"x" * ring.retained_bytes
    assert all(len(data) <= READ_CHUNK_MAX for _, data in live.chunks)


def test_ring_time_bound_evicts_aged_bytes() -> None:
    store = RingStore()
    fanout = store.rotate("att-1", "e1", NOW)
    fanout.publish(b"old", NOW)
    fanout.publish(b"new", NOW + RING_MAX_AGE_MS + 1)
    ring = fanout.ring
    assert ring.retained_bytes == 3
    assert ring.base_offset == 3
    stale = ring.read("e1", 0)
    assert stale.status == "stale_offset"
    assert stale.chunks == ()
    assert ring.read("e1", 3).chunks == ((3, b"new"),)


def test_ring_epoch_rotation_forces_reset() -> None:
    store = RingStore()
    fanout = store.rotate("att-1", "e1", NOW)
    fanout.publish(b"before", NOW)
    fanout.rotate("e2")
    ring = fanout.ring
    assert (ring.base_offset, ring.end_offset, ring.retained_bytes) == (0, 0, 0)
    stale = ring.read("e1", 0)
    assert stale.status == "stale_epoch"
    assert stale.chunks == ()
    assert ring.read("e2", 0).status == "ok"


def test_ring_single_huge_append_keeps_tail() -> None:
    store = RingStore()
    fanout = store.rotate("att-1", "e1", NOW)
    fanout.publish(b"z" * (RING_MAX_BYTES + 1024), NOW)
    ring = fanout.ring
    assert ring.retained_bytes == RING_MAX_BYTES
    assert ring.base_offset == 1024
    assert ring.read("e1", 0).status == "stale_offset"
    live = ring.read("e1", 1024)
    assert live.status == "ok"
    assert sum(len(data) for _, data in live.chunks) == RING_MAX_BYTES


def test_ring_future_offset_is_stale() -> None:
    store = RingStore()
    fanout = store.rotate("att-1", "e1", NOW)
    fanout.publish(b"abc", NOW)
    read = fanout.ring.read("e1", 4)
    assert read.status == "stale_offset"
    assert read.chunks == ()


# -- per-viewer bounded queues --------------------------------------------------


def test_slow_viewer_overflows_alone() -> None:
    store = RingStore()
    fanout = store.rotate("att-1", "e1", NOW)
    slow = fanout.subscribe("slow")
    fast = fanout.subscribe("fast")
    segment = b"v" * 65536
    for _ in range(17):  # 1088 KiB offered; slow never pops
        fanout.publish(segment, NOW)
        for _ in fast.pop_all():
            pass
    assert slow.overflowed is True
    assert slow.overflow_at_offset == 16 * 65536
    assert slow.pending_bytes <= VIEWER_QUEUE_MAX_BYTES
    assert slow.push(1 << 20, b"more") is False  # latched until reattach
    assert fast.overflowed is False
    assert fanout.ring.retained_bytes == 17 * 65536
    # Reattach clears the latch and the stream continues for the slow viewer.
    slow.reset("e1")
    assert slow.overflowed is False
    assert fanout.publish(b"again", NOW) == (17 * 65536, 17 * 65536 + 5)
    assert slow.pop_all() == ((17 * 65536, b"again"),)


def test_viewer_queue_reset_cycle_on_rotation() -> None:
    store = RingStore()
    fanout = store.rotate("att-1", "e1", NOW)
    queue = fanout.subscribe("viewer")
    fanout.publish(b"data", NOW)
    assert len(queue.pop_all()) == 1
    fanout.rotate("e2")
    assert queue.reset_pending is True
    assert queue.push(0, b"stale-offer") is False  # mixed streams never deliver
    queue.reset("e2")
    assert queue.reset_pending is False
    fanout.publish(b"fresh", NOW)
    assert queue.pop_all() == ((0, b"fresh"),)


# -- backend stream plumbing ----------------------------------------------------


def test_backend_publish_and_read_stream() -> None:
    backend = FakeBackend(now_ms=_Clock())
    attachment, displaced = backend.create_attachment(
        "home-mac", "term-1", "shell", "acct-1", "dev-1", takeover=False
    )
    assert displaced == ()
    lease = backend.acquire_control(attachment.attachment_id, "acct-1", "dev-1")
    assert backend.publish_output(attachment.attachment_id, "e1", b"hello ") == (0, 6)
    assert backend.publish_output(attachment.attachment_id, "e1", b"world") == (6, 11)
    batch = backend.read_stream(lease.lease_id, "e1", 0)
    assert batch.status == "ok"
    assert batch.epoch == "e1"
    assert b"".join(data for _, data in batch.chunks) == b"hello world"
    assert batch.next_offset == 11
    resumed = backend.read_stream(lease.lease_id, "e1", 6)
    assert resumed.status == "ok"
    assert resumed.chunks == ((6, b"world"),)


def test_backend_takeover_rotates_ring_and_displaces() -> None:
    backend = FakeBackend(now_ms=_Clock())
    attachment, _ = backend.create_attachment(
        "home-mac", "term-1", "shell", "acct-1", "dev-1", takeover=False
    )
    lease = backend.acquire_control(attachment.attachment_id, "acct-1", "dev-1")
    backend.publish_output(attachment.attachment_id, "e1", b"old-stream")
    retaken, displaced = backend.create_attachment(
        "home-mac", "term-1", "shell", "acct-1", "dev-2", takeover=True
    )
    assert retaken.epoch == "e2"
    assert displaced == (lease.lease_id,)
    with pytest.raises(BackendError) as exc:
        backend.read_stream(lease.lease_id, "e1", 0)
    assert exc.value.code == "lease_expired"
    fresh = backend.acquire_control(retaken.attachment_id, "acct-1", "dev-2")
    assert backend.read_stream(fresh.lease_id, "e1", 0).status == "stale_epoch"
    assert backend.read_stream(fresh.lease_id, "e2", 0).status == "ok"
    backend.publish_output(retaken.attachment_id, "e2", b"new")
    assert backend.read_stream(fresh.lease_id, "e2", 0).chunks == ((0, b"new"),)


def test_backend_publish_wrong_epoch_rejected() -> None:
    backend = FakeBackend(now_ms=_Clock())
    attachment, _ = backend.create_attachment(
        "home-mac", "term-1", "shell", "acct-1", "dev-1", takeover=False
    )
    with pytest.raises(BackendError) as exc:
        backend.publish_output(attachment.attachment_id, "e9", b"nope")
    assert exc.value.code == "stale_epoch"


def test_backend_close_drops_ring() -> None:
    backend = FakeBackend(now_ms=_Clock())
    attachment, _ = backend.create_attachment(
        "home-mac", "term-1", "shell", "acct-1", "dev-1", takeover=False
    )
    backend.publish_output(attachment.attachment_id, "e1", b"data")
    assert len(backend.rings) == 1
    backend.close_attachment(attachment.attachment_id, "acct-1")
    assert len(backend.rings) == 0


# -- gateway WebSocket resume and reset ------------------------------------------


def _setup_stream() -> tuple[TestClient, FakeBackend, str, str]:
    client, state, backend, _ = _client()
    token = _token(state)
    response = _attach(client, token)
    assert response.status_code == 201, response.text
    return client, backend, token, response.json()["attachment_id"]


def test_ws_ticket_only_first_connect_replays_ring_backlog() -> None:
    """Production clients open ticket-only (no resume params): bytes
    published BEFORE the socket connects (attach-time greeting order:
    publish, then lease, then ticket, then WS) must replay from the
    ring. Browser regression: the ticket-only branch read the lease
    output log that nothing publishes to, so the terminal stayed
    blank at @0 despite a live stream. The receive runs under a
    thread timeout (TestClient receives block): a pre-fix failure
    times out instead of parking the suite forever."""
    client, backend, token, att_id = _setup_stream()
    backend.publish_output(att_id, "e1", b"hello-backlog")
    lease_id = _lease(client, token, att_id, "control").json()["lease_id"]
    ticket = _ticket(client, token, lease_id)
    with client.websocket_connect(f"/v1/terminal/{lease_id}?ticket={ticket}") as ws:
        chunk = terminal_wire.decode_output(_receive_bytes_bounded(ws, timeout_s=15.0))
        ws.send_text(json.dumps({"kind": "bye"}))
    assert (chunk.epoch, chunk.offset, chunk.data) == ("e1", 0, b"hello-backlog")


def test_ws_first_connect_full_replay() -> None:
    client, backend, token, att_id = _setup_stream()
    lease_id = _lease(client, token, att_id, "control").json()["lease_id"]
    raw = (VT / "09_split_sequences.bin").read_bytes()
    backend.publish_output(att_id, "e1", raw[:10])
    backend.publish_output(att_id, "e1", raw[10:])
    ticket = _ticket(client, token, lease_id)
    with client.websocket_connect(
        f"/v1/terminal/{lease_id}?ticket={ticket}&epoch=e1&offset=0"
    ) as ws:
        first = terminal_wire.decode_output(ws.receive_bytes())
        second = terminal_wire.decode_output(ws.receive_bytes())
        ws.send_text(json.dumps({"kind": "bye"}))
    assert (first.epoch, first.offset) == ("e1", 0)
    assert (second.epoch, second.offset) == ("e1", 10)
    assert first.data + second.data == raw


def test_ws_resume_exact_continuation() -> None:
    client, backend, token, att_id = _setup_stream()
    lease_id = _lease(client, token, att_id, "control").json()["lease_id"]
    backend.publish_output(att_id, "e1", b"0123456789")
    ticket = _ticket(client, token, lease_id)
    with client.websocket_connect(
        f"/v1/terminal/{lease_id}?ticket={ticket}&epoch=e1&offset=6"
    ) as ws:
        chunk = terminal_wire.decode_output(ws.receive_bytes())
        ws.send_text(json.dumps({"kind": "bye"}))
    assert (chunk.epoch, chunk.offset, chunk.data) == ("e1", 6, b"6789")


def test_ws_split_sequences_reassemble_over_wire() -> None:
    client, backend, token, att_id = _setup_stream()
    lease_id = _lease(client, token, att_id, "view").json()["lease_id"]
    raw = (VT / "09_split_sequences.bin").read_bytes()
    for byte in raw:  # worst case: one byte per publish
        backend.publish_output(att_id, "e1", bytes([byte]))
    ticket = _ticket(client, token, lease_id)
    frames: list[bytes] = []
    with client.websocket_connect(
        f"/v1/terminal/{lease_id}?ticket={ticket}&epoch=e1&offset=0"
    ) as ws:
        for _ in range(len(raw)):
            frames.append(ws.receive_bytes())
        ws.send_text(json.dumps({"kind": "bye"}))
    decoded = [terminal_wire.decode_output(frame) for frame in frames]
    assert [chunk.offset for chunk in decoded] == list(range(len(raw)))
    assert b"".join(chunk.data for chunk in decoded) == raw
    projection = TerminalProjection()
    for chunk in decoded:
        projection.feed(chunk.data)
    projection.flush()
    assert projection.lines == ["color red \U0001f600 tail"]


def test_ws_first_connect_live_edge_reports_base() -> None:
    client, backend, token, att_id = _setup_stream()
    lease_id = _lease(client, token, att_id, "view").json()["lease_id"]
    segment = b"L" * 65536
    for _ in range(40):
        backend.publish_output(att_id, "e1", segment)
    base = backend.rings.get(att_id).ring.base_offset  # type: ignore[union-attr]
    assert base > 0
    ticket = _ticket(client, token, lease_id)
    with client.websocket_connect(f"/v1/terminal/{lease_id}?ticket={ticket}&offset=0") as ws:
        chunk = terminal_wire.decode_output(ws.receive_bytes())
        ws.send_text(json.dumps({"kind": "bye"}))
    # Live edge, not byte 0: a fresh stream at 0 cannot consume it (gap
    # with zero bytes), so the client reopens exactly at the offered base
    # and labels its screen incomplete rather than whole.
    assert chunk.offset == base > 0
    first_try = TerminalByteStream()
    event = first_try.push(terminal_wire.OutputChunk(chunk.epoch, chunk.offset, chunk.data))
    assert event.kind == EVENT_GAP
    assert event.data == b""
    reopened = TerminalByteStream(chunk.epoch, chunk.offset)
    live = reopened.push(terminal_wire.OutputChunk(chunk.epoch, chunk.offset, chunk.data))
    assert live.kind == EVENT_DATA
    decision = decide_recovery(live, reopened, EMULATOR_FRESH, start_offset=live.offset)
    assert (decision.action, decision.notice) == (ACTION_CONTINUE, INCOMPLETE_HISTORY)


def test_ws_stale_offset_reset_doc() -> None:
    client, backend, token, att_id = _setup_stream()
    lease_id = _lease(client, token, att_id, "control").json()["lease_id"]
    for _ in range(40):
        backend.publish_output(att_id, "e1", b"z" * 65536)
    ticket = _ticket(client, token, lease_id)
    with client.websocket_connect(
        f"/v1/terminal/{lease_id}?ticket={ticket}&epoch=e1&offset=0"
    ) as ws:
        doc = json.loads(ws.receive_text())
        assert doc == {"kind": "reset", "detail": "stale_offset"}
        try:
            ws.receive_text()
            raise AssertionError("expected disconnect after reset")
        except WebSocketDisconnect as exc:
            assert exc.code == 1008


def test_ws_stale_epoch_reset_doc() -> None:
    client, backend, token, att_id = _setup_stream()
    lease_id = _lease(client, token, att_id, "control").json()["lease_id"]
    backend.publish_output(att_id, "e1", b"data")
    ticket = _ticket(client, token, lease_id)
    with client.websocket_connect(
        f"/v1/terminal/{lease_id}?ticket={ticket}&epoch=e9&offset=0"
    ) as ws:
        assert json.loads(ws.receive_text()) == {"kind": "reset", "detail": "stale_epoch"}
        try:
            ws.receive_text()
            raise AssertionError("expected disconnect after reset")
        except WebSocketDisconnect as exc:
            assert exc.code == 1008


def test_ws_bad_resume_rejected() -> None:
    client, _, token, att_id = _setup_stream()
    lease_id = _lease(client, token, att_id, "control").json()["lease_id"]
    for query in ("offset=abc", "offset=-1"):
        ticket = _ticket(client, token, lease_id)
        try:
            with client.websocket_connect(f"/v1/terminal/{lease_id}?ticket={ticket}&{query}"):
                raise AssertionError(f"expected reject for {query}")
        except WebSocketDisconnect as exc:
            assert exc.code == 1008


def test_ws_control_resize_recorded_view_rejected() -> None:
    client, backend, token, att_id = _setup_stream()
    control_id = _lease(client, token, att_id, "control").json()["lease_id"]
    ticket = _ticket(client, token, control_id)
    with client.websocket_connect(f"/v1/terminal/{control_id}?ticket={ticket}") as ws:
        ws.send_bytes(terminal_wire.encode_resize(control_id, 1, 1, 100, 30))
        ws.send_text(json.dumps({"kind": "bye"}))
    assert backend.resize_log == [ResizeRequest(control_id, 1, 1, 100, 30)]
    assert backend.input_logs[control_id] == [(1, 1, "resize", struct.pack(">HH", 100, 30))]

    view_id = _lease(client, token, att_id, "view").json()["lease_id"]
    ticket = _ticket(client, token, view_id)
    with client.websocket_connect(f"/v1/terminal/{view_id}?ticket={ticket}") as ws:
        ws.send_bytes(terminal_wire.encode_resize(view_id, 1, 1, 100, 30))
        assert json.loads(ws.receive_text()) == {
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
    assert view_id not in backend.input_logs


def test_ws_takeover_displaces_and_resets_stream() -> None:
    client, backend, token, att_id = _setup_stream()
    control_id = _lease(client, token, att_id, "control").json()["lease_id"]
    backend.publish_output(att_id, "e1", b"before-takeover")
    retake = client.post(
        "/v1/hosts/home-mac/terminals/term-1/attachments",
        headers=_headers(token),
        json={"kind": "shell", "takeover": True, "cols": 80, "rows": 24},
    )
    assert retake.status_code == 200, retake.text
    assert retake.json()["epoch"] == "e2"
    # The displaced lease cannot even mint a ticket (410 Gone).
    gone = client.post(
        "/v1/terminal-tickets", headers=_headers(token), json={"lease_id": control_id}
    )
    assert gone.status_code == 410
    # A fresh lease on the new epoch sees only the new stream from 0, and
    # the old epoch is explicitly stale (never a forged tail).
    fresh_id = _lease(client, token, att_id, "control").json()["lease_id"]
    backend.publish_output(att_id, "e2", b"after-takeover")
    ticket = _ticket(client, token, fresh_id)
    with client.websocket_connect(
        f"/v1/terminal/{fresh_id}?ticket={ticket}&epoch=e1&offset=0"
    ) as ws:
        assert json.loads(ws.receive_text()) == {"kind": "reset", "detail": "stale_epoch"}
    ticket = _ticket(client, token, fresh_id)
    with client.websocket_connect(
        f"/v1/terminal/{fresh_id}?ticket={ticket}&epoch=e2&offset=0"
    ) as ws:
        chunk = terminal_wire.decode_output(ws.receive_bytes())
        ws.send_text(json.dumps({"kind": "bye"}))
    assert (chunk.epoch, chunk.offset, chunk.data) == ("e2", 0, b"after-takeover")


# -- ordered client assembly and recovery decisions --------------------------------


def test_byte_stream_order_gap_reset() -> None:
    stream = TerminalByteStream()
    first = stream.push(terminal_wire.OutputChunk("e1", 0, b"ab"))
    assert (first.kind, first.offset, first.data) == (EVENT_DATA, 0, b"ab")
    assert stream.next_offset == 2
    gap = stream.push(terminal_wire.OutputChunk("e1", 9, b"skip"))
    assert gap.kind == EVENT_GAP
    assert gap.data == b""  # a gap carries no bytes
    assert stream.next_offset == 2  # position unchanged: resume continues here
    reset = stream.push(terminal_wire.OutputChunk("e2", 2, b"new"))
    assert reset.kind == EVENT_RESET
    assert reset.data == b""


def test_truncated_tail_never_presented_complete() -> None:
    # A mid-stream start with a fresh emulator continues the bytes but is
    # always labeled incomplete; only byte 0 starts clean.
    stream = TerminalByteStream()
    event = stream.push(terminal_wire.OutputChunk("e1", 500, b"tail"))
    assert event.kind == EVENT_GAP  # not data: nothing is delivered
    assert event.data == b""
    resumed = TerminalByteStream("e1", 500)
    live = resumed.push(terminal_wire.OutputChunk("e1", 500, b"tail"))
    assert live.kind == EVENT_DATA
    decision = decide_recovery(live, resumed, EMULATOR_FRESH, start_offset=500)
    assert (decision.action, decision.notice) == (ACTION_CONTINUE, INCOMPLETE_HISTORY)
    clean = TerminalByteStream()
    first = clean.push(terminal_wire.OutputChunk("e1", 0, b"head"))
    assert decide_recovery(first, clean, EMULATOR_FRESH).notice is None


def test_recovery_matrix() -> None:
    stream = TerminalByteStream("e1", 42)
    gap = StreamEvent(EVENT_GAP, 42, b"", "offset 42 -> 100")
    reset = StreamEvent(EVENT_RESET, 42, b"", "epoch e1 -> e2")
    data = StreamEvent(EVENT_DATA, 42, b"x")
    decision = decide_recovery(gap, stream, EMULATOR_PRESERVED)
    assert (decision.action, decision.resume) == (ACTION_RESUME, ResumeRequest("e1", 42))
    assert decision.notice is None  # exact continuation: no notice
    decision = decide_recovery(gap, stream, EMULATOR_FRESH)
    assert (decision.action, decision.notice) == (ACTION_REATTACH, INCOMPLETE_HISTORY)
    decision = decide_recovery(reset, stream, EMULATOR_PRESERVED)
    assert (decision.action, decision.notice) == (ACTION_REATTACH, INCOMPLETE_HISTORY)
    assert decide_recovery(data, stream, EMULATOR_FRESH).action == ACTION_CONTINUE
    assert decide_viewer_overflow().notice == VIEWER_OVERFLOW_HISTORY
    assert decide_viewer_overflow().action == ACTION_REATTACH


def test_stream_client_reset_routing() -> None:
    async def scenario() -> None:
        doc = ("text", json.dumps({"kind": "reset", "detail": "stale_offset"}))
        gaps: list[str] = []
        resets: list[str] = []
        client = TerminalStreamClient(
            socket_factory=lambda ticket: _ScriptedSocket([doc]),
            on_output=lambda offset, data: None,
            on_control=lambda doc: None,
            on_gap=gaps.append,
            on_reset=resets.append,
        )
        await client.open("lease-1", 1, "rdt_test")
        await client.run()
        assert resets == ["stale_offset"]
        assert gaps == []
        # Without a reset listener the message still surfaces via on_gap.
        gaps2: list[str] = []
        fallback = TerminalStreamClient(
            socket_factory=lambda ticket: _ScriptedSocket([doc]),
            on_output=lambda offset, data: None,
            on_control=lambda doc: None,
            on_gap=gaps2.append,
        )
        await fallback.open("lease-1", 1, "rdt_test")
        await fallback.run()
        assert gaps2 == ["stale_offset"]

    asyncio.run(scenario())


def test_client_resume_over_live_server() -> None:
    async def scenario() -> None:
        targets: list[str] = []
        received: list[tuple[str, Any]] = []

        async def capture(connection: Any, request: Any) -> None:
            targets.append(request.path)
            return None

        async def handler(connection: Any) -> None:
            await connection.send(terminal_wire.encode_output("e1", 6, b"world"))
            async for message in connection:
                if isinstance(message, bytes):
                    chunk = terminal_wire.decode_input(message)
                    received.append(("bytes", (chunk.input_seq, chunk.kind, chunk.data)))
                else:
                    doc = json.loads(message)
                    received.append(("text", doc))
                    if doc.get("kind") in ("ack", "bye"):
                        await connection.send(json.dumps({"kind": "bye"}))
                        return

        server = await ws_server.serve(handler, "127.0.0.1", 0, process_request=capture)
        port = server.sockets[0].getsockname()[1]
        try:
            outputs: list[tuple[int, bytes]] = []
            gaps: list[str] = []
            client = TerminalStreamClient(
                socket_factory=lambda ticket: WebsocketTerminalSocket(
                    f"ws://127.0.0.1:{port}/v1/terminal/lease-7?ticket={ticket}&epoch=e1&offset=6"
                ),
                on_output=lambda offset, data: outputs.append((offset, data)),
                on_control=lambda doc: None,
                on_gap=gaps.append,
            )
            await client.open("lease-7", 3, "rdt_live", "e1", 6)
            assert client.resume_query() == "epoch=e1&offset=6"
            await client.send_input(b"ls\n")
            runner = asyncio.ensure_future(client.run())
            deadline = time.monotonic() + 5
            while not outputs and time.monotonic() < deadline:
                await asyncio.sleep(0.01)
            assert outputs == [(6, b"world")]
            await client.ack()
            await asyncio.wait_for(runner, timeout=10)
            await client.close()
        finally:
            server.close()
            await server.wait_closed()
        assert outputs == [(6, b"world")]
        assert gaps == []
        assert targets and "epoch=e1" in targets[0] and "offset=6" in targets[0]
        assert ("bytes", (1, "text", b"ls\n")) in received
        assert {"kind": "ack", "applied": 11} in [r[1] for r in received if r[0] == "text"]

    asyncio.run(scenario())


# -- controller: resize gating and state ------------------------------------------


def test_controller_view_resize_never_sent() -> None:
    async def scenario() -> None:
        sock = _ScriptedSocket([("text", json.dumps({"kind": "bye"}))])
        seen: list[bytes] = []
        controller = TerminalController(
            socket_factory=lambda ticket: sock,
            lease_id="lease-1",
            generation=1,
            mode="view",
            on_bytes=seen.append,
        )
        await controller.start("rdt_test")
        assert await controller.request_resize(100, 30) is False
        assert controller.rejected_resizes == [(100, 30)]
        assert sock.sent_bytes == []
        await controller.run()

    asyncio.run(scenario())


def test_controller_control_resize_sent() -> None:
    async def scenario() -> None:
        sock = _ScriptedSocket([("text", json.dumps({"kind": "bye"}))])
        controller = TerminalController(
            socket_factory=lambda ticket: sock,
            lease_id="lease-1",
            generation=1,
            mode="control",
            on_bytes=lambda data: None,
        )
        await controller.start("rdt_test")
        assert await controller.request_resize(100, 30) is True
        assert await controller.request_resize(0, 30) is False  # bad geometry
        assert controller.rejected_resizes == []
        await controller.run()
        (frame,) = sock.sent_bytes
        chunk = terminal_wire.decode_input(frame)
        assert (chunk.kind, chunk.cols, chunk.rows) == ("resize", 100, 30)

    asyncio.run(scenario())


def test_controller_gap_resumes_when_preserved() -> None:
    async def scenario() -> None:
        sock = _ScriptedSocket(
            [
                ("bytes", terminal_wire.encode_output("e1", 0, b"hi")),
                ("bytes", terminal_wire.encode_output("e1", 9, b"skip")),
            ]
        )
        states: list[str] = []
        controller = TerminalController(
            socket_factory=lambda ticket: sock,
            lease_id="lease-1",
            generation=1,
            mode="control",
            on_bytes=lambda data: None,
            on_state=lambda state: states.append(state.status),
            emulator=EMULATOR_PRESERVED,
        )
        await controller.start("rdt_test")
        await controller.run()
        assert controller.pending_resume == ResumeRequest("e1", 2)
        assert controller.reattach_needed is False
        assert controller.state.status == "reconnecting"
        assert controller.state.notice is not None
        assert "Reconnecting" in controller.state.notice.text

    asyncio.run(scenario())


def test_controller_reset_reattaches_with_notice() -> None:
    async def scenario() -> None:
        sock = _ScriptedSocket(
            [
                ("bytes", terminal_wire.encode_output("e1", 0, b"hi")),
                ("bytes", terminal_wire.encode_output("e2", 2, b"new")),
            ]
        )
        controller = TerminalController(
            socket_factory=lambda ticket: sock,
            lease_id="lease-1",
            generation=1,
            mode="control",
            on_bytes=lambda data: None,
            emulator=EMULATOR_PRESERVED,
        )
        await controller.start("rdt_test")
        await controller.run()
        assert controller.reattach_needed is True
        assert controller.pending_resume is None
        assert controller.state.status == "reset"
        assert controller.state.notice is not None
        assert controller.state.notice.text == INCOMPLETE_HISTORY

    asyncio.run(scenario())


def test_controller_live_edge_reopen_flows_with_notice() -> None:
    async def scenario() -> None:
        first = _ScriptedSocket([("bytes", terminal_wire.encode_output("e1", 1000, b"late"))])
        second = _ScriptedSocket(
            [
                ("bytes", terminal_wire.encode_output("e1", 1000, b"late")),
                ("text", json.dumps({"kind": "bye"})),
            ]
        )
        sockets = [first, second]
        delivered: list[bytes] = []
        controller = TerminalController(
            socket_factory=lambda ticket: sockets.pop(0),
            lease_id="lease-1",
            generation=1,
            mode="view",
            on_bytes=delivered.append,
            emulator=EMULATOR_FRESH,
        )
        await controller.start("rdt_one")
        await controller.run()
        assert controller.pending_resume == ResumeRequest("e1", 1000)
        assert controller.reattach_needed is False
        resume = controller.pending_resume
        controller.pending_resume = None
        await controller.start("rdt_two", resume.epoch, resume.offset)
        await controller.run()
        assert delivered == [b"late"]
        assert controller.state.status == "live"
        assert controller.state.notice is not None
        assert controller.state.notice.text == INCOMPLETE_HISTORY

    asyncio.run(scenario())


def test_controller_viewer_overflow_resets() -> None:
    controller = TerminalController(
        socket_factory=lambda ticket: _ScriptedSocket([]),
        lease_id="lease-1",
        generation=1,
        mode="view",
        on_bytes=lambda data: None,
    )
    controller.on_viewer_overflow()
    assert controller.reattach_needed is True
    assert controller.state.status == "reset"
    assert controller.state.notice is not None
    assert controller.state.notice.text == VIEWER_OVERFLOW_HISTORY


# -- stress profiles and responsiveness --------------------------------------------


def test_stress_profiles_memory_bounded() -> None:
    clock = _Clock()
    backend = FakeBackend(now_ms=clock)
    attachment, _ = backend.create_attachment(
        "home-mac", "term-1", "shell", "acct-1", "dev-1", takeover=False
    )
    att_id = attachment.attachment_id
    # Profile 1: 100 KiB/s for 60 s (6 MiB through the ring, virtual clock).
    segment = b"s" * 65536
    started = time.monotonic()
    fed = 0
    for _ in range(96):
        backend.publish_output(att_id, "e1", segment)
        clock.now += 640  # ~100 KiB/s
        fed += len(segment)
    elapsed = time.monotonic() - started
    ring = backend.rings.get(att_id).ring  # type: ignore[union-attr]
    assert fed == 96 * 65536
    assert ring.retained_bytes <= RING_MAX_BYTES
    assert ring.end_offset == fed
    assert fed / max(elapsed, 1e-6) > 2 * 1024 * 1024  # generous floor, MiB/s
    # Profile 2: 1 MiB/s for 5 s on a fresh epoch.
    retaken, _ = backend.create_attachment(
        "home-mac", "term-1", "shell", "acct-1", "dev-1", takeover=True
    )
    fed = 0
    for _ in range(5):
        backend.publish_output(att_id, retaken.epoch, b"f" * (1024 * 1024))
        clock.now += 1000
        fed += 1024 * 1024
    ring = backend.rings.get(att_id).ring  # type: ignore[union-attr]
    assert ring.retained_bytes <= RING_MAX_BYTES
    assert ring.end_offset == fed


def test_control_plane_responsive_during_stream() -> None:
    clock = _Clock()
    backend = FakeBackend(now_ms=clock)
    attachment, _ = backend.create_attachment(
        "home-mac", "term-1", "shell", "acct-1", "dev-1", takeover=False
    )
    lease = backend.acquire_control(attachment.attachment_id, "acct-1", "dev-1")
    for _ in range(40):
        backend.publish_output(attachment.attachment_id, "e1", b"q" * 65536)
    started = time.monotonic()
    for _ in range(100):
        backend.renew_lease(lease.lease_id, "acct-1", "dev-1")
        clock.now += 10
    fanout = backend.rings.get(attachment.attachment_id)
    assert fanout is not None
    backend.read_stream(lease.lease_id, "e1", fanout.ring.end_offset)
    dead = backend.revoke_leases_device("acct-1", "dev-1")
    elapsed = time.monotonic() - started
    assert dead == (lease.lease_id,)
    assert elapsed < 1.0  # 100 renews + resume + revoke stay interactive


def test_projection_latency_bounded() -> None:
    chunk = (b"The quick brown fox jumps over the lazy dog.\r\n" * 1500)[:65536]
    assert len(chunk) == 65536
    projection = TerminalProjection()
    started = time.monotonic()
    projection.feed(chunk)
    projection.flush()
    elapsed = time.monotonic() - started
    assert projection.lines  # content landed
    assert elapsed < 2.0  # generous ceiling; real cost is milliseconds


# -- controller-side resize reaches the attach session --------------------------------

_ECHO_STUB = """#!{python}
import sys
sys.stdout.write("stub-ready\\r\\n")
sys.stdout.flush()
while True:
    data = sys.stdin.readline()
    if not data:
        break
    sys.stdout.write("echo:" + data)
    sys.stdout.flush()
"""


def _stub(tmp_path: Path, name: str, body: str) -> str:
    path = tmp_path / name
    path.write_text(body.format(python=sys.executable))
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


def test_apply_controller_resize_reaches_stub_pty(tmp_path: Path) -> None:
    import fcntl
    import termios

    session = AttachSession(argv=[_stub(tmp_path, "echo-herdr", _ECHO_STUB)])
    session.spawn()
    try:
        assert b"stub-ready" in session.expect_ready(b"stub-ready", timeout=5.0)
        assert apply_controller_resize(session, 100, 30) == (100, 30)
        assert (session.cols, session.rows) == (100, 30)
        assert session._fd is not None
        packed = fcntl.ioctl(session._fd, termios.TIOCGWINSZ, b"\x00" * 8)
        rows, cols, _, _ = struct.unpack("HHHH", packed)
        assert (cols, rows) == (100, 30)
        with pytest.raises(AttachError, match="bad size"):
            apply_controller_resize(session, 0, 30)
    finally:
        session.detach()
    assert session.running is False


def test_upstream_pane_resize_is_layout_only() -> None:
    """Pin the frozen 0.9.1 export: `pane.resize` carries direction/amount
    for split layout, never PTY cols/rows, so must not route
    controller geometry through it (winsize on the attach PTY instead)."""
    schema = json.loads((UPSTREAM / "herdr-0.9.1-schema.json").read_text())
    params = schema["schemas"]["request"]["$defs"]["PaneResizeParams"]
    assert params["required"] == ["direction"]
    assert set(params["properties"]) == {"direction", "amount", "pane_id"}
    assert "pane.resize" in (UPSTREAM / "live_methods.txt").read_text().split()


# -- fix round (review F1/F2/F3/F5) -------------------------------------------


def test_ws_epochless_nonzero_offset_is_stale() -> None:
    """F1: `?offset=5` without epoch knowledge is stale_epoch with zero
    bytes (read_stream docstring rule), never a byte-5 continuation."""
    client, backend, token, att_id = _setup_stream()
    lease_id = _lease(client, token, att_id, "control").json()["lease_id"]
    backend.publish_output(att_id, "e1", b"0123456789")
    batch = backend.read_stream(lease_id, "", 5)
    assert batch.status == "stale_epoch"
    assert batch.chunks == ()
    assert batch.epoch == "e1"
    ticket = _ticket(client, token, lease_id)
    with client.websocket_connect(f"/v1/terminal/{lease_id}?ticket={ticket}&offset=5") as ws:
        assert json.loads(ws.receive_text()) == {"kind": "reset", "detail": "stale_epoch"}
        try:
            ws.receive_bytes()
            raise AssertionError("expected disconnect after reset (zero bytes)")
        except WebSocketDisconnect as exc:
            assert exc.code == 1008


def test_ticket_cross_account_is_not_found() -> None:
    """F2: account B cannot mint a ticket for A's lease, even when B's
    session lists the same host (lease ids are guessable)."""
    client, state, _, _ = _client()
    mine = _token(state, device="dev-1")
    response = _attach(client, mine)
    assert response.status_code == 201, response.text
    lease_id = _lease(client, mine, response.json()["attachment_id"], "control").json()["lease_id"]
    other, _ = state.sessions.create("acct-2", ALL_CAPS, HOSTS, NOW, "dev-9")
    r = client.post("/v1/terminal-tickets", headers=_headers(other), json={"lease_id": lease_id})
    assert r.status_code == 404
    assert r.json()["code"] == "not_found"
    # The lease itself is untouched: the owner still mints.
    assert _ticket(client, mine, lease_id).startswith("rdt_")


def test_ws_foreign_ticket_rejected_without_bytes() -> None:
    """F2: a grant bound to another account dies at WS accept with zero
    bytes (belt-and-braces behind the ticket route)."""
    client, state, backend, _ = _client()
    token = _token(state, device="dev-1")
    response = _attach(client, token)
    att_id = response.json()["attachment_id"]
    lease_id = _lease(client, token, att_id, "control").json()["lease_id"]
    backend.publish_output(att_id, "e1", b"secret-bytes")
    # Fault injection: the route can no longer produce such a grant, so
    # the forged ticket is issued directly against the store.
    forged = state.tickets.issue(
        TicketGrant("acct-2", "dev-9", ALL_CAPS, HOSTS, lease_id, "home-mac"), NOW
    )
    try:
        with client.websocket_connect(f"/v1/terminal/{lease_id}?ticket={forged}") as ws:
            ws.receive_bytes()
        raise AssertionError("expected disconnect for foreign ticket")
    except WebSocketDisconnect as exc:
        assert exc.code == 1008


def test_ws_takeover_displacement_sends_reset_doc() -> None:
    """F3: a connected socket displaced by HTTP takeover receives the
    reset doc (not a bare close), and the exact server doc drives the
    client reset path: notice + reattach flag, run() clean."""
    client, backend, token, att_id = _setup_stream()
    lease_id = _lease(client, token, att_id, "control").json()["lease_id"]
    ticket = _ticket(client, token, lease_id)
    with client.websocket_connect(f"/v1/terminal/{lease_id}?ticket={ticket}") as ws:
        retake = client.post(
            "/v1/hosts/home-mac/terminals/term-1/attachments",
            headers=_headers(token),
            json={"kind": "shell", "takeover": True, "cols": 80, "rows": 24},
        )
        assert retake.status_code == 200, retake.text
        assert retake.json()["epoch"] == "e2"
        raw = ws.receive_text()
        assert json.loads(raw) == {"kind": "reset", "detail": "stale_epoch"}
        try:
            ws.receive_text()
            raise AssertionError("expected disconnect after reset")
        except WebSocketDisconnect as exc:
            assert exc.code == 1008
    assert backend.open_lease(lease_id) is None

    async def scenario() -> None:
        controller = TerminalController(
            socket_factory=lambda ticket: _ScriptedSocket([("text", raw)]),
            lease_id=lease_id,
            generation=1,
            mode="control",
            on_bytes=lambda data: None,
            emulator=EMULATOR_PRESERVED,
        )
        await controller.start("rdt_test")
        await controller.run()
        assert controller.reattach_needed is True
        assert controller.pending_resume is None
        assert controller.state.status == "reset"
        assert controller.state.notice is not None
        assert controller.state.notice.text == INCOMPLETE_HISTORY

    asyncio.run(scenario())


def test_backend_resize_bad_length_rejected() -> None:
    """F5: a malformed resize payload is BackendError forbidden (with a
    details reason), never struct.error."""
    backend = FakeBackend(now_ms=_Clock())
    attachment, _ = backend.create_attachment(
        "home-mac", "term-1", "shell", "acct-1", "dev-1", takeover=False
    )
    lease = backend.acquire_control(attachment.attachment_id, "acct-1", "dev-1")
    for seq, bad in enumerate((b"abc", b"\x00d\x00\x1e\x00", b""), start=1):
        with pytest.raises(BackendError) as exc:
            backend.write_input(lease.lease_id, 1, seq, "resize", bad)
        assert exc.value.code == "forbidden"
        assert exc.value.details == {"reason": "bad_resize"}
    assert backend.write_input(lease.lease_id, 1, 99, "resize", struct.pack(">HH", 100, 30)) is True
    assert backend.resize_log == [ResizeRequest(lease.lease_id, 1, 99, 100, 30)]
