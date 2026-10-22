"""DataChannel packet protocol for the RelayDeck terminal extension.

Python -> Dart (output and commands), first byte is the kind:
  0x01 WRITE u32be length + raw terminal bytes
  0x03 INPUT_ENABLED 0x00/0x01 (Composer display-only vs Direct Input)
  0x06 REQUEST_SELECTION_TEXT (no body)
  0x07 CLEAR_SELECTION (no body)
  0x08 RESET (no body; emulator reset after gap/reset)
  0x09 REQUEST_SNAPSHOT u32be request id (emulator buffer snapshot)
  0x0A REQUEST_KEYBOARD (no body; open the soft keyboard when input is enabled)
  0x0B SCROLL_VIEWPORT 1 signed byte: -n/+n = pages up/down (n 1..10),
       0x7F = jump to bottom (local view scroll only, never the PTY)
  0x0C RESUME_PROBE u32be nonzero request id (same-engine rebind probe)
  0x0D RESUME_VERIFIED (no body; the resume positively completed on this
       binding — Dart releases its transport-loss input fence. Sent last,
       after the redelivered suffix and the input-truth re-assert, and
       never on an incomplete outcome.)
  0x0E RELEASE_KEYBOARD (no body; hide the soft keyboard and disarm touch
       input; never writes to the PTY)
  0x0F KEYBOARD_BUTTON 0x00/0x01 (show the native keyboard button in the view)
  0x10 CANCEL_SCROLL u32be seq (retire widget scroll motion — wheel queue,
       momentum coast, pending release velocity — then ack the seq with
       0x8A. Sent before a server-side key/paste write so stale wheel rows
       already in flight drop instead of landing after the key. Only
       sent to a viewer that proved it acks: see 0x8A seq 0.)

Dart -> Python:
  0x81 INPUT origin u8 + u32be length + bytes from the emulator:
       origin 1 = committed text (IME / soft keyboard)
       origin 2 = encoded key event (hardware keys via xterm2)
       origin 3 = mouse report (SGR wheel in TUI mouse mode)
       origin 4 = swipe gesture (alt buffer, no mouse reporting): exactly
       2 bytes, direction u8 (0 = up toward older output, 1 = down
       toward latest) + line count u8 (1..255). Never PTY bytes: Python
       translates to bounded PageUp/PageDown and drops anything
       malformed or out of mode. Never raw arrows.
  0x82 SIZE_REPORT u16be cols + u16be rows (measured view size)
  0x83 SELECTION_TEXT u32be length + utf-8 text (reply to request)
  0x84 SNAPSHOT_CHUNK u32be length + u32be request id + u32be total
       bytes + u32be offset + bytes (chunked reply to 0x09; reassemble
       by id, complete when coverage reaches total)
  0x85 MODE_REPORT 0x00/0x01 (live xterm2 `bracketedPasteMode`, sent
       after every WRITE and RESET: paste framing follows engine truth,
       never a Python-side re-parse of the output bytes)
  0x86 VIEW_REPORT 1 byte bitfield: bit0 alt buffer, bit1 at bottom,
       bit2 mouse reporting on (sent on change only, once per frame)
  0x87 RESUME_REPORT u32be req + u64be instance + u32be resets + u64be
       applied (reply to 0x0C; req 0 is the unsolicited mount hello)
  0x8A SCROLL_CANCELLED u32be seq (the widget retired its scroll motion
       for the 0x10 with that seq; wheel reports after the matching ack
       belong to a fresh gesture. Seq 0, sent unsolicited at mount, is
       the capability advertisement and releases nothing.)
  0x89 KEYBOARD_TRACE event u8 + flags u8 + visual viewport height u16be
       (touch only; fixed keyboard metadata, 0xFFFF = unavailable)
  0x8B PAGE_METRICS reset u8 + page scroll i16be + visual viewport offset
       i16be + visual viewport height u16be + window height u16be (touch web
       only; reset 1 = the page had scrolled and was pinned back to the top)
  0xFF ACK u64be cumulative applied output bytes (backpressure)

Snapshot trust boundary: the declared total/offset arrive from the
Dart side and are NOT bounded by the frame-size check above, so the
receiver validates them against MAX_SNAPSHOT_TOTAL /
MAX_SNAPSHOT_INFLIGHT_BYTES BEFORE allocating any reassembly buffer
and fails the waiter with a visible error on violation. Engines keep
maxLines x cols within MAX_SNAPSHOT_TOTAL; larger truths are rejected,
never silently truncated.

Input direction rationale: physical keys are
encoded Dart-side by xterm2 and cross to Python as opaque INPUT bytes;
Python tools buttons and reviewed paste are encoded Python-side
and travel to the PTY through the gateway lease path, never
through Dart. There is intentionally no Python->Dart key packet.
"""

