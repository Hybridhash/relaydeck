"""Native committed text uses the generation-bound paste review."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from flet.data_channel import _ProtocolMuxedDataChannel
from relaydeck_terminal import protocol

from relaydeck.client.features.shell.screen import mount_shell_screen
from relaydeck.client.features.shell.session import ShellSession
from relaydeck.client.features.terminal.agent_session import AgentSession
from relaydeck.client.features.terminal.input_modes import (
    ROUTE_DRAFT,
    ROUTE_DROPPED,
    ROUTE_PTY,
    WORKFLOW_AGENT_COMPOSER,
    WORKFLOW_AGENT_DIRECT,
)
from relaydeck.client.features.terminal.paste_review import REQUEST_REJECTED, REQUEST_REVIEW
from relaydeck.client.features.terminal.surfaces import mount_agent_screen
from relaydeck.contracts.wire.terminal import decode_input
from tests.support.host_harness import render_component
from tests.support.scripted_sessions import ScriptedSessions, make_scripted_sessions

Session = AgentSession | ShellSession


@asynccontextmanager
async def connected(shell: bool) -> AsyncIterator[tuple[Session, ScriptedSessions]]:
    ports = make_scripted_sessions()
    if shell:
        session: Session = ports.factory.make_shell(host_id="mac")
    else:
        session = ports.factory.make_agent(
            host_id="mac", host_epoch="h1", agent_id="pane", target="pane"
        )
    try:
        if isinstance(session, AgentSession):
            session.open()
            assert await session.confirm_takeover() == "live"
            session.set_workflow(WORKFLOW_AGENT_DIRECT)
            session.on_draft_change("kept draft")
        else:
            assert await session.open("pane") == "live"
        assert session.keys_enabled
        yield session, ports
    finally:
        await session.dispose(clear_sensitive=True)


def inputs(ports: ScriptedSessions) -> list[bytes]:
    return [
        decode_input(data).data
        for _, _, socket in ports.streams.opened
        for kind, data in socket.sent
        if kind == "bytes"
    ]


@pytest.mark.parametrize("shell", [False, True])
@pytest.mark.parametrize("text", ["one\ntwo", "one\rtwo", "one\ttwo", "\r\n"])
def test_native_bulk_text_waits_for_exact_review_confirmation(shell: bool, text: str) -> None:
    async def scenario() -> None:
        async with connected(shell) as (session, ports):
            result = await session.handle_view_input(
                protocol.ORIGIN_TEXT, text.encode(), token=session.input_token, native_text=True
            )
            assert result == REQUEST_REVIEW and inputs(ports) == []
            assert session.sessions.paste.pending.text == text
            assert await session.confirm_paste()
            assert inputs(ports) == [text.encode()]
            assert not await session.confirm_paste()
            if isinstance(session, AgentSession):
                assert session.store.draft == "kept draft"

    asyncio.run(scenario())


@pytest.mark.parametrize("shell", [False, True])
@pytest.mark.parametrize("text", ["a", "hello", "你好", "\r", "\n"])
def test_native_plain_typing_and_lone_return_stay_direct(shell: bool, text: str) -> None:
    async def scenario() -> None:
        async with connected(shell) as (session, ports):
            assert (
                await session.handle_view_input(
                    protocol.ORIGIN_TEXT, text.encode(), token=session.input_token, native_text=True
                )
                == ROUTE_PTY
            )
            assert inputs(ports) == [b"\r" if text == "\n" else text.encode()]
            assert session.pending_review is None

    asyncio.run(scenario())


@pytest.mark.parametrize("shell", [False, True])
@pytest.mark.parametrize("text", ["\x1b[200~one\rtwo\x1b[201~", "bad\x00text", "\x7f"])
def test_native_unsafe_text_is_rejected_without_input(shell: bool, text: str) -> None:
    async def scenario() -> None:
        async with connected(shell) as (session, ports):
            assert (
                await session.handle_view_input(
                    protocol.ORIGIN_TEXT, text.encode(), token=session.input_token, native_text=True
                )
                == REQUEST_REJECTED
            )
            assert session.pending_review is None and inputs(ports) == []

    asyncio.run(scenario())


@pytest.mark.parametrize("shell", [False, True])
def test_field_origin_del_and_returns_keep_exact_input(shell: bool) -> None:
    async def scenario() -> None:
        async with connected(shell) as (session, ports):
            for data in (b"\x7f", b"\r", b"\n"):
                assert (
                    await session.handle_view_input(
                        protocol.ORIGIN_TEXT, data, token=session.input_token
                    )
                    == ROUTE_PTY
                )
            assert inputs(ports) == [b"\x7f", b"\r", b"\r"]
            assert session.pending_review is None

    asyncio.run(scenario())


def test_native_composer_multiline_stays_in_the_draft() -> None:
    async def scenario() -> None:
        async with connected(False) as (session, ports):
            assert isinstance(session, AgentSession)
            session.set_workflow(WORKFLOW_AGENT_COMPOSER)
            assert (
                await session.handle_view_input(
                    protocol.ORIGIN_TEXT,
                    b"one\ntwo\tthree",
                    token=session.input_token,
                    native_text=True,
                )
                == ROUTE_DRAFT
            )
            assert session.store.draft == "kept draftone\ntwo\tthree"
            assert session.pending_review is None and inputs(ports) == []

    asyncio.run(scenario())


@pytest.mark.parametrize("shell", [False, True])
def test_native_review_cannot_confirm_after_generation_loss_or_replay(shell: bool) -> None:
    async def scenario() -> None:
        async with connected(shell) as (session, ports):
            assert (
                await session.handle_view_input(protocol.ORIGIN_TEXT, b"one\ntwo", native_text=True)
                == REQUEST_REVIEW
            )
            controller = session.controller
            assert controller is not None
            generation = controller.generation
            controller.generation += 1
            assert not await session.confirm_paste()
            controller.generation = generation
            assert not await session.confirm_paste()
            assert await session.flush_keys() == 0 and inputs(ports) == []

    asyncio.run(scenario())


@pytest.mark.parametrize("shell", [False, True])
def test_stale_or_background_native_text_never_opens_review(shell: bool) -> None:
    async def scenario() -> None:
        async with connected(shell) as (session, ports):
            stale = session.input_token
            session.sessions.keys.invalidate()
            assert (
                await session.handle_view_input(
                    protocol.ORIGIN_TEXT, b"one\ntwo", token=stale, native_text=True
                )
                == ROUTE_DROPPED
            )
            session.set_foreground(False)
            assert (
                await session.handle_view_input(protocol.ORIGIN_TEXT, b"one\ntwo", native_text=True)
                == ROUTE_DROPPED
            )
            assert session.pending_review is None and inputs(ports) == []

    asyncio.run(scenario())


@pytest.mark.parametrize("shell", [False, True])
def test_native_single_character_still_consumes_ctrl_once(shell: bool) -> None:
    async def scenario() -> None:
        async with connected(shell) as (session, ports):
            session.toggle_modifier("ctrl")
            for data in (b"c", b"d"):
                assert (
                    await session.handle_view_input(protocol.ORIGIN_TEXT, data, native_text=True)
                    == ROUTE_PTY
                )
            assert inputs(ports) == [b"\x03", b"d"]

    asyncio.run(scenario())


@pytest.mark.parametrize("shell", [False, True])
@pytest.mark.parametrize("tier", ["compact", "expanded"])
def test_actual_native_channel_routes_bulk_text_through_review(shell: bool, tier: str) -> None:
    async def scenario() -> None:
        async with connected(shell) as (session, ports):
            mount = mount_shell_screen if shell else mount_agent_screen
            options = {"on_back": lambda: None, "on_new_terminal": lambda: None} if shell else {}
            app = await render_component(
                mount, session, phone=tier != "expanded", width_tier=tier, **options
            )
            channel = _ProtocolMuxedDataChannel(79, app.conn)
            app.conn.data_channel_for = lambda number: channel
            await app.set_props_and_fire(
                "terminal-surface",
                {},
                "data_channel_open",
                {"channel_name": "terminal", "channel_id": 79},
            )
            channel._deliver(protocol.encode_input(b"one\ntwo", protocol.ORIGIN_TEXT))
            await app.settle()
            assert session.pending_review is not None, inputs(ports)
            assert session.sessions.paste.pending.text == "one\ntwo"
            assert inputs(ports) == []
            assert await session.confirm_paste()
            assert inputs(ports) == [b"one\ntwo"]
            session.controller.mode = "view"
            channel._deliver(protocol.encode_input(b"blocked\ntext", protocol.ORIGIN_TEXT))
            await app.settle()
            assert session.pending_review is None and inputs(ports) == [b"one\ntwo"]

    asyncio.run(scenario())
