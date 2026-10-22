"""The phone "Agent is working" label shows above the input only while the agent works."""

from __future__ import annotations

from typing import Any

from relaydeck.client.features.terminal import input_modes
from tests.support.host_harness import render_component, run
from tests.support.terminal.direct_typing import _direct
from tests.support.terminal.sessions import _agent_session
from tests.support.terminal.touch_keyboard import _Screen


def _has(app: Any, key: str) -> bool:
    try:
        app.find(key)
    except KeyError:
        return False
    return True


def test_composer_label_follows_the_agent_status() -> None:
    async def scenario() -> None:
        session, _ops, _factory, _submitted = _agent_session()
        session.open()
        await session.confirm_takeover()
        session.set_workflow(input_modes.WORKFLOW_AGENT_COMPOSER)
        app = await render_component(_Screen, session, True, True)
        try:
            assert not _has(app, "composer-working")
            session.set_agent_status("working")
            await app.settle()
            assert _has(app, "composer-working") and not _has(app, "direct-working")
            session.set_agent_status("idle")
            await app.settle()
            assert not _has(app, "composer-working")
        finally:
            await session.dispose()

    run(scenario())


def test_direct_input_shows_the_same_label_above_its_keys() -> None:
    async def scenario() -> None:
        app, session, _socket, _focuses = await _direct(False)
        try:
            assert not _has(app, "direct-working")
            session.set_agent_status("working")
            await app.settle()
            assert _has(app, "direct-working") and not _has(app, "composer-working")
            session.set_agent_status("idle")
            await app.settle()
            assert not _has(app, "direct-working")
        finally:
            await session.dispose()

    run(scenario())
