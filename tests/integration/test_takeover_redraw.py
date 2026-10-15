"""Take over keeps the screen the viewer already shows.

Owner symptom: watching an idle agent (view lease), then Take over, left the terminal
nearly blank: only the last lines at the top, the earlier screen and the prompt gone.
Mechanism: confirm_takeover reset the bound native emulator and the new control stream
only carries post-takeover bytes, so the TUI's incremental updates land on a blank screen.
Synthetic data only; real gateway ring and ASGI sockets.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import Any

import pytest

from relaydeck.client.features.terminal import agent_session
from relaydeck.client.features.terminal.warm_watch import watch_only
from relaydeck.gateway.ring import TerminalRing
from tests.support.host_harness import run
from tests.support.terminal.reader_handoff import _actor
from tests.support.terminal.switch_gap import _Renderer, _TicketOnlySocket, _write

SCREEN = b"\x1b[2J\x1b[HRelayDeck synthetic agent\r\n> prompt\r\n"
PROMPT = b"> prompt\r\n"


async def _watch_with_screen(ctx: Any, monkeypatch: pytest.MonkeyPatch) -> _Renderer:
    actor, backend, lease = ctx.actor, ctx.backend, ctx.lease
    ctx.readers[0].cancel()
    with suppress(asyncio.CancelledError):
        await ctx.readers[0]
    term = _Renderer()
    actor.bind_view(term, write=_write)
    fanout = backend.fanout_for(lease.attachment_id)
    fanout.ring = TerminalRing(lease.epoch, ctx.clock.now)
    fanout.ring.append(SCREEN, ctx.clock.now)

    def ticket_only(ticket: str) -> Any:
        socket = _TicketOnlySocket(ctx.factory, ticket)
        ctx.factory.sockets.append(socket)
        return socket

    monkeypatch.setattr(actor, "_socket_factory", ticket_only)
    assert await watch_only(actor, lambda: True)
    ctx.readers.append(asyncio.create_task(actor.run()))
    return term


def test_take_over_keeps_the_screen_the_viewer_already_shows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(agent_session, "WATCH_RETRY_DELAYS_S", (0.05, 0.05, 0.05, 0.05, 0.05))

    async def scenario() -> None:
        async with _actor("agent") as ctx:
            actor = ctx.actor
            term = await _watch_with_screen(ctx, monkeypatch)
            for _ in range(50):  # the watch replays asynchronously
                if b"".join(term.output).endswith(PROMPT):
                    break
                await asyncio.sleep(0.02)
            assert b"".join(term.output).endswith(PROMPT)
            actor.request_takeover()
            await actor.confirm_takeover()
            assert actor.lease.mode == "control"
            # The emulator the viewer shows is not cleared by the takeover...
            assert term.resets == 0
            assert b"".join(term.output).endswith(PROMPT)

    run(scenario())


def test_reload_resumes_the_session_without_a_reconnect_tap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """a reloaded page replaces the native viewer (the old lease ends).
    With identity verified and the Settings takeover rule on, the session resumes as
    Reconnect plus automatic takeover, and the new viewer starts from a fresh emulator."""
    from relaydeck.client.features.terminal.stream import EMULATOR_FRESH

    monkeypatch.setattr(agent_session, "WATCH_RETRY_DELAYS_S", (0.05, 0.05, 0.05, 0.05, 0.05))

    async def scenario() -> None:
        async with _actor("agent") as ctx:
            actor = ctx.actor
            await _watch_with_screen(ctx, monkeypatch)
            actor.request_takeover()
            await actor.confirm_takeover()
            assert actor.lease.mode == "control"
            # The page reloads: the native viewer is replaced and the control lease ends.
            actor._viewer_reopened("Viewer restarted; reattach explicitly.")
            assert actor.phase == "disconnected"
            actor.auto_take_allowed = True
            await actor.recover_after_reload()
            assert actor.phase == "live"
            assert actor.lease.mode == "control"
            assert actor.controller.emulator == EMULATOR_FRESH

    run(scenario())


def test_takeover_on_an_unverified_transport_starts_the_emulator_fresh_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bound terminal still fenced on an unverified transport
    is re-synced by one reset on takeover (fence retired, screen rebuilt from the stream).
    The same takeover without that fence keeps the emulator and does not reset."""
    from relaydeck.client.features.terminal.stream import EMULATOR_FRESH, EMULATOR_PRESERVED

    monkeypatch.setattr(agent_session, "WATCH_RETRY_DELAYS_S", (0.05, 0.05, 0.05, 0.05, 0.05))

    async def takeover(unverified: bool) -> tuple[str, int]:
        async with _actor("agent") as ctx:
            actor = ctx.actor
            term = await _watch_with_screen(ctx, monkeypatch)
            term.resume_unverified = unverified
            actor.request_takeover()
            await actor.confirm_takeover()
            assert actor.lease.mode == "control"
            return actor.controller.emulator, term.resets

    fresh, fresh_resets = run(takeover(True))
    assert fresh == EMULATOR_FRESH and fresh_resets == 1
    kept, kept_resets = run(takeover(False))
    assert kept == EMULATOR_PRESERVED and kept_resets == 0
