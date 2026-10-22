"""REQUEST_KEYBOARD (0x0A) and RELEASE_KEYBOARD (0x0E) packets.

Both are body-less and never write to the PTY; the viewer decides what they do.
"""

from __future__ import annotations

import logging
import struct

import pytest
from relaydeck_terminal import protocol
from relaydeck_terminal.terminal import RelayDeckTerminal


def test_release_keyboard_is_a_bodyless_0x0e_packet() -> None:
    assert protocol.RELEASE_KEYBOARD == 0x0E
    assert protocol.encode_release_keyboard() == b"\x0e"


def test_decoder_yields_release_keyboard_without_a_body() -> None:
    buffer = bytearray(b"\x0a\x0e\x0d")
    kinds = [packet.kind for packet in protocol.decode_packets(buffer)]
    assert kinds == [protocol.REQUEST_KEYBOARD, protocol.RELEASE_KEYBOARD, protocol.RESUME_VERIFIED]
    assert buffer == bytearray()


def test_terminal_sends_request_and_release_packets_and_never_pty_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    term = RelayDeckTerminal(key="keyboard-packets")
    sent: list[bytes] = []
    monkeypatch.setattr(term, "_send", sent.append)
    term.request_keyboard()
    term.release_keyboard()
    assert sent == [b"\x0a", b"\x0e"]


def test_keyboard_button_and_state_packets_encode_one_byte_bodies() -> None:
    assert protocol.KEYBOARD_BUTTON == 0x0F and protocol.KEYBOARD_STATE == 0x88
    assert protocol.encode_keyboard_button(True) == b"\x0f\x01"
    assert protocol.encode_keyboard_button(False) == b"\x0f\x00"
    assert protocol.encode_keyboard_state(True) == b"\x88\x01"
    assert protocol.encode_keyboard_state(False) == b"\x88\x00"


def test_decoder_reads_keyboard_state_with_its_one_byte_body() -> None:
    buffer = bytearray(b"\x88\x01\x88\x00")
    packets = protocol.decode_packets(buffer)
    assert [(p.kind, p.payload) for p in packets] == [
        (protocol.KEYBOARD_STATE, b"\x01"),
        (protocol.KEYBOARD_STATE, b"\x00"),
    ]
    assert buffer == bytearray()


def test_terminal_reports_keyboard_state_to_listeners_and_can_unsubscribe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    term = RelayDeckTerminal(key="keyboard-state")
    seen: list[bool] = []
    unsubscribe = term.add_keyboard_state_listener(seen.append)
    sent: list[bytes] = []
    monkeypatch.setattr(term, "_send", sent.append)
    term.set_keyboard_button(True)
    assert sent == [b"\x0f\x01"]
    term._dispatch(protocol.DecodedPacket(protocol.KEYBOARD_STATE, b"\x01"))
    term._dispatch(protocol.DecodedPacket(protocol.KEYBOARD_STATE, b"\x00"))
    assert seen == [True, False]
    unsubscribe()
    term._dispatch(protocol.DecodedPacket(protocol.KEYBOARD_STATE, b"\x01"))
    assert seen == [True, False]


@pytest.mark.parametrize("event", range(1, 10))
def test_keyboard_trace_round_trip(event: int) -> None:
    frame = struct.pack(">BBBH", 0x89, event, 0x1F, 844)
    buffer = bytearray(frame)
    packets = protocol.decode_packets(buffer)
    assert len(packets) == 1 and packets[0].kind == protocol.KEYBOARD_TRACE
    assert protocol.decode_keyboard_trace(packets[0].payload) == (event, 0x1F, 844)
    assert not buffer


def test_keyboard_trace_logging_is_fixed_metadata_only(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The app's diagnostics logger stops propagation once installed; capture regardless of order.
    monkeypatch.setattr(logging.getLogger("relaydeck.client.typing_split"), "propagate", True)
    term = RelayDeckTerminal(key="trace-should-not-be-logged")
    sent: list[tuple[int, bytes]] = []
    term.add_input_listener(lambda origin, data: sent.append((origin, data)))
    with caplog.at_level(logging.INFO, logger="relaydeck.client.typing_split"):
        term._on_dart_message(struct.pack(">BBBH", 0x89, 2, 0x19, 65535))
    assert [record.getMessage() for record in caplog.records] == [
        "keyboard_trace event=button_tap armed=True focus=False conn=False "
        "input=True down_shown=True vv=65535"
    ]
    assert not sent
    assert "trace-should-not-be-logged" not in caplog.text


@pytest.mark.parametrize("cut", range(1, 5))
def test_truncated_trace_waits_for_complete_frame(cut: int) -> None:
    frame = struct.pack(">BBBH", 0x89, 1, 8, 1000)
    buffer = bytearray(frame[:cut])
    assert protocol.decode_packets(buffer) == []
    assert buffer == frame[:cut]
    buffer.extend(frame[cut:])
    packets = protocol.decode_packets(buffer)
    assert protocol.decode_keyboard_trace(packets[0].payload) == (1, 8, 1000)
    assert not buffer


@pytest.mark.parametrize("payload", [b"", b"\x01", b"\x01\x08\xff", b"\x00\x00\xff\xff"])
def test_malformed_trace_is_rejected_and_not_logged(
    payload: bytes, caplog: pytest.LogCaptureFixture
) -> None:
    with pytest.raises(ValueError):
        protocol.decode_keyboard_trace(payload)
    term = RelayDeckTerminal(key="bad-trace")
    with caplog.at_level(logging.INFO, logger="relaydeck.client.typing_split"):
        term._dispatch(protocol.DecodedPacket(protocol.KEYBOARD_TRACE, payload))
    assert not caplog.records


@pytest.mark.parametrize("cut", range(1, 10))
def test_page_metrics_wait_for_the_whole_frame_and_log_geometry(
    cut: int, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The diagnostics logger stops propagating once configured; capture needs it back.
    monkeypatch.setattr(logging.getLogger("relaydeck.client.typing_split"), "propagate", True)
    frame = struct.pack(">BBhhHH", protocol.PAGE_METRICS, 1, -44, 12, 526, 873)
    buffer = bytearray(frame[:cut])
    assert protocol.decode_packets(buffer) == []
    buffer.extend(frame[cut:])
    (packet,) = protocol.decode_packets(buffer)
    assert protocol.decode_page_metrics(packet.payload) == (1, -44, 12, 526, 873)
    term = RelayDeckTerminal(key="page-metrics")
    with caplog.at_level(logging.INFO, logger="relaydeck.client.typing_split"):
        term._dispatch(packet)
    assert "page_metrics reset=True scroll=-44 vv_offset=12 vv=526 window=873" in caplog.text


@pytest.mark.parametrize("payload", [b"", b"\x01" * 8, b"\x02" + b"\x00" * 8])
def test_malformed_page_metrics_are_rejected_and_not_logged(
    payload: bytes, caplog: pytest.LogCaptureFixture
) -> None:
    with pytest.raises(ValueError):
        protocol.decode_page_metrics(payload)
    term = RelayDeckTerminal(key="bad-page")
    with caplog.at_level(logging.INFO, logger="relaydeck.client.typing_split"):
        term._dispatch(protocol.DecodedPacket(protocol.PAGE_METRICS, payload))
    assert not caplog.records