from __future__ import annotations

import codecs
import contextvars
import re
import struct
from dataclasses import dataclass

WRITE = 0x01
INPUT_ENABLED = 0x03
REQUEST_SELECTION_TEXT = 0x06
CLEAR_SELECTION = 0x07
RESET = 0x08
REQUEST_SNAPSHOT = 0x09
REQUEST_KEYBOARD = 0x0A
SCROLL_VIEWPORT = 0x0B
RESUME_PROBE = 0x0C
RESUME_VERIFIED = 0x0D
RELEASE_KEYBOARD = 0x0E
KEYBOARD_BUTTON = 0x0F
CANCEL_SCROLL = 0x10

INPUT = 0x81
SIZE_REPORT = 0x82
SELECTION_TEXT = 0x83
SNAPSHOT_CHUNK = 0x84
MODE_REPORT = 0x85
VIEW_REPORT = 0x86
KEYBOARD_STATE = 0x88
KEYBOARD_TRACE = 0x89
SCROLL_CANCELLED = 0x8A
PAGE_METRICS = 0x8B
PAGE_METRICS_LEN = 9
RESUME_REPORT = 0x87
ACK = 0xFF

MAX_INPUT_CHUNK = 16 * 1024  # Maximum input chunk.
MAX_WRITE_CHUNK = 64 * 1024
VIEWER_QUEUE_LIMIT = 1024 * 1024  # per-viewer queue bound
MAX_SNAPSHOT_TOTAL = 4 * 1024 * 1024  # largest single snapshot truth (decl.)
MAX_SNAPSHOT_INFLIGHT = 8  # concurrent snapshot requests per terminal
MAX_SNAPSHOT_INFLIGHT_BYTES = 16 * 1024 * 1024  # sum of declared totals

ORIGIN_TEXT = 1
ORIGIN_KEY = 2
ORIGIN_MOUSE = 3
ORIGIN_SWIPE = 4

SWIPE_UP = 0
SWIPE_DOWN = 1
MAX_SWIPE_COUNT = 255
MAX_SWIPE_PAGES = 2

# Cancel epoch of the INPUT packet being dispatched, stamped synchronously so async handlers
# can tell a wheel report accepted before a cancel from a fresh gesture. -1: unstamped.
input_cancel_epoch: contextvars.ContextVar[int] = contextvars.ContextVar(
    "input_cancel_epoch", default=-1
)


def encode_write(data: bytes) -> bytes:
    if len(data) > MAX_WRITE_CHUNK:
        raise ValueError(f"write chunk {len(data)} exceeds {MAX_WRITE_CHUNK}")
    return struct.pack(">BI", WRITE, len(data)) + data


def encode_input_enabled(enabled: bool) -> bytes:
    return struct.pack(">BB", INPUT_ENABLED, 0x01 if enabled else 0x00)


def encode_request_selection_text() -> bytes:
    return struct.pack(">B", REQUEST_SELECTION_TEXT)


def encode_clear_selection() -> bytes:
    return struct.pack(">B", CLEAR_SELECTION)


def encode_request_keyboard() -> bytes:
    return struct.pack(">B", REQUEST_KEYBOARD)


def encode_release_keyboard() -> bytes:
    return struct.pack(">B", RELEASE_KEYBOARD)


def encode_keyboard_button(enabled: bool) -> bytes:
    return struct.pack(">BB", KEYBOARD_BUTTON, 0x01 if enabled else 0x00)


