"""A parked agent reopened after the phone returns from the background takes control again.

Leaving an agent parks it on a watch-only lease. Hiding the page is only observed; while hidden
the server drops the watch socket, or the agent's output overflows the parked viewer whose native
channel went with the page. Reopening the agent with automatic takeover on must end in control
without a tap, exactly like a normal reopen; another holder, the takeover setting and an exited
agent keep their usual answers."""

from __future__ import annotations

import json
import logging
from typing import Any

import pytest

from relaydeck.client.features.inventory.routing import RouteBridge
from relaydeck.client.features.settings.prefs import PREFS_KEY
from relaydeck.client.features.terminal.diagnostics import diagnostics_logger
from tests.support.host_harness import run
from tests.support.terminal.retained_transport import resource, until
from tests.support.ui.instant_open import jobs
from tests.support.ui.packaged import Preferences
from tests.support.ui.retained_root import _open, _render

pytestmark = pytest.mark.auto_takeover  # the real open flow takes control on its own

AGENT = "agent-home-mac:term-1"


class _Reopens(logging.Handler):
    """The `reopen site=` diagnostics, in order."""

    def __init__(self) -> None:
        super().__init__()
        self.sites: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        message = record.getMessage()
        if message.startswith("reopen site="):
            self.sites.append(message.split()[1].removeprefix("site="))


def _no_input(scripted: Any) -> None:
    assert scripted.submitted == [] and scripted.prompted == []
    assert not any(
        kind == "bytes" for _, _, sock in scripted.streams.opened for kind, _ in sock.sent
    )


def _takeovers(scripted: Any) -> int:
    return sum(1 for item in scripted.ops.attaches if item["takeover"] is True)


async def _parked(automatic: bool = True) -> tuple[Any, ...]:
    """Render, open the agent (in control), leave it: a live watch-only lease remains."""
    prefs = Preferences({PREFS_KEY: json.dumps({"auto_takeover": automatic})})
    app, _, scripted, pool, _ = await _render(
        preferences=prefs, bridge=RouteBridge(width=390, height=844)
    )
    resources = resource(pool)
    if automatic:
        await app.set_props_and_fire(AGENT, {}, "click")
    else:
        await _open(app, "home-mac:term-1")
    await until(
        lambda: pool.foreground is not None
        and pool.foreground.session.lease is not None
        and pool.foreground.session.lease.mode == "control"
    )
    entry = pool.foreground
    session = entry.session
    await app.set_props_and_fire("terminal-back", {}, "click")
    await jobs(pool)
    assert pool.foreground is None
    assert session.lease.mode == "view" and session.phase == "live"
    socket = next(
        sock for lease, _, sock in scripted.streams.opened if lease == session.lease.lease_id
    )
    return app, scripted, pool, resources, entry, session, socket


def _overflow_parked_viewer(session: Any) -> None:
    # The agent keeps producing output while the parked viewer's native channel is gone: its
    # queue overflows and the view controller waits for a live-edge resume nobody drives.
    controller = session.controller
    controller.on_viewer_overflow()
    assert controller.live_edge_pending


@pytest.mark.parametrize(
    "drop", ["while_hidden", "at_reopen", "viewer_overflow", "viewer_overflow_then_closed"]
)
def test_a_parked_agent_reopened_after_the_app_returns_takes_control_again(drop: str) -> None:
    async def scenario() -> None:
        app, scripted, pool, resources, entry, session, socket = await _parked()
        parked = session.lease
        reopens = _Reopens()
        logger = diagnostics_logger()
        logger.addHandler(reopens)
        try:
            resources.set_page_visible(False)
            if drop.startswith("viewer_overflow"):
                _overflow_parked_viewer(session)
            if drop in ("while_hidden", "viewer_overflow_then_closed"):
                await socket.close()
                await until(lambda: session.phase == "disconnected")
            resources.set_page_visible(True)
            await jobs(pool)
            reopens.sites.clear()
            attaches = len(scripted.ops.attaches)
            await app.set_props_and_fire(AGENT, {}, "click")
            if drop == "at_reopen":
                await socket.close()
            await until(
                lambda: session.lease is not None
                and session.lease.mode == "control"
                and session.phase == "live"
            )
            assert pool.foreground is entry and session.lease is not parked
            assert "auto_take" in reopens.sites
            assert not {"take_over", "reconnect"} & set(reopens.sites)
            assert len(scripted.ops.attaches) == attaches + 1
            _no_input(scripted)
        finally:
            logger.removeHandler(reopens)
            await resources.close()
            await app.set_props_and_fire("test-unmount", {}, "click")

    run(scenario())


