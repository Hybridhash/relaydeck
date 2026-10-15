"""Touch-tablet overlay structure and real scroll dispatch; native hits need device proof."""

import flet as ft
import pytest
from relaydeck_terminal import protocol

from relaydeck.client.features.terminal.input_modes import WORKFLOW_AGENT_DIRECT
from tests.support.host_harness import render_component, run
from tests.support.terminal.channel_open import _HarnessChannel
from tests.support.terminal.latch_text import _inputs
from tests.support.terminal.scroll_ui import _scroll_packets, _view
from tests.support.terminal.sessions import _agent_session
from tests.support.terminal.touch_keyboard import _Screen


def test_touch_tablet_scroll_overlay_uses_column_and_keeps_scroll_actions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        session, _ops, factory, _submitted = _agent_session()
        session.open()
        await session.confirm_takeover()
        session.set_workflow(WORKFLOW_AGENT_DIRECT)
        app = await render_component(_Screen, session, True)
        try:
            overlay = app.find("terminal-scroll-overlay")
            assert isinstance(overlay, ft.Column) and not isinstance(overlay, ft.Container)
            assert overlay.visible
            assert overlay.right == 8 and overlay.top == 0 and overlay.bottom == 0
            assert overlay.width == 32 and overlay.tight is False and overlay.spacing == 0
            assert overlay.alignment == ft.MainAxisAlignment.CENTER
            assert overlay.horizontal_alignment == ft.CrossAxisAlignment.CENTER
            assert overlay.scroll is None
            buttons = app.find("terminal-scroll-buttons")
            assert overlay.controls == [buttons]
            assert isinstance(buttons, ft.Column) and buttons.tight and buttons.spacing == 6

            channel = _HarnessChannel()
            monkeypatch.setattr(app.conn, "data_channel_for", lambda number: channel)
            await app.set_props_and_fire(
                "terminal-surface",
                {},
                "data_channel_open",
                {"channel_name": "terminal", "channel_id": 12},
            )
            channel.handler(protocol.encode_size_report(80, 24))
            await _view(app, channel, alt=False, bottom=False)
            opened = len(_scroll_packets(channel))  # the open-at-end jump on the first report
            for key in ("terminal-scroll-up", "terminal-scroll-down", "terminal-scroll-latest"):
                button = app.find(key)
                assert button.visible and not button.disabled
                await app.set_props_and_fire(key, {}, "click")
            assert _scroll_packets(channel)[opened:] == [b"\x0b\xff", b"\x0b\x01", b"\x0b\x7f"]
            assert _inputs(factory.sockets[0]) == []
        finally:
            await session.dispose()

    run(scenario())
