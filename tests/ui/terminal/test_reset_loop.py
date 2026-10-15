"""A live terminal viewer must not reset in a loop.

Real Flet dispatch on a bound, live viewer. Each reset of the viewer increments its reset counter; a
reset storm used to reset the viewer once per state set, without limit. These tests fail on the code
before the fix (every reset state set reset the viewer again; there was no cap).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from typing import Any

import pytest

from relaydeck.client.features.inventory.routing import RouteBridge
from relaydeck.client.features.terminal.session import rendering
from tests.support.host_harness import run
from tests.support.terminal.resume import _hello, _MuxConn
from tests.support.ui.instant_inventory import valid_inventory
from tests.support.ui.navigation import _StubTransport
from tests.support.ui.retained_root import _open, _render


async def settle(app: Any, rounds: int) -> None:
    for _ in range(rounds):
        await asyncio.sleep(0.01)
        await app.settle()


@pytest.fixture
def stubbed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_StubTransport, "snapshot", valid_inventory)


async def _bound_live_viewer() -> tuple[Any, Any, Any]:
    app, _metadata, _scripted, pool, _clock = await _render(
        bridge=RouteBridge(width=390, height=844)
    )
    await _open(app, "home-mac:term-1")
    entry = pool.foreground
    session, term = entry.session, entry.session.current_term
    conn = _MuxConn()
    app.session.attach_connection(conn)
    await app.session.dispatch_event(
        term._i, "data_channel_open", {"channel_name": "terminal", "channel_id": 7}
    )
    conn.channels[7].deliver(_hello(instance=123))
    await app.settle()
    await settle(app, 10)
    return app, session, term


def test_a_reset_state_announced_again_does_not_reset_the_viewer_again(stubbed: None) -> None:
    async def scenario() -> None:
        app, session, term = await _bound_live_viewer()
        assert session._renderer.terms(), "the scenario needs a bound viewer"
        before = term._reset_count
        for _ in range(3):
            session.update_term_state(replace(session.term_state, status="reset"))
            await settle(app, 5)
        assert term._reset_count - before == 1, "one transition into reset is one reset"

    run(scenario())


def test_a_reset_storm_is_capped_per_viewer_and_logged_once(
    stubbed: None, caplog: pytest.LogCaptureFixture
) -> None:
    async def scenario() -> None:
        app, session, term = await _bound_live_viewer()
        before = term._reset_count
        caplog.set_level(logging.WARNING, logger="relaydeck.client.terminal")
        for _ in range(20):
            session.update_term_state(replace(session.term_state, status="reset"))
            await settle(app, 1)
            session.update_term_state(replace(session.term_state, status="live"))
            await settle(app, 1)
        assert term._reset_count - before <= rendering.RESET_LIMIT
        capped = [r for r in caplog.records if "terminal_reset_capped" in r.getMessage()]
        assert len(capped) == 1, "the cap is announced once, with no viewer content"

    run(scenario())


def test_the_cap_admits_ordinary_resets(stubbed: None) -> None:
    """A few real resets (takeover, reattach) still reset the viewer each time."""

    async def scenario() -> None:
        app, session, term = await _bound_live_viewer()
        before = term._reset_count
        for _ in range(rendering.RESET_LIMIT - 1):
            session._renderer.reset_bound_renderers("controller_replaced")
            await settle(app, 2)
        assert term._reset_count - before == rendering.RESET_LIMIT - 1

    run(scenario())
