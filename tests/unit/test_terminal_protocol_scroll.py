"""SCROLL_VIEWPORT (0x0B) / VIEW_REPORT (0x86) protocol and bridge tests.

The local view-scroll path: Python scroll buttons send page jumps to Dart,
Dart reports (alt buffer, at bottom, mouse reporting) back on change only.
"""

import struct

import pytest
from relaydeck_terminal import protocol
from relaydeck_terminal.protocol import ViewState
from relaydeck_terminal.terminal import RelayDeckTerminal


def test_encode_scroll_viewport_pages():
    assert protocol.encode_scroll_viewport(1) == bytes([0x0B, 0x01])
    assert protocol.encode_scroll_viewport(10) == bytes([0x0B, 0x0A])
    assert protocol.encode_scroll_viewport(-1) == bytes([0x0B, 0xFF])
    assert protocol.encode_scroll_viewport(-10) == bytes([0x0B, 0xF6])


def test_encode_scroll_viewport_bottom():
    assert protocol.encode_scroll_viewport(None) == bytes([0x0B, 0x7F])


@pytest.mark.parametrize("bad", [0, 11, -11, 127, -128, True, "1", 1.0])
def test_encode_scroll_viewport_range_errors(bad):
    with pytest.raises(ValueError):
        protocol.encode_scroll_viewport(bad)


def test_scroll_viewport_framing_round_trip():
    buf = bytearray(protocol.encode_scroll_viewport(-3))
    packets = protocol.decode_packets(buf)
    assert len(buf) == 0
    assert packets == [protocol.DecodedPacket(0x0B, bytes([0xFD]))]
    (pages,) = struct.unpack(">b", packets[0].payload)
    assert pages == -3


def test_scroll_viewport_partial_tail_waits():
    buf = bytearray([0x0B])
    assert protocol.decode_packets(buf) == []
    assert buf == bytearray([0x0B])


def test_view_report_bitfield_decoding():
    assert protocol.decode_view_report(bytes([0b000])) == ViewState(False, False, False)
    assert protocol.decode_view_report(bytes([0b111])) == ViewState(True, True, True)
    assert protocol.decode_view_report(bytes([0b101])) == ViewState(True, False, True)
    assert protocol.decode_view_report(bytes([0b010])) == ViewState(False, True, False)


def test_view_report_short_payload_rejected():
    with pytest.raises(ValueError):
        protocol.decode_view_report(b"")


def test_view_report_encode_decode_round_trip():
    for bits in range(8):
        state = protocol.decode_view_report(bytes([bits]))
        assert protocol.encode_view_report(state) == bytes([0x86, bits])


def test_view_report_framing_round_trip():
    buf = bytearray(protocol.encode_view_report(ViewState(True, True, False)))
    packets = protocol.decode_packets(buf)
    assert len(buf) == 0
    assert packets == [protocol.DecodedPacket(0x86, bytes([0b011]))]


def test_terminal_scroll_viewport_sends_packet():
    term = RelayDeckTerminal()
    sent: list[bytes] = []
    term._test_mark_ready(sent)
    term.scroll_viewport(-2)
    term.scroll_to_bottom()
    assert sent == [bytes([0x0B, 0xFE]), bytes([0x0B, 0x7F])]


def test_terminal_scroll_viewport_range_error_sends_nothing():
    term = RelayDeckTerminal()
    sent: list[bytes] = []
    term._test_mark_ready(sent)
    with pytest.raises(ValueError):
        term.scroll_viewport(0)
    assert sent == []


def test_terminal_dispatches_view_state_to_listeners():
    term = RelayDeckTerminal()
    seen: list[ViewState] = []
    term.add_view_state_listener(seen.append)
    term._test_inject_dart_message(protocol.encode_view_report(ViewState(True, False, True)))
    assert seen == [ViewState(alt=True, at_bottom=False, mouse=True)]


def test_existing_packets_unchanged():
    # Spot-check the decoder table around the two new 1-byte-body kinds.
    buf = bytearray(bytes([0x03, 0x01, 0x85, 0x00, 0x82, 0x00, 0x50, 0x00, 0x18]))
    kinds = [p.kind for p in protocol.decode_packets(buf)]
    assert kinds == [0x03, 0x85, 0x82]
    assert len(buf) == 0
