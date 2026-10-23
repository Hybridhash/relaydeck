"""Done releases the live terminal keyboard or briefly disables Composer without losing its draft.
Repeated hides and unmounts cannot re-enable a retired editor.
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import Any

import flet as ft
import pytest

from relaydeck.client.features.composer import components as composer_components
from relaydeck.client.features.composer.components import DRAFT_KEY, ComposerBox
from relaydeck.client.features.terminal.input_modes import WORKFLOW_AGENT_DIRECT
from relaydeck.client.features.terminal.surfaces import (
    mount_agent_screen,
    release_terminal_keyboard,
)
from relaydeck.client.features.terminal.surfaces.common import AGENT_WORKFLOW_DIRECT_KEY
from relaydeck.client.hooks.typing_focus import use_typing_focus
from tests.support.host_harness import render_component, run
from tests.support.terminal.sessions import _agent_session


@pytest.fixture(autouse=True)
def _no_handler_errors(caplog: pytest.LogCaptureFixture) -> Any:
    caplog.set_level(logging.ERROR)
    yield
    errors = [
        record.getMessage()
        for phase in ("setup", "call", "teardown")
        for record in caplog.get_records(phase)
        if record.levelno >= logging.ERROR
    ]
    assert errors == []


@ft.component
def _Column(session: Any, route: tuple[str, str] = ("agents", "agent-1")) -> Any:
    _cancel, on_focus, _set, typing_focus, exit_typing, field_focused, keep = use_typing_focus(
        sticky=True, viewport=(1376.0, 800.0), route=route
    )
    return ft.Container(
        expand=True,
        content=mount_agent_screen(  # type: ignore[call-arg]
            session,
            width_tier="expanded",
            phone=False,
            viewport_height=800,
            viewport_width=1376.0,
            touch=True,
            typing_split=typing_focus,
            on_composer_focus=on_focus,
            on_keep_typing=keep,
            on_typing_exit=exit_typing,
            keyboard_up=field_focused,
        ),
    )


async def _click(app: Any, key: str) -> None:
    await app.session.dispatch_event(app.find(key)._i, "click", "")


def _gone(app: Any, key: str) -> bool:
    try:
        app.find(key)
    except KeyError:
        return True
    return False


class _Spawns:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.count = 0
        real = composer_components.spawn

        def counted(coro: Any, *, label: str) -> Any:
            if label == "Close composer keyboard":
                self.count += 1
            return real(coro, label=label)

        monkeypatch.setattr(composer_components, "spawn", counted)


async def _open_split(session: Any) -> Any:
    session.open()
    await session.confirm_takeover()
    app = await render_component(_Column, session)
    await app.set_props_and_fire("agent-detail-panel", {}, "size_change", {"w": 1376.0, "h": 800.0})
    return app


def test_done_in_compose_disables_the_field_for_one_update_and_keeps_the_draft(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        spawns = _Spawns(monkeypatch)
        session, _ops, _factory, _submitted = _agent_session()
        session.on_draft_change("keep me")
        app = await _open_split(session)
        try:
            await asyncio.sleep(0.1)
            await app.settle()
            assert spawns.count == 0  # nothing hides on first mount
            assert not app.find(DRAFT_KEY).disabled
            await app.set_props_and_fire(DRAFT_KEY, {}, "focus")
            assert app.find("agent-typing-done") is not None
            await _click(app, "agent-typing-done")
            await asyncio.sleep(0.02)
            assert _gone(app, "agent-typing-done")  # typing mode is off
            assert app.find(DRAFT_KEY).disabled
            await asyncio.sleep(0.12)
            await app.settle()
            assert not app.find(DRAFT_KEY).disabled
            assert app.find(DRAFT_KEY).value == "keep me"
            assert spawns.count == 1
        finally:
            await session.dispose()

    run(scenario())


def test_done_again_after_re_entering_hides_again(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        spawns = _Spawns(monkeypatch)
        session, _ops, _factory, _submitted = _agent_session()
        app = await _open_split(session)
        try:
            for round_number in (1, 2):
                await app.set_props_and_fire(DRAFT_KEY, {}, "focus")
                await app.set_props_and_fire("agent-typing-done", {}, "click")
                await asyncio.sleep(0.12)
                await app.settle()
                assert spawns.count == round_number
                assert not app.find(DRAFT_KEY).disabled
        finally:
            await session.dispose()

    run(scenario())


def test_done_in_direct_releases_the_live_terminal_once_and_never_enables_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        session, _ops, _factory, _submitted = _agent_session()
        app = await _open_split(session)
        try:
            await app.set_props_and_fire(DRAFT_KEY, {}, "focus")
            await app.set_props_and_fire(AGENT_WORKFLOW_DIRECT_KEY, {}, "click")
            assert session.view().workflow == WORKFLOW_AGENT_DIRECT
            term = session.current_term
            assert term is not None
            calls: list[str] = []
            monkeypatch.setattr(term, "release_keyboard", lambda: calls.append("release"))
            monkeypatch.setattr(
                term, "set_input_enabled", lambda *_a: calls.append("set_input_enabled")
            )
            await _click(app, "agent-typing-done")
            assert calls == ["release"]
        finally:
            await session.dispose()

    run(scenario())


def _stub(**changes: Any) -> Any:
    term = SimpleNamespace(
        released=0,
        release_keyboard=lambda: None,
        set_input_enabled=lambda *_a: pytest.fail("input mode must not change"),
    )
    state = {"current_term": term, "disposed": False, "paused": False, "input_token": (9, 9)}
    state.update(changes)
    return SimpleNamespace(**state)


@pytest.mark.parametrize("fence", [{"paused": True}, {"input_token": (1, 1)}])
def test_release_ignores_pause_and_a_stale_token(fence: dict[str, Any]) -> None:
    released: list[int] = []
    session = _stub(**fence)
    session.current_term.release_keyboard = lambda: released.append(1)
    release_terminal_keyboard(session)
    assert released == [1]


@pytest.mark.parametrize("fence", [{"current_term": None}, {"disposed": True}])
def test_release_needs_a_live_terminal(fence: dict[str, Any]) -> None:
    session = _stub(**fence)
    if session.current_term is not None:
        session.current_term.release_keyboard = lambda: pytest.fail("no terminal to release")
    release_terminal_keyboard(session)


@ft.component
def _Box() -> Any:
    requests, set_requests = ft.use_state(0)
    shown, set_shown = ft.use_state(True)
    return ft.Column(
        controls=[
            ft.Button("hide", key="hide", on_click=lambda _e: set_requests(requests + 1)),
            ft.Button("drop", key="drop", on_click=lambda _e: set_shown(False)),
            ComposerBox(
                draft="",
                markers=(),
                can_send=False,
                blockers=(),
                submission="idle",
                error=None,
                review=None,
                input_mode="text",
                on_draft_change=lambda _t: None,
                on_send=lambda: None,
                on_confirm_blocked=lambda: None,
                on_cancel_blocked=lambda: None,
                on_clear_error=lambda: None,
                hide_requests=requests,
            )
            if shown
            else ft.Text("gone", key="gone"),
        ]
    )


def test_repeated_hides_reopen_once_after_the_last() -> None:
    async def scenario() -> None:
        app = await render_component(_Box)
        await asyncio.sleep(0.1)
        await app.settle()
        assert not app.find(DRAFT_KEY).disabled
        await _click(app, "hide")
        await asyncio.sleep(0.03)
        await _click(app, "hide")
        await asyncio.sleep(0.03)
        assert app.find(DRAFT_KEY).disabled  # the first round's timer must not re-enable it
        await asyncio.sleep(0.1)
        await app.settle()
        assert not app.find(DRAFT_KEY).disabled

    run(scenario())


def test_a_stale_reopen_after_unmount_is_harmless() -> None:
    async def scenario() -> None:
        app = await render_component(_Box)
        await app.set_props_and_fire("hide", {}, "click")
        await app.set_props_and_fire("drop", {}, "click")
        assert app.find("gone") is not None
        await asyncio.sleep(0.15)
        await app.settle()
        assert app.find("gone") is not None

    run(scenario())
