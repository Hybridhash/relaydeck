"""Phone select-sheet helper."""

from __future__ import annotations

from typing import Any

from flet.messaging.protocol import MessageAction
from relaydeck_terminal import protocol

from relaydeck.client.features.terminal.surfaces import direct_typing
from tests.support.terminal.channel_open import _HarnessChannel
from tests.support.terminal.direct_typing import _direct


async def _sheet(
    shell: bool, *, tier: str = "compact", reading: bool = False
) -> tuple[Any, Any, Any, Any]:
    app, session, socket, _focuses = await _direct(shell, tier)
    send = app.conn.send_message

    def acknowledge_scroll(message: Any) -> None:
        send(message)
        if message.action == MessageAction.INVOKE_METHOD and message.body.name == "scroll_to":
            body = message.body
            app.session.handle_invoke_method_results(body.control_id, body.call_id, None, None)

    app.conn.send_message = acknowledge_scroll
    if reading:
        # Exercise the real keyboard action, not private component state.
        original = direct_typing._typing_now
        direct_typing._typing_now = lambda: original() + 1
        try:
            await app.set_props_and_fire(
                "shell-keyboard-toggle" if shell else "agent-workflow-toggle", {}, "click"
            )
        finally:
            direct_typing._typing_now = original
    channel = _HarnessChannel()
    app.conn.data_channel_for = lambda number: channel
    await app.set_props_and_fire(
        "terminal-surface",
        {},
        "data_channel_open",
        {"channel_name": "terminal", "channel_id": 12},
    )
    # The carrier reports layout after opening the channel. This drives the
    # parent/session rerender with the new viewer generation as in production.
    channel.handler(protocol.encode_size_report(80, 24))
    await app.settle()
    return app, session, socket, channel
