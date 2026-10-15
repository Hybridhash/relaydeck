"""Mouse desktops offer only "Jump to latest", and only when the view is not at the end."""

from __future__ import annotations

import pytest
from relaydeck_terminal import protocol

from relaydeck.client.features.terminal.input_modes import WORKFLOW_AGENT_DIRECT
from tests.support.host_harness import render_component, run
from tests.support.terminal.channel_open import _HarnessChannel
from tests.support.terminal.latch_text import _inputs
from tests.support.terminal.scroll_ui import _scroll_packets, _view
from tests.support.terminal.sessions import _agent_session
from tests.support.terminal.touch_keyboard import _Screen

JUMP_PACKET = b"\x0b\x7f"


async def _open(monkeypatch: pytest.MonkeyPatch, phone: bool = False):
    session, _ops, factory, _submitted = _agent_session()
    session.open()
    await session.confirm_takeover()
    session.set_workflow(WORKFLOW_AGENT_DIRECT)
    app = await render_component(_Screen, session, False, phone)
    channel = _HarnessChannel()
    monkeypatch.setattr(app.conn, "data_channel_for", lambda number: channel)
    await app.set_props_and_fire(
        "terminal-surface",
        {},
        "data_channel_open",
        {"channel_name": "terminal", "channel_id": 12},
    )
    channel.handler(protocol.encode_size_report(80, 24))
    return session, factory, app, channel


def test_desktop_shows_only_jump_when_scrolled_back_and_it_reaches_the_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        session, factory, app, channel = await _open(monkeypatch)
        try:
            await _view(app, channel, alt=False, bottom=True)
            assert not app.find("terminal-scroll-overlay").visible
            await _view(app, channel, alt=False, bottom=False)
            assert app.find("terminal-scroll-overlay").visible
            assert app.find("terminal-scroll-latest").visible
            assert not app.find("terminal-scroll-up").visible
            assert not app.find("terminal-scroll-down").visible
            before = len(_scroll_packets(channel))  # the open-at-end jump on the first report
            await app.set_props_and_fire("terminal-scroll-latest", {}, "click")
            assert _scroll_packets(channel)[before:] == [JUMP_PACKET]
            assert _inputs(factory.sockets[0]) == []
            await _view(app, channel, alt=False, bottom=True)
            assert not app.find("terminal-scroll-overlay").visible
        finally:
            await session.dispose()

    run(scenario())


def test_desktop_offers_jump_for_a_full_screen_app_while_live(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        session, _factory, app, channel = await _open(monkeypatch)
        try:
            await _view(app, channel, alt=True, bottom=True)
            assert app.find("terminal-scroll-overlay").visible
            assert app.find("terminal-scroll-latest").visible
            assert not app.find("terminal-scroll-up").visible
        finally:
            await session.dispose()

    run(scenario())


def test_the_phone_keeps_all_three_buttons_regardless_of_position(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        session, _factory, app, channel = await _open(monkeypatch, phone=True)
        try:
            await _view(app, channel, alt=False, bottom=True)
            assert app.find("terminal-scroll-overlay").visible
            assert app.find("terminal-scroll-up").visible
            assert app.find("terminal-scroll-down").visible
        finally:
            await session.dispose()

    run(scenario())