def encode_keyboard_state(shown: bool) -> bytes:
    """Dart -> Python: the keyboard is shown (armed) or hidden. Used by the viewer and
    by tests; Python never sends it."""
    return struct.pack(">BB", KEYBOARD_STATE, 0x01 if shown else 0x00)


def decode_keyboard_trace(payload: bytes) -> tuple[int, int, int]:
    """Decode fixed touch-keyboard metadata; never text or terminal identity."""
    if len(payload) != 4:
        raise ValueError("bad keyboard trace length")
    event, flags, viewport = struct.unpack(">BBH", payload)
    if event not in range(1, 10) or flags & ~0x1F:
        raise ValueError("bad keyboard trace flags or event")
    return event, flags, viewport


def decode_page_metrics(payload: bytes) -> tuple[int, int, int, int, int]:
    """Decode page geometry only: scroll and viewport sizes, never content."""
    if len(payload) != PAGE_METRICS_LEN:
        raise ValueError("bad page metrics length")
    reset, scroll, offset, height, window = struct.unpack(">BhhHH", payload)
    if reset not in (0, 1):
        raise ValueError("bad page metrics reset flag")
    return reset, scroll, offset, height, window


def encode_reset() -> bytes:
    return struct.pack(">B", RESET)


def encode_cancel_scroll(seq: int) -> bytes:
    """Encode a CANCEL_SCROLL packet: the caller\'s nonzero cancel sequence."""
    if isinstance(seq, bool) or not isinstance(seq, int):
        raise ValueError(f"bad cancel seq {seq!r}")
    if not 1 <= seq <= 0xFFFFFFFF:
        raise ValueError(f"bad cancel seq {seq!r}")
    return struct.pack(">BI", CANCEL_SCROLL, seq)


def encode_scroll_cancelled(seq: int) -> bytes:
    """Encode a SCROLL_CANCELLED packet (mirrors the Dart encoder; seq 0
    is the mount capability advertisement)."""
    if isinstance(seq, bool) or not isinstance(seq, int):
        raise ValueError(f"bad cancel seq {seq!r}")
    if not 0 <= seq <= 0xFFFFFFFF:
        raise ValueError(f"bad cancel seq {seq!r}")
    return struct.pack(">BI", SCROLL_CANCELLED, seq)


def decode_scroll_cancelled(payload: bytes) -> int:
    """Split a SCROLL_CANCELLED payload into its cancel sequence."""
    if len(payload) != 4:
        raise ValueError(f"bad SCROLL_CANCELLED payload length {len(payload)}")
    return struct.unpack(">I", payload)[0]


def encode_request_snapshot(req_id: int) -> bytes:
    return struct.pack(">BI", REQUEST_SNAPSHOT, req_id)


def encode_snapshot_chunk(req_id: int, total: int, offset: int, data: bytes) -> bytes:
    payload = struct.pack(">III", req_id, total, offset) + data
    if len(payload) > MAX_WRITE_CHUNK:
        raise ValueError(f"snapshot chunk {len(payload)} exceeds {MAX_WRITE_CHUNK}")
    return struct.pack(">BI", SNAPSHOT_CHUNK, len(payload)) + payload


def decode_snapshot_chunk(payload: bytes) -> tuple[int, int, int, bytes]:
    """Split a SNAPSHOT_CHUNK payload into (req_id, total, offset, bytes)."""
    if len(payload) < 12:
        raise ValueError("short SNAPSHOT_CHUNK payload")
    req_id, total, offset = struct.unpack_from(">III", payload, 0)
    return req_id, total, offset, payload[12:]


def encode_input(data: bytes, origin: int = ORIGIN_TEXT) -> bytes:
    if origin not in (ORIGIN_TEXT, ORIGIN_KEY, ORIGIN_MOUSE, ORIGIN_SWIPE):
        raise ValueError(f"bad origin {origin}")
    return struct.pack(">BBI", INPUT, origin, len(data)) + data


