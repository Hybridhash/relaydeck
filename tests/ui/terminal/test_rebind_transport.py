"""A terminal follows the transport that exists now.

After a Flet transport reconnect (same page, same native engine) the terminal's
channel binding must move to the new transport, so later output reaches the live
channel. Synthetic mux connections only.
"""

from __future__ import annotations

from typing import Any

from relaydeck_terminal import protocol

from tests.support.host_harness import run
from tests.support.terminal.resume import _MuxConn, _open_term


def test_rebind_transport_moves_output_to_the_new_channel_and_fences_inbound() -> None:
    async def scenario() -> None:
        app, term, _conn, old = await _open_term(7)
        term.write(b"before")
        assert old.closed is False

        new_conn = _MuxConn()
        app.session.attach_connection(new_conn)
        new = new_conn.channels.setdefault(7, _new_channel(new_conn, 7))
        sent: list[bytes] = []
        new.send = sent.append  # type: ignore[method-assign]

        assert term.resume_unverified is False
        assert term.rebind_transport() is True
        assert term._channel is new  # the binding moved
        assert old.closed is True  # the dead binding is closed
        assert old.handler is None  # and its callbacks are ignored
        assert term.resume_unverified is True  # inbound fenced until verified or reset

        term.write(b"after")
        assert protocol.encode_write(b"after") in sent
        assert all(b"before" not in packet for packet in sent)

        assert term.rebind_transport() is True  # idempotent for the same channel
        assert term._channel is new

        term.reset()
        assert term.resume_unverified is False  # reset retires the fence

    run(scenario())


def test_rebind_transport_without_recorded_session_is_a_safe_noop_before_capture() -> None:
    from relaydeck_terminal.terminal import RelayDeckTerminal

    term = RelayDeckTerminal(key="unbound-term")
    assert term._session_ref is None and term._channel_id is None
    assert term.rebind_transport() is False
    assert term.resume_unverified is False


def _new_channel(conn: Any, channel_id: int) -> Any:
    return conn.data_channel_for(channel_id)


def test_rebind_transport_before_the_control_is_back_on_the_page_uses_recorded_session() -> None:
    """Owner repro: on reconnect the terminal control is not yet re-added to the page;
    rebinding follows its recorded session's live connection without needing the parent."""

    async def scenario() -> None:
        app, term, _conn, old = await _open_term(7)
        live_connection = _MuxConn()
        app.session.attach_connection(live_connection)
        new = live_connection.data_channel_for(7)

        def not_on_page(_channel_id: Any) -> Any:
            raise RuntimeError("Control must be added to the page first")

        term.get_data_channel = not_on_page  # type: ignore[method-assign]
        assert term._session_ref is not None and term._session_ref() is app.session
        generation = term.viewer_generation
        assert term.rebind_transport() is True
        assert term._channel is new
        assert old.closed and old.handler is None
        assert term.viewer_generation == generation and term.resume_unverified
        term.write(b"live connection output")
        assert live_connection.data_frames == [
            (7, protocol.encode_write(b"live connection output"))
        ]

    run(scenario())
