"""Mux connection and resume helpers."""

from __future__ import annotations

import struct
from typing import Any

import flet as ft
from relaydeck_terminal import protocol
from relaydeck_terminal.terminal import RelayDeckTerminal

from tests.support.host_harness import FakeConnection, render_component

INST = 0x123456789ABCDEF


OTHER_INST = 0xFEDCBA987654321


class _MuxChannel:
    """Tracking muxed channel mirroring FletApp semantics: closed send
    drops, close is idempotent, deliver() drives the attached handler."""

    def __init__(self, conn: _MuxConn, channel_id: int) -> None:
        self._conn = conn
        self._id = channel_id
        self.handler: Any = None
        self.closed = False

    def on_bytes(self, handler: Any) -> None:
        self.handler = handler

    def send(self, payload: bytes) -> None:
        if self.closed:
            return
        self._conn.send_data_channel_frame(self._id, payload)

    def close(self) -> None:
        self.closed = True

    def deliver(self, payload: bytes) -> None:
        if self.closed or self.handler is None:
            return
        self.handler(payload)


class _MuxConn(FakeConnection):
    """FakeConnection with a per-connection mux channel cache, like
    FletApp.data_channel_for (idempotent per id, empty on a fresh
    connection)."""

    def __init__(self) -> None:
        super().__init__()
        self.channels: dict[int, _MuxChannel] = {}

    def data_channel_for(self, channel_id: int) -> _MuxChannel:  # type: ignore[override]
        return self.channels.setdefault(channel_id, _MuxChannel(self, channel_id))


@ft.component
def _Host() -> Any:
    return RelayDeckTerminal(key="r1b-term")


async def _open_term(channel_id: int = 7) -> tuple[Any, RelayDeckTerminal, _MuxConn, _MuxChannel]:
    app = await render_component(_Host)
    conn = _MuxConn()
    app.session.attach_connection(conn)
    term = app.find("r1b-term")
    await app.set_props_and_fire(
        "r1b-term",
        {},
        "data_channel_open",
        {"channel_name": "terminal", "channel_id": channel_id},
    )
    channel = conn.channels[channel_id]
    assert term.channel_ready is True
    return app, term, conn, channel


def _hello(instance: int = INST, resets: int = 0, applied: int = 0) -> bytes:
    return protocol.encode_resume_report(0, instance, resets, applied)


def _report(req: int, instance: int = INST, resets: int = 0, applied: int = 0) -> bytes:
    return protocol.encode_resume_report(req, instance, resets, applied)


def _write_payloads(frames: list[tuple[int, bytes]]) -> list[bytes]:
    """Raw WRITE payload bytes from captured mux frames (in order)."""
    out = []
    for _, payload in frames:
        assert payload[0] == protocol.WRITE
        (length,) = struct.unpack_from(">I", payload, 1)
        out.append(payload[5 : 5 + length])
    return out
