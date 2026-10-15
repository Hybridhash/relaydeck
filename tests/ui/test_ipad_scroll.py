"""Scroll controls on touch widths; safe alt+mouse default."""

from __future__ import annotations

from typing import Any

import flet as ft
from relaydeck_terminal.protocol import ORIGIN_KEY, ViewState
from relaydeck_terminal.terminal import RelayDeckTerminal

from relaydeck.client.features.terminal.input_modes import ROUTE_PTY, WORKFLOW_AGENT_DIRECT
from relaydeck.client.features.terminal.models import TerminalViewState
from relaydeck.client.features.terminal.screen import TerminalScreen
from relaydeck.client.features.terminal.scrolling import use_terminal_scroll
from tests.support.host_harness import render_component, run
from tests.support.terminal.sessions import _agent_session


@ft.component
def _scroll_probe(session: Any, send: Any, touch: bool, captured: dict[str, Any]) -> Any:
    captured.update(use_terminal_scroll(session, send, touch=touch))
    return ft.Container(key="scroll-probe")


async def _probed(touch: bool) -> tuple[Any, Any, list[tuple[int, Any]], dict[str, Any]]:
    session, _ops, _factory, _submitted = _agent_session()
    session.open()
    await session.confirm_takeover()
    session.set_workflow(WORKFLOW_AGENT_DIRECT)
    sent: list[tuple[int, Any]] = []

    async def send(origin: int, data: Any, **kwargs: Any) -> str:
        del kwargs
        sent.append((origin, data))
        return ROUTE_PTY

    captured: dict[str, Any] = {}
    term = RelayDeckTerminal()
    session.bind_view(term)
    app = await render_component(_scroll_probe, session, send, touch, captured)
    captured["on_view_report"](term, ViewState(alt=True, at_bottom=True, mouse=True))
    await app.settle()
    return app, session, sent, captured


def test_touch_alt_mouse_unknown_encoder_offers_page_keys() -> None:
    async def scenario() -> None:
        _app, session, sent, captured = await _probed(True)
        try:
            assert captured["can_scroll"]() is True
            await captured["on_scroll"](-1)
            assert sent == [(ORIGIN_KEY, (b"\x1b[5~",))]
            await captured["on_scroll"](1)
            assert sent[-1] == (ORIGIN_KEY, (b"\x1b[6~",))
        finally:
            await session.dispose()

    run(scenario())


def test_mouse_alt_mouse_unknown_encoder_stays_hidden_and_inert() -> None:
    async def scenario() -> None:
        _app, session, sent, captured = await _probed(False)
        try:
            assert captured["can_scroll"]() is False
            await captured["on_scroll"](-1)
            assert sent == []
        finally:
            await session.dispose()

    run(scenario())


def test_scroll_controls_visible_on_touch_regardless_of_width() -> None:
    async def scenario() -> None:
        state = TerminalViewState(lease_mode="control")
        tablet = await render_component(
            TerminalScreen,
            state=state,
            on_jump_to_bottom=lambda: None,
            on_size_report=lambda c, r: None,
            on_input=lambda origin, data: None,
            compact=False,
            touch=True,
        )
        assert tablet.find("terminal-scroll-overlay").visible is True
        desktop = await render_component(
            TerminalScreen,
            state=state,
            on_jump_to_bottom=lambda: None,
            on_size_report=lambda c, r: None,
            on_input=lambda origin, data: None,
            compact=False,
            touch=False,
        )
        assert desktop.find("terminal-scroll-overlay").visible is False
        phone = await render_component(
            TerminalScreen,
            state=state,
            on_jump_to_bottom=lambda: None,
            on_size_report=lambda c, r: None,
            on_input=lambda origin, data: None,
            compact=True,
            touch=False,
        )
        assert phone.find("terminal-scroll-overlay").visible is True

    run(scenario())
