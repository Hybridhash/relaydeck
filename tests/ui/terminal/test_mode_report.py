"""Engine truth: MODE_REPORT protocol + dispatch + mounts.

Paste framing follows the REAL xterm2 `bracketedPasteMode`, reported over
the extension bridge after every write/reset — Python never re-parses
output bytes for 2004 (a regex re-parse provably diverges from the engine
across RIS and split private-mode params). Pinned here at three levels:
protocol framing, viewer dispatch, and the mounted agent/shell screens
(the mounts route reports to `session.set_bracketed_paste`).
"""

from __future__ import annotations

from relaydeck_terminal import protocol
from relaydeck_terminal.terminal import RelayDeckTerminal

from relaydeck.client.features.shell.screen import mount_shell_screen
from relaydeck.client.features.terminal.agent_session import AGENT_LIVE
from relaydeck.client.features.terminal.screen import SURFACE_KEY
from relaydeck.client.features.terminal.surfaces import mount_agent_screen
from tests.support.host_harness import render_component, run
from tests.support.terminal.sessions import _agent_session, _session


def test_mode_report_protocol_roundtrip() -> None:
    assert protocol.MODE_REPORT == 0x85
    assert protocol.encode_mode_report(True) == bytes((0x85, 0x01))
    assert protocol.encode_mode_report(False) == bytes((0x85, 0x00))
    buf = bytearray(protocol.encode_mode_report(True))
    packets = protocol.decode_packets(buf)
    assert len(packets) == 1
    assert packets[0].kind == protocol.MODE_REPORT
    assert packets[0].payload == b"\x01"
    assert buf == bytearray()
    # A lone kind byte is a partial tail: held, not dropped or decoded.
    tail = bytearray((0x85,))
    assert protocol.decode_packets(tail) == []
    assert tail == bytearray((0x85,))


def test_mode_report_viewer_dispatch() -> None:
    term = RelayDeckTerminal()
    seen: list[bool] = []
    term.add_mode_listener(seen.append)
    term._test_inject_dart_message(protocol.encode_mode_report(True))
    term._test_inject_dart_message(protocol.encode_mode_report(False))
    assert seen == [True, False]
    # Coexists with neighboring packets (no misframing): an ACK and a
    # report in one delivery both dispatch.
    term._test_inject_dart_message(protocol.encode_ack(7) + protocol.encode_mode_report(True))
    assert seen == [True, False, True]
    assert term._acked_bytes == 7
    # A body byte that is neither 0x00 nor 0x01 fails closed (unframed).
    term._test_inject_dart_message(bytes((0x85, 0x02)))
    assert seen == [True, False, True, False]


def test_mode_report_mount_agent() -> None:
    async def scenario() -> None:
        session, _, _, _ = _agent_session()
        session.open()
        assert await session.confirm_takeover() == AGENT_LIVE
        assert session.bracketed_paste is False
        app = await render_component(mount_agent_screen, session)
        term = app.find(SURFACE_KEY)
        assert isinstance(term, RelayDeckTerminal)
        term._test_inject_dart_message(protocol.encode_mode_report(True))
        assert session.bracketed_paste is True
        term._test_inject_dart_message(protocol.encode_mode_report(False))
        assert session.bracketed_paste is False

    run(scenario())


def test_mode_report_mount_shell() -> None:
    async def scenario() -> None:
        session, _, _ = _session()
        assert await session.open("term-1") == "live"
        assert session.bracketed_paste is False
        app = await render_component(mount_shell_screen, session, lambda: None, lambda: None)
        term = app.find(SURFACE_KEY)
        assert isinstance(term, RelayDeckTerminal)
        term._test_inject_dart_message(protocol.encode_mode_report(True))
        assert session.bracketed_paste is True
        term._test_inject_dart_message(protocol.encode_mode_report(False))
        assert session.bracketed_paste is False

    run(scenario())


def test_output_bytes_never_move_framing_flag() -> None:
    # Neither direction of 2004 traffic moves the flag: set sequences,
    # reset sequences, RIS, and split deliveries are all inert here —
    # only engine MODE_REPORTs (via set_bracketed_paste) move it.
    agent, _, _, _ = _agent_session()
    shell, _, _ = _session()
    for session in (agent, shell):
        assert session.bracketed_paste is False
        session._handle_bytes(b"\x1b[?2004hready $ ")
        session._handle_bytes(b"\x1b[?1;2004h")
        session._handle_bytes(b"abc\x1b[?20")
        session._handle_bytes(b"04hdef")
        assert session.bracketed_paste is False
        session.set_bracketed_paste(True)
        session._handle_bytes(b"\x1b[?2004l$ ")
        session._handle_bytes(b"\x1bc")
        assert session.bracketed_paste is True
