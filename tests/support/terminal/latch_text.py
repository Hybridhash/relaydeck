"""Text-input inspection helper."""

from __future__ import annotations

from typing import Any

from relaydeck.gateway import terminal_wire


def _inputs(socket: Any) -> list[bytes]:
    return [
        frame.data
        for raw in socket.sent_bytes
        if (frame := terminal_wire.decode_input(raw)).kind == "text"
    ]
