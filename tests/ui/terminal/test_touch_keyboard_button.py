"""On touch tablets the keyboard button is drawn by the native
terminal (a tap there focuses inside the gesture). The app tells the terminal to show it,
and the terminal's KEYBOARD_STATE report moves a Compose draft to Direct. Phone tier keeps
its direct-typing toggle. Synthetic session and a fake terminal; no network.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from relaydeck.client.features.terminal import input_modes
from relaydeck.client.features.terminal.agent_session import AgentSession
from relaydeck.client.features.terminal.surfaces import mount as mount_module
from tests.support.host_harness import render_component, run
from tests.support.terminal.sessions import _agent_session
from tests.support.terminal.touch_keyboard import _Screen


class _FakeTerm:
    """The native terminal as the app sees it: records the button flag and listeners."""

    font_size = 14

    def __init__(self) -> None:
        self.button: list[bool] = []
        self.listeners: list[Callable[[bool], None]] = []

    def set_keyboard_button(self, enabled: bool) -> None:
        self.button.append(enabled)

    def add_keyboard_state_listener(self, listener: Callable[[bool], None]) -> Callable[[], None]:
        self.listeners.append(listener)
        return lambda: self.listeners.remove(listener)

    def report(self, shown: bool) -> None:
        for listener in list(self.listeners):
            listener(shown)


@pytest.fixture
def fake_term(monkeypatch: Any) -> _FakeTerm:
    term = _FakeTerm()
    monkeypatch.setattr(AgentSession, "current_term", property(lambda _self: term))
    return term


def _record_requests(monkeypatch: Any) -> list[str]:
    """Record the terminal keyboard requests that would pass the Direct-workflow guard."""
    requested: list[str] = []

    def request(session: Any, _token: Any, *, agent_direct: bool = False) -> None:
        if agent_direct and session.view().workflow != input_modes.WORKFLOW_AGENT_DIRECT:
            return
        requested.append("request")

    monkeypatch.setattr(mount_module, "request_terminal_keyboard", request)
    return requested


def _has(app: Any, key: str) -> bool:
    try:
        app.find(key)
    except KeyError:
        return False
    return True


def test_tap_on_the_terminal_never_requests_the_keyboard_on_touch(monkeypatch: Any) -> None:
    requested = _record_requests(monkeypatch)

    async def scenario() -> None:
        session, _ops, _factory, _submitted = _agent_session()
        session.open()
        await session.confirm_takeover()
        session.set_workflow(input_modes.WORKFLOW_AGENT_DIRECT)
        app = await render_component(_Screen, session, True)
        try:
            await app.set_props_and_fire("terminal-direct-tap", {}, "tap")
            assert requested == []
        finally:
            await session.dispose()

    run(scenario())


def test_expanded_touch_hands_the_keyboard_button_to_the_native_terminal(
    fake_term: _FakeTerm,
) -> None:
    async def scenario() -> None:
        session, _ops, _factory, _submitted = _agent_session()
        session.open()
        await session.confirm_takeover()
        app = await render_component(_Screen, session, True)
        try:
            assert fake_term.button and fake_term.button[-1] is True
            assert not _has(app, "terminal-keyboard-button"), "no Flet keyboard button"
        finally:
            await session.dispose()

    run(scenario())


def test_desktop_does_not_show_the_native_keyboard_button(fake_term: _FakeTerm) -> None:
    async def scenario() -> None:
        session, _ops, _factory, _submitted = _agent_session()
        session.open()
        await session.confirm_takeover()
        await render_component(_Screen, session, False)
        try:
            assert fake_term.button and fake_term.button[-1] is False
        finally:
            await session.dispose()

    run(scenario())


def test_native_keyboard_shown_from_compose_switches_to_direct_and_keeps_the_draft(
    fake_term: _FakeTerm,
) -> None:
    async def scenario() -> None:
        session, _ops, _factory, _submitted = _agent_session()
        session.open()
        await session.confirm_takeover()
        session.set_workflow(input_modes.WORKFLOW_AGENT_COMPOSER)
        session.store.replace_draft("keep this")
        app = await render_component(_Screen, session, True)
        try:
            fake_term.report(True)
            await app.settle()
            assert session.view().workflow == input_modes.WORKFLOW_AGENT_DIRECT
            assert session.store.draft == "keep this"
        finally:
            await session.dispose()

    run(scenario())


def test_keyboard_button_is_shown_on_the_phone_tier_too(monkeypatch: Any) -> None:
    async def scenario() -> None:
        session, _ops, _factory, _submitted = _agent_session()
        session.open()
        await session.confirm_takeover()
        session.set_workflow(input_modes.WORKFLOW_AGENT_DIRECT)
        app = await render_component(_Screen, session, True, True)
        try:
            assert _has(app, "terminal-keyboard-button")
        finally:
            await session.dispose()

    run(scenario())
