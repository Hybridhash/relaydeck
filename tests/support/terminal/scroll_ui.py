"""Scroll packets and view helpers."""

from __future__ import annotations

from typing import Any

from relaydeck_terminal import protocol

from relaydeck.client.features.shell.screen import mount_shell_screen
from relaydeck.client.features.terminal.input_modes import WORKFLOW_AGENT_DIRECT
from relaydeck.client.features.terminal.surfaces import mount_agent_screen
from tests.support.host_harness import render_component
from tests.support.terminal.channel_open import _HarnessChannel
from tests.support.terminal.select_sheet import _sheet
from tests.support.terminal.sessions import _agent_session, _session


async def _view(
    app: Any, channel: Any, *, alt: bool, bottom: bool = True, mouse: bool = False
) -> None:
    channel.handler(protocol.encode_view_report(protocol.ViewState(alt, bottom, mouse)))
    await app.settle()


def _scroll_packets(channel: Any) -> list[bytes]:
    return [raw for raw in channel.sent if raw[0] == protocol.SCROLL_VIEWPORT]


async def _app(shell: bool, tier: str) -> tuple[Any, Any, Any, Any]:
    if tier != "expanded":
        return await _sheet(shell, tier=tier)
    if shell:
        session, _ops, factory = _session()
        await session.open("term-1")
        app = await render_component(
            mount_shell_screen,
            session,
            on_back=lambda: None,
            on_new_terminal=lambda: None,
            width_tier="expanded",
            phone=False,
        )
    else:
        session, _ops, factory, _submitted = _agent_session()
        session.open()
        await session.confirm_takeover()
        session.on_draft_change("retained compose draft")
        session.set_workflow(WORKFLOW_AGENT_DIRECT)
        app = await render_component(
            mount_agent_screen, session, width_tier="expanded", phone=False
        )
    app.session.apply_patch(app.page._i, {"width": 1280, "height": 844})
    channel = _HarnessChannel()
    app.conn.data_channel_for = lambda number: channel
    await app.set_props_and_fire(
        "terminal-surface", {}, "data_channel_open", {"channel_name": "terminal", "channel_id": 12}
    )
    channel.handler(protocol.encode_size_report(80, 24))
    await app.settle()
    return app, session, factory.sockets[0], channel
