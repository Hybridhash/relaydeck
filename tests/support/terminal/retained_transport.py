"""Retained terminal fixtures over the real Flet channel lifecycle."""

from __future__ import annotations

import asyncio
import struct
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import flet as ft
from relaydeck_terminal import protocol

from relaydeck.client.features.terminal.screen import TerminalScreen
from relaydeck.client.resources import AppResources
from relaydeck.client.retained_sessions import RetainedSessions
from tests.support.host_harness import render_component
from tests.support.scripted_sessions import make_scripted_sessions
from tests.support.terminal.resume import _hello, _MuxConn


async def until(check: Any) -> None:
    async with asyncio.timeout(4):
        while not check():
            await asyncio.sleep(0.01)


def resource(pool: RetainedSessions) -> AppResources:
    async def close() -> None:
        pass

    return AppResources(
        props={},
        client=None,
        owns_client=False,
        retained=pool,
        bridge=None,
        controller=None,
        transport=None,
        commands=SimpleNamespace(close=close),
    )  # type: ignore[arg-type]


async def capture(app: Any, term: Any, conn: Any, number: int, *, instance: int = 123) -> Any:
    await app.session.dispatch_event(
        term._i, "data_channel_open", {"channel_name": "terminal", "channel_id": number}
    )
    channel = conn.channels[number]
    channel.deliver(_hello(instance=instance))
    await app.settle()
    return channel


def requests(conn: Any) -> list[tuple[int, int]]:
    return [
        (number, struct.unpack_from(">I", packet, 1)[0])
        for number, packet in conn.data_frames
        if packet[0] == protocol.RESUME_PROBE
    ]


async def mounted(kind: str) -> tuple[Any, ...]:
    scripted = make_scripted_sessions()
    if kind == "agent":
        session = scripted.factory.make_agent(
            host_id="mac", host_epoch="e1", agent_id="a", target="a"
        )
        session.open()
        await session.confirm_takeover()
        session.on_draft_change("keep this draft")
    else:
        session = scripted.factory.make_shell(host_id="mac")
        await session.open("shell")
    session.controller._set(replace(session.controller.state, status="live"))
    pool = RetainedSessions()
    entry = await pool.acquire("mac", kind, "key", "e1", lambda: session)
    resources = resource(pool)

    @ft.component
    def Surface() -> Any:
        _, tick = ft.use_state(0)
        ft.use_effect(lambda: session.subscribe(lambda _: tick(lambda n: n + 1)), [])

        def bind(term: Any) -> None:
            if term is None:
                session.unbind_view()
            else:
                session.bind_view(term)

        return TerminalScreen(
            session.term_state,
            lambda: None,
            lambda *_: None,
            lambda *_: None,
            on_terminal=bind,
            input_enabled=True,
            phone=True,
        )

    app = await render_component(Surface)
    conn = _MuxConn()
    app.session.attach_connection(conn)
    term = session.current_term
    old = await capture(app, term, conn, 7)
    return scripted, session, entry, resources, app, term, old
