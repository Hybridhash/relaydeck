"""Live channel-open regression.

The committed `RelayDeckTerminal` declared `on_data_channel_open`
under `from __future__ import annotations`, so Flet 1.0.3's
`get_event_field_type` (which only unwraps `ForwardRef`, never a PEP
563 string) crashed live rendering at startup with `[resolve error]
on_data_channel_open: tuple index out of range` — the harness never
fired a real channel open, so nothing caught it. This test fires a
real `data_channel_open` through the host harness and proves the
channel is captured and functional.
"""

from __future__ import annotations

from typing import Any

import flet as ft
from flet.controls.control_event import get_event_field_type
from flet.data_channel import DataChannelOpenEvent
from relaydeck_terminal import protocol
from relaydeck_terminal.terminal import RelayDeckTerminal

from tests.support.host_harness import render_component, run
from tests.support.terminal.channel_open import _HarnessChannel


@ft.component
def _Host() -> Any:
    return RelayDeckTerminal(key="rd1t-term")


def test_event_field_type_resolves_to_open_event() -> None:
    term = RelayDeckTerminal()
    assert get_event_field_type(term, "on_data_channel_open") is DataChannelOpenEvent


def test_real_channel_open_captures_functional_channel() -> None:
    async def scenario() -> None:
        app = await render_component(_Host)
        term = app.find("rd1t-term")
        assert term.channel_ready is False
        channel = _HarnessChannel()
        app.conn.data_channel_for = lambda channel_id: channel  # type: ignore[method-assign]
        seen: list[tuple[int, bytes]] = []
        term.add_input_listener(lambda origin, data: seen.append((origin, data)))

        await app.set_props_and_fire(
            "rd1t-term",
            {},
            "data_channel_open",
            {"channel_name": "terminal", "channel_id": 7},
        )

        assert term.channel_ready is True
        assert channel.handler is not None
        # Dart -> Python path live: an INPUT packet reaches the listener.
        channel.handler(protocol.encode_input(b"a", origin=protocol.ORIGIN_KEY))
        await app.settle()
        assert seen == [(protocol.ORIGIN_KEY, b"a")]
        # Python -> Dart path live: output flushes to the captured channel.
        term.write(b"hi")
        assert channel.sent == [protocol.encode_write(b"hi")]

    run(scenario())