def encode_swipe(direction: int, count: int) -> bytes:
    """Encode a swipe INPUT packet: direction SWIPE_UP/SWIPE_DOWN and a
    line count 1..255. Anything else raises ValueError."""
    if isinstance(direction, bool) or direction not in (SWIPE_UP, SWIPE_DOWN):
        raise ValueError(f"bad swipe direction {direction!r}")
    if isinstance(count, bool) or not isinstance(count, int):
        raise ValueError(f"bad swipe count {count!r}")
    if not 1 <= count <= MAX_SWIPE_COUNT:
        raise ValueError(f"bad swipe count {count!r}")
    return encode_input(bytes([direction, count]), ORIGIN_SWIPE)


def decode_swipe(payload: bytes) -> tuple[int, int]:
    """Split a swipe payload into (direction, count), strictly."""
    if len(payload) != 2:
        raise ValueError(f"bad swipe payload length {len(payload)}")
    direction, count = payload[0], payload[1]
    if direction not in (SWIPE_UP, SWIPE_DOWN):
        raise ValueError(f"bad swipe direction {direction}")
    if not 1 <= count <= MAX_SWIPE_COUNT:
        raise ValueError(f"bad swipe count {count}")
    return direction, count


def decode_input(payload: bytes) -> tuple[int, bytes]:
    """Split an INPUT payload into (origin, bytes)."""
    if len(payload) < 1:
        raise ValueError("short INPUT payload")
    return payload[0], payload[1:]


def is_wheel_report(data: bytes) -> bool:
    """True for one vertical mouse wheel frame (SGR, urxvt or X10 byte
    forms): the same byte contract `scrolling.is_scroll_input` accepts
    without its view-state gate. Tells stale wheel rows from fresh
    widget input when releasing the wheel block."""
    try:
        text = data.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return False
    sgr = re.fullmatch(r"\x1b\[<(\d+);(\d+);(\d+)M", text)
    urxvt = re.fullmatch(r"\x1b\[(\d+);(\d+);(\d+)M", text)
    if sgr or urxvt:
        match = sgr or urxvt
        assert match is not None
        button, x, y = map(int, match.groups())
        if urxvt:
            button -= 32
    elif text.startswith("\x1b[M") and len(text) == 6:
        button, x, y = (ord(char) - 32 for char in text[3:])
    else:
        return False
    return button & ~28 in (64, 65) and 0 < x <= 65535 and 0 < y <= 65535


def encode_size_report(cols: int, rows: int) -> bytes:
    return struct.pack(">BHH", SIZE_REPORT, cols, rows)


def encode_selection_text(text: str) -> bytes:
    data = text.encode("utf-8")
    return struct.pack(">BI", SELECTION_TEXT, len(data)) + data


def encode_mode_report(bracketed: bool) -> bytes:
    return struct.pack(">BB", MODE_REPORT, 0x01 if bracketed else 0x00)


def encode_scroll_viewport(pages: int | None) -> bytes:
    """Encode a SCROLL_VIEWPORT packet: None jumps to bottom, else -10..-1
    pages up or 1..10 pages down. Anything else raises ValueError."""
    if pages is None:
        return struct.pack(">Bb", SCROLL_VIEWPORT, 0x7F)
    if isinstance(pages, bool) or not isinstance(pages, int):
        raise ValueError(f"scroll pages must be None or +-1..10, got {pages!r}")
    if pages == 0 or abs(pages) > 10:
        raise ValueError(f"scroll pages must be None or +-1..10, got {pages!r}")
    return struct.pack(">Bb", SCROLL_VIEWPORT, pages)


@dataclass(frozen=True)
class ViewState:
    alt: bool
    at_bottom: bool
    mouse: bool


def decode_view_report(payload: bytes) -> ViewState:
    """Split a VIEW_REPORT payload into a ViewState (bit0 alt, bit1 at
    bottom, bit2 mouse reporting on)."""
    if len(payload) < 1:
        raise ValueError("short VIEW_REPORT payload")
    bits = payload[0]
    return ViewState(
        alt=bool(bits & 0x01),
        at_bottom=bool(bits & 0x02),
        mouse=bool(bits & 0x04),
    )


