"""Wire assertions shared by retained terminal resize tests."""

from __future__ import annotations

import struct
from typing import Any

from relaydeck.contracts.wire import terminal as terminal_wire


def wire_sizes(socket: Any) -> list[tuple[int, int]]:
    frames = [terminal_wire.decode_input(data) for kind, data in socket.sent if kind == "bytes"]
    assert all(frame.kind == "resize" for frame in frames), "Resize must never send PTY input."
    return [struct.unpack(">HH", frame.data) for frame in frames]
