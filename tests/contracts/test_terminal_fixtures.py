"""VT fixtures, extension protocol,
incremental decoding, viewer bounds, and Dart-source safety tripwires.

Screen-cell assertions run Dart-side
(extensions/terminal/src/flutter/relaydeck_terminal/test/) once the
Flutter SDK is provisioned; these tests pin everything verifiable here.
"""

from __future__ import annotations

import asyncio
import json
import struct
from pathlib import Path

import pytest
from relaydeck_terminal import protocol
from relaydeck_terminal.protocol import IncrementalTextDecoder
from relaydeck_terminal.terminal import RelayDeckTerminal, TerminalOverflowError

VT = Path(__file__).resolve().parent.parent / "fixtures" / "vt"
KEYS = Path(__file__).resolve().parent.parent / "fixtures" / "keys"
DART = (
    Path(__file__).resolve().parent.parent.parent
    / "extensions"
    / "terminal"
    / "src"
    / "flutter"
    / "relaydeck_terminal"
    / "lib"
    / "src"
    / "relaydeck_terminal.dart"
)


def test_fixture_pairs_present_and_valid() -> None:
    stems = [
        "01_ascii_colors",
        "02_cursor_addressing",
        "03_erased_line",
        "04_alt_screen",
        "05_wide_emoji",
        "06_combining",
        "07_bracketed_mode",
        "08_scrollback",
        "09_split_sequences",
    ]
    for stem in stems:
        spec = json.loads((VT / f"{stem}.json").read_text())
        assert isinstance(spec["description"], str)
    for name in [
        "01_ascii_colors.bin",
        "02_cursor_addressing.bin",
        "03_erased_line.bin",
        "04a_alt_enter_prefix.bin",
        "04b_alt_content.bin",
        "04c_alt_exit.bin",
        "05_wide_emoji.bin",
        "06_combining.bin",
        "07a_bracketed_on.bin",
        "07b_bracketed_off.bin",
        "08_scrollback.bin",
        "09_split_sequences.bin",
    ]:
        assert (VT / name).stat().st_size > 0, name


def test_keys_golden_valid() -> None:
    spec = json.loads((KEYS / "encodings.json").read_text())
    names = [e["name"] for e in spec["encodings"]]
    assert len(names) == len(set(names)) == 31
    for entry in spec["encodings"]:
        raw = bytes.fromhex(entry["bytes_hex"])
        assert 1 <= len(raw) <= 8, entry["name"]
        assert entry["source"] in ("keytab", "ctrl-handler", "shift-enter-override")


def test_packet_round_trip() -> None:
    cases = [
        (protocol.encode_write(b"hello \x1b[31m"), protocol.WRITE),
        (protocol.encode_input_enabled(True), protocol.INPUT_ENABLED),
        (protocol.encode_request_selection_text(), protocol.REQUEST_SELECTION_TEXT),
        (protocol.encode_clear_selection(), protocol.CLEAR_SELECTION),
        (protocol.encode_reset(), protocol.RESET),
        (protocol.encode_input(b"typed", protocol.ORIGIN_KEY), protocol.INPUT),
        (protocol.encode_size_report(100, 30), protocol.SIZE_REPORT),
        (protocol.encode_selection_text("héllo"), protocol.SELECTION_TEXT),
        (protocol.encode_ack(123456789), protocol.ACK),
    ]
    blob = b"".join(p for p, _ in cases)
    buf = bytearray(blob)
    out = protocol.decode_packets(buf)
    assert len(buf) == 0
    assert [p.kind for p in out] == [k for _, k in cases]
    assert out[0].payload == b"hello \x1b[31m"
    assert struct.unpack(">HH", out[6].payload) == (100, 30)
    assert struct.unpack(">Q", out[8].payload) == (123456789,)


def test_partial_tail_held_and_completed() -> None:
    full = protocol.encode_write(b"abcdef")
    buf = bytearray(full[:4])
    assert protocol.decode_packets(buf) == []
    assert bytes(buf) == full[:4]
    buf += full[4:]
    out = protocol.decode_packets(buf)
    assert [p.kind for p in out] == [protocol.WRITE]
    assert out[0].payload == b"abcdef"


def test_packet_limits_reject() -> None:
    with pytest.raises(ValueError):
        protocol.encode_write(b"x" * (protocol.MAX_WRITE_CHUNK + 1))
    with pytest.raises(ValueError):
        protocol.encode_input(b"x", origin=9)
    with pytest.raises(ValueError):
        protocol.decode_packets(bytearray(b"\x99"))
    evil = struct.pack(">BI", protocol.WRITE, protocol.MAX_WRITE_CHUNK + 1)
    with pytest.raises(ValueError):
        protocol.decode_packets(bytearray(evil))


def test_input_payload_split() -> None:
    origin, data = protocol.decode_input(
        protocol.decode_packets(
            bytearray(protocol.encode_input("hé".encode(), protocol.ORIGIN_MOUSE))
        )[0].payload
    )
    assert origin == protocol.ORIGIN_MOUSE
    assert data == "hé".encode()