def encode_view_report(state: ViewState) -> bytes:
    bits = (
        (0x01 if state.alt else 0) | (0x02 if state.at_bottom else 0) | (0x04 if state.mouse else 0)
    )
    return struct.pack(">BB", VIEW_REPORT, bits)


def encode_ack(applied: int) -> bytes:
    return struct.pack(">BQ", ACK, applied)


# Resume probe: after session reuse, verify continuity before rebinding the retained engine.
# The report echoes a nonzero request id with the Dart native instance id, applied-reset count
# and applied output bytes; id 0 is the mount hello that records the baseline. A resume with
# no baseline fails closed unless the terminal is provably zero-traffic. The report is
# fixed-size and length-checked exactly, so no declared length drives allocation.
RESUME_REPORT_LEN = 24


def encode_resume_probe(req_id: int) -> bytes:
    """Encode a RESUME_PROBE packet: nonzero u32 request id only."""
    if isinstance(req_id, bool) or not isinstance(req_id, int):
        raise ValueError(f"bad resume req id {req_id!r}")
    if not 1 <= req_id <= 0xFFFFFFFF:
        raise ValueError(f"bad resume req id {req_id!r}")
    return struct.pack(">BI", RESUME_PROBE, req_id)


def encode_resume_verified() -> bytes:
    """Encode a RESUME_VERIFIED packet: the current binding proved same-
    engine continuity, so Dart may release its transport-loss input
    fence. The sender emits it last and only on verified success."""
    return struct.pack(">B", RESUME_VERIFIED)


def encode_resume_report(req_id: int, instance: int, resets: int, applied: int) -> bytes:
    """Encode a RESUME_REPORT packet (mirrors the Dart encoder; req 0
    is the mount hello). Anything outside the u32/u64 ranges raises."""
    for name, value, bits in (
        ("req id", req_id, 32),
        ("instance", instance, 64),
        ("resets", resets, 32),
        ("applied", applied, 64),
    ):
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"bad resume {name} {value!r}")
        if not 0 <= value < (1 << bits):
            raise ValueError(f"bad resume {name} {value!r}")
    payload = struct.pack(">IQIQ", req_id, instance, resets, applied)
    assert len(payload) == RESUME_REPORT_LEN
    return struct.pack(">B", RESUME_REPORT) + payload


def decode_resume_report(payload: bytes) -> tuple[int, int, int, int]:
    """Split a RESUME_REPORT payload into (req_id, instance, resets,
    applied), strictly: exactly 24 bytes, no length prefix to trust."""
    if len(payload) != RESUME_REPORT_LEN:
        raise ValueError(f"bad resume report length {len(payload)}")
    return struct.unpack(">IQIQ", payload)


@dataclass(frozen=True)
class DecodedPacket:
    kind: int
    payload: bytes