def test_a_parked_agent_held_elsewhere_asks_first_instead_of_taking_control() -> None:
    async def scenario() -> None:
        app, scripted, pool, resources, entry, session, _ = await _parked()
        try:
            resources.set_page_visible(False)
            _overflow_parked_viewer(session)
            ops = scripted.ops
            attachment = session.attachment
            ops._control_holder[attachment.attachment_id] = (ops.account, "other-device")
            resources.set_page_visible(True)
            await jobs(pool)
            takeovers = _takeovers(scripted)
            await app.set_props_and_fire(AGENT, {}, "click")
            await until(lambda: session.phase == "conflict")
            await app.settle()
            assert session.view().owner_label  # the phone names the holder in its panel
            assert _takeovers(scripted) == takeovers
            assert ops._control_holder[attachment.attachment_id] == (ops.account, "other-device")
            assert session.lease is None or session.lease.mode == "view"
            _no_input(scripted)
        finally:
            await resources.close()
            await app.set_props_and_fire("test-unmount", {}, "click")

    run(scenario())


def test_a_parked_agent_stays_a_viewer_when_automatic_takeover_is_off() -> None:
    async def scenario() -> None:
        app, scripted, pool, resources, entry, session, _ = await _parked(automatic=False)
        reopens = _Reopens()
        logger = diagnostics_logger()
        logger.addHandler(reopens)
        try:
            resources.set_page_visible(False)
            _overflow_parked_viewer(session)
            resources.set_page_visible(True)
            await jobs(pool)
            reopens.sites.clear()
            takeovers = _takeovers(scripted)
            await app.set_props_and_fire(AGENT, {}, "click")
            await app.settle()
            await jobs(pool)
            await app.settle()
            assert pool.foreground is entry
            assert session.lease is None or session.lease.mode == "view"
            assert "auto_take" not in reopens.sites and _takeovers(scripted) == takeovers
            _no_input(scripted)
        finally:
            logger.removeHandler(reopens)
            await resources.close()
            await app.set_props_and_fire("test-unmount", {}, "click")

    run(scenario())


def test_an_exited_parked_agent_is_never_taken_over_on_reopen() -> None:
    async def scenario() -> None:
        app, scripted, pool, resources, entry, session, socket = await _parked()
        reopens = _Reopens()
        logger = diagnostics_logger()
        logger.addHandler(reopens)
        try:
            resources.set_page_visible(False)
            _overflow_parked_viewer(session)
            socket.queue(("text", json.dumps({"kind": "bye", "detail": "exited"})))
            await until(lambda: session.phase == "exited")
            resources.set_page_visible(True)
            await jobs(pool)
            reopens.sites.clear()
            takeovers = _takeovers(scripted)
            await app.set_props_and_fire(AGENT, {}, "click")
            await app.settle()
            await jobs(pool)
            await app.settle()
            assert session.phase == "exited" and _takeovers(scripted) == takeovers
            assert "take_over" not in reopens.sites
            _no_input(scripted)
        finally:
            logger.removeHandler(reopens)
            await resources.close()
            await app.set_props_and_fire("test-unmount", {}, "click")

    run(scenario())