def test_decoder_identical_at_every_cut() -> None:
    raw = (VT / "09_split_sequences.bin").read_bytes()
    want = json.loads((VT / "09_split_sequences.json").read_text())["decoded_text"]
    for cut in range(len(raw) + 1):
        decoder = IncrementalTextDecoder()
        text = decoder.feed(raw[:cut]) + decoder.feed(raw[cut:]) + decoder.flush()
        assert text == want, f"cut at {cut}"


def test_decoder_holds_split_escape_and_recovers() -> None:
    decoder = IncrementalTextDecoder()
    assert decoder.feed(b"ab\x1b[3") == "ab"
    assert decoder.feed(b"1mX") == "\x1b[31mX"
    assert decoder.flush() == ""


def test_decoder_never_raises_on_malformed() -> None:
    decoder = IncrementalTextDecoder()
    text = decoder.feed(b"\xff\xfe bad \x80") + decoder.flush()
    assert "bad" in text
    assert "\ufffd" in text


def test_split_for_transport_bounds() -> None:
    data = b"z" * (protocol.MAX_WRITE_CHUNK * 2 + 7)
    chunks = protocol.split_for_transport(data)
    assert b"".join(chunks) == data
    assert all(len(c) <= protocol.MAX_WRITE_CHUNK for c in chunks)
    assert len(chunks) == 3


def _ready_control() -> tuple[RelayDeckTerminal, list[bytes]]:
    control = RelayDeckTerminal()
    sent: list[bytes] = []
    control._test_mark_ready(sent)
    return control, sent


def test_control_write_chunks_and_acks() -> None:
    control, sent = _ready_control()
    control.write(b"a" * 100)
    assert len(sent) == 1
    (kind,) = struct.unpack(">B", sent[0][:1])
    assert kind == protocol.WRITE
    control._test_inject_dart_message(protocol.encode_ack(100))
    assert not control.overflowed


def test_control_write_overflow_is_explicit() -> None:
    control, _ = _ready_control()
    big = b"q" * protocol.VIEWER_QUEUE_LIMIT
    control.write(big)
    assert not control.overflowed
    with pytest.raises(TerminalOverflowError):
        control.write(b"one more byte")
    assert control.overflowed
    with pytest.raises(TerminalOverflowError):
        control.write(b"stays failed")
    control.reset()
    assert not control.overflowed


def test_control_input_switch_sends_packet() -> None:
    control, sent = _ready_control()
    assert control.input_enabled is False
    control.set_input_enabled(True)
    assert control.input_enabled is True
    (packet,) = protocol.decode_packets(bytearray(b"".join(sent)))
    assert packet.kind == protocol.INPUT_ENABLED
    assert packet.payload == b"\x01"


def test_control_queues_before_ready_and_flushes() -> None:
    control = RelayDeckTerminal()
    control.write(b"early")
    sent: list[bytes] = []
    control._test_mark_ready(sent)
    assert len(sent) == 1
    assert protocol.decode_packets(bytearray(b"".join(sent)))[0].payload == b"early"


def test_control_input_dispatch_with_origin() -> None:
    control, _ = _ready_control()
    got: list[tuple[int, bytes]] = []
    control.add_input_listener(lambda origin, data: got.append((origin, data)))
    control._test_inject_dart_message(
        protocol.encode_input(b"x", protocol.ORIGIN_KEY)
        + protocol.encode_input("é".encode(), protocol.ORIGIN_TEXT)
    )
    assert got == [(protocol.ORIGIN_KEY, b"x"), (protocol.ORIGIN_TEXT, "é".encode())]


def test_control_size_reports_dispatch() -> None:
    control, _ = _ready_control()
    got: list[tuple[int, int]] = []
    control.add_size_listener(lambda c, r: got.append((c, r)))
    control._test_inject_dart_message(protocol.encode_size_report(132, 43))
    assert got == [(132, 43)]


def test_control_selection_round_trip() -> None:
    async def scenario() -> None:
        control, sent = _ready_control()
        task = asyncio.ensure_future(control.get_selection_text(timeout=5))
        await asyncio.sleep(0)
        assert protocol.decode_packets(bytearray(b"".join(sent)))[0].kind == (
            protocol.REQUEST_SELECTION_TEXT
        )
        control._test_inject_dart_message(protocol.encode_selection_text("sel"))
        assert await task == "sel"

    asyncio.run(scenario())


def test_control_malformed_dart_bytes_protocol_error() -> None:
    control, _ = _ready_control()
    control._test_inject_dart_message(b"\x99")
    assert control.protocol_error is not None
    assert control.overflowed is False
    control.reset()
    assert control.protocol_error is None


def test_dart_denies_untrusted_output_powers() -> None:
    """Static tripwires: the widget must never enable OSC52 writes,
    hyperlink auto-open, or local paste (all input crosses Python policy)."""
    import re

    src = DART.read_text()
    assert re.search(r"onClipboardStore\s*[:=,)]", src) is None
    assert re.search(r"onOpenUrl\s*[:=,)]", src) is None
    assert re.search(r"onHyperlinkTap\s*:", src) is None
    # No local paste at all: browser paste text goes to Python's reviewed paste.
    for forbidden in (r"\.paste\s*\(", r"Clipboard\.getData", r"\bpasteText\b"):
        assert re.search(forbidden, src) is None, forbidden
    assert "readOnly: !_inputEnabled" in src
    assert "onKeyEvent: _onKeyEvent" in src