def decode_packets(buffer: bytearray) -> list[DecodedPacket]:
    """Pop complete packets from buffer; leave a partial tail in place."""
    out: list[DecodedPacket] = []
    view = buffer
    offset = 0
    while offset < len(view):
        kind = view[offset]
        if kind in (
            REQUEST_SELECTION_TEXT,
            CLEAR_SELECTION,
            RESET,
            REQUEST_KEYBOARD,
            RELEASE_KEYBOARD,
            RESUME_VERIFIED,
        ):
            out.append(DecodedPacket(kind, b""))
            offset += 1
        elif kind in (SIZE_REPORT, KEYBOARD_TRACE):
            if len(view) - offset < 5:
                break
            out.append(DecodedPacket(kind, bytes(view[offset + 1 : offset + 5])))
            offset += 5
        elif kind in (
            INPUT_ENABLED,
            MODE_REPORT,
            SCROLL_VIEWPORT,
            VIEW_REPORT,
            KEYBOARD_BUTTON,
            KEYBOARD_STATE,
        ):
            if len(view) - offset < 2:
                break
            out.append(DecodedPacket(kind, bytes(view[offset + 1 : offset + 2])))
            offset += 2
        elif kind in (REQUEST_SNAPSHOT, RESUME_PROBE, CANCEL_SCROLL, SCROLL_CANCELLED):
            if len(view) - offset < 5:
                break
            out.append(DecodedPacket(kind, bytes(view[offset + 1 : offset + 5])))
            offset += 5
        elif kind == PAGE_METRICS:
            if len(view) - offset < 1 + PAGE_METRICS_LEN:
                break
            end = offset + 1 + PAGE_METRICS_LEN
            out.append(DecodedPacket(kind, bytes(view[offset + 1 : end])))
            offset = end
        elif kind == RESUME_REPORT:
            if len(view) - offset < 1 + RESUME_REPORT_LEN:
                break
            end = offset + 1 + RESUME_REPORT_LEN
            out.append(DecodedPacket(kind, bytes(view[offset + 1 : end])))
            offset = end
        elif kind == SNAPSHOT_CHUNK:
            if len(view) - offset < 5:
                break
            (length,) = struct.unpack_from(">I", view, offset + 1)
            if length > MAX_WRITE_CHUNK:
                raise ValueError(f"packet kind {kind:#x} length {length} too large")
            if len(view) - offset < 5 + length:
                break
            out.append(DecodedPacket(kind, bytes(view[offset + 5 : offset + 5 + length])))
            offset += 5 + length
        elif kind == ACK:
            if len(view) - offset < 9:
                break
            out.append(DecodedPacket(kind, bytes(view[offset + 1 : offset + 9])))
            offset += 9
        elif kind in (WRITE, SELECTION_TEXT):
            if len(view) - offset < 5:
                break
            (length,) = struct.unpack_from(">I", view, offset + 1)
            if length > MAX_WRITE_CHUNK:
                raise ValueError(f"packet kind {kind:#x} length {length} too large")
            if len(view) - offset < 5 + length:
                break
            out.append(DecodedPacket(kind, bytes(view[offset + 5 : offset + 5 + length])))
            offset += 5 + length
        elif kind == INPUT:
            if len(view) - offset < 6:
                break
            (length,) = struct.unpack_from(">I", view, offset + 2)
            if length > MAX_WRITE_CHUNK:
                raise ValueError(f"packet kind {kind:#x} length {length} too large")
            if len(view) - offset < 6 + length:
                break
            origin = bytes(view[offset + 1 : offset + 2])
            data = bytes(view[offset + 6 : offset + 6 + length])
            out.append(DecodedPacket(kind, origin + data))
            offset += 6 + length
        else:
            raise ValueError(f"unknown packet kind {kind:#x}")
    del buffer[:offset]
    return out


class IncrementalTextDecoder:
    """UTF-8 decoder that buffers split multibyte/escape sequences.

    Used on the preview/diagnostic path (bounded `pane.read` text for
    non-terminal surfaces). The display path decodes Dart-side with an
    equivalent incremental decoder; both must agree on never splitting a
    sequence across a flush boundary.
    """

    def __init__(self) -> None:
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._pending_esc = b""

    def feed(self, chunk: bytes) -> str:
        data = self._pending_esc + chunk
        # Hold back a trailing incomplete escape sequence (ESC [ params...).
        cut = len(data)
        esc = data.rfind(b"\x1b")
        if esc != -1:
            tail = data[esc:]
            if len(tail) < 2 or tail[1:2] == b"[" and not _csi_complete(tail):
                cut = esc
        elif data.endswith(b"\x1b"):
            cut = len(data) - 1
        self._pending_esc = data[cut:]
        return self._decoder.decode(data[:cut])

    def flush(self) -> str:
        text = self._decoder.decode(self._pending_esc, final=True)
        self._pending_esc = b""
        return text


def _csi_complete(tail: bytes) -> bool:
    for byte in tail[2:]:
        if 0x40 <= byte <= 0x7E:
            return True
    return False


def split_for_transport(data: bytes, limit: int = MAX_WRITE_CHUNK) -> list[bytes]:
    """Split bytes into bounded chunks without decoding (decoder-agnostic)."""
    return [data[i : i + limit] for i in range(0, len(data), limit)] or [b""]
