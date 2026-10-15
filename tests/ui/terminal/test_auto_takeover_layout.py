"""An automatic takeover lays out as in control from the first frame.

The take-over panel and the in-control controls have different heights, so showing the panel for
the moment before the automatic takeover resized the terminal (and a full-screen program redrew)
several times per open. While the takeover is expected, the screen shows the in-control layout
with a floating "Connecting" chip; a conflict or failure falls back to the usual panels.
"""

from __future__ import annotations

import asyncio

from relaydeck.client.features.terminal.agent_session import AGENT_CONFLICT, AGENT_LIVE
from relaydeck.client.features.terminal.surfaces import AGENT_TAKEOVER_DISCLOSURE_KEY
from relaydeck.client.features.terminal.surfaces.agent_hooks import OPEN_HOLD_S
from relaydeck.client.features.terminal.surfaces.mount import mount_agent_screen
from tests.support.host_harness import render_component, run
from tests.support.terminal.sessions import _agent_session

CHIP = "terminal-connecting"


def _present(app: object, key: str) -> bool:
    try:
        app.find(key)  # type: ignore[attr-defined]
    except KeyError:
        return False
    return True


def test_an_expected_takeover_shows_the_in_control_layout_and_the_chip() -> None:
    async def scenario() -> None:
        session, _ops, _factory, _ = _agent_session()
        session.expect_control()
        session.open()
        assert session.control_pending
        app = await render_component(mount_agent_screen, session)
        assert not _present(app, AGENT_TAKEOVER_DISCLOSURE_KEY)
        assert app.find(CHIP).visible is True
        # The takeover completes: same layout, chip gone.
        assert await session.confirm_takeover() == AGENT_LIVE
        session.expect_control(False)
        assert not session.control_pending
        live = await render_component(mount_agent_screen, session)
        # A newly mounted screen first holds while its size settles (chip shown), then shows.
        await asyncio.sleep(OPEN_HOLD_S + 0.1)
        await live.settle()
        assert live.find(CHIP).visible is False

    run(scenario())


def test_without_an_expected_takeover_the_disclosure_panel_stays() -> None:
    async def scenario() -> None:
        session, _ops, _factory, _ = _agent_session()
        session.open()
        assert not session.control_pending
        app = await render_component(mount_agent_screen, session)
        assert _present(app, AGENT_TAKEOVER_DISCLOSURE_KEY)

    run(scenario())


def test_a_conflict_ends_the_pending_layout() -> None:
    session, _ops, _factory, _ = _agent_session()
    session.expect_control()
    session.open()
    session._phase = AGENT_CONFLICT  # another device holds control
    assert not session.control_pending
    session._phase = AGENT_LIVE  # live but only watching: still pending until control
    assert session.control_pending


def test_cancel_and_completion_clear_the_expectation() -> None:
    session, _ops, _factory, _ = _agent_session()
    session.expect_control()
    session.open()
    session.cancel_takeover()
    assert not session.control_pending
