"""Mobile latch and native committed text share one guarded modifier owner."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from flet.data_channel import _ProtocolMuxedDataChannel
from relaydeck_terminal import protocol

from relaydeck.client.features.shell.screen import mount_shell_screen
from relaydeck.client.features.terminal.surfaces import mount_agent_screen
from tests.support.host_harness import render_component, run
from tests.support.terminal.latch_text import _inputs
from tests.support.terminal.sessions import _agent_session, _session


async def _mobile(shell: bool) -> tuple[Any, Any, Any, Any]:
    if shell:
        session, _ops, factory = _session()
        await session.open("term-1")
        app = await render_component(
            mount_shell_screen,
            session,
            phone=True,
            on_back=lambda: None,
            on_new_terminal=lambda: None,
        )
    else:
        session, _ops, factory, _submitted = _agent_session()
        session.open()
        await session.confirm_takeover()
        app = await render_component(mount_agent_screen, session, phone=True)
        await app.set_props_and_fire("agent-workflow-toggle", {}, "click")
    channel = _ProtocolMuxedDataChannel(79, app.conn)
    app.conn.data_channel_for = lambda number: channel
    await app.set_props_and_fire(
        "terminal-surface", {}, "data_channel_open", {"channel_name": "terminal", "channel_id": 79}
    )
    return app, session, factory.sockets[0], channel


@pytest.mark.parametrize("shell", [False, True])
def test_locked_ctrl_rearms_between_native_commits_even_while_sender_waits(shell: bool) -> None:
    async def scenario() -> None:
        app, session, socket, channel = await _mobile(shell)
        release = asyncio.Event()
        started = asyncio.Event()
        try:
            ctrl = app.find("mod-ctrl")
            await app.session.dispatch_event(ctrl._i, "click", "")
            await app.session.dispatch_event(ctrl._i, "click", "")
            await app.settle()
            assert app.find("mod-ctrl").data["latch"] == "locked"
            keys = session.sessions.keys
            send = keys._sender

            async def delayed(data: bytes) -> None:
                if data == b"\x03":
                    started.set()
                    await release.wait()
                await send(data)

            keys._sender = delayed
            channel._deliver(protocol.encode_input(b"c", protocol.ORIGIN_TEXT))
            channel._deliver(protocol.encode_input(b"d", protocol.ORIGIN_TEXT))
            await asyncio.wait_for(started.wait(), 1)
            release.set()
            await app.settle()
            assert _inputs(socket) == [b"\x03", b"\x04"]
            assert app.find("mod-ctrl").data["latch"] == "locked"
            assert keys.pending_modifiers == frozenset({"ctrl"})
            await app.set_props_and_fire("key-ctrl-l", {}, "click")
            assert _inputs(socket) == [b"\x03", b"\x04", b"\x0c"]
            assert keys.pending_modifiers == frozenset({"ctrl"})
        finally:
            release.set()
            await session.dispose()

    run(scenario())


@pytest.mark.parametrize("shell", [False, True])
def test_native_commit_consumes_one_shot_before_next_chip(shell: bool) -> None:
    async def scenario() -> None:
        app, session, socket, channel = await _mobile(shell)
        try:
            await app.set_props_and_fire("mod-ctrl", {}, "click")
            channel._deliver(protocol.encode_input(b"c", protocol.ORIGIN_TEXT))
            await app.settle()
            assert _inputs(socket) == [b"\x03"]
            assert app.find("mod-ctrl").data["latch"] == "off"
            assert session.sessions.keys.pending_modifiers == frozenset()
            await app.set_props_and_fire("mod-alt", {}, "click")
            channel._deliver(protocol.encode_input(b"c", protocol.ORIGIN_TEXT))
            await app.settle()
            assert app.find("mod-alt").data["latch"] == "off"
            await app.set_props_and_fire("key-slash", {}, "click")
            assert _inputs(socket) == [b"\x03", b"\x1bc", b"/"]
            assert session.sessions.keys.pending_modifiers == frozenset()
        finally:
            await session.dispose()

    run(scenario())


@pytest.mark.parametrize("shell", [False, True])
def test_ime_fallthrough_and_encoded_packets_preserve_pending_modifier(shell: bool) -> None:
    async def scenario() -> None:
        app, session, socket, channel = await _mobile(shell)
        try:
            await app.set_props_and_fire("mod-ctrl", {}, "click")
            for text in ("", "漢", "你好"):
                channel._deliver(protocol.encode_input(text.encode(), protocol.ORIGIN_TEXT))
                await app.settle()
                assert app.find("mod-ctrl").data["latch"] == "once"
                assert session.sessions.keys.pending_modifiers == frozenset({"ctrl"})
            assert _inputs(socket) == ["漢".encode(), "你好".encode()]
            channel._deliver(protocol.encode_input(b"c", protocol.ORIGIN_TEXT))
            await app.settle()
            assert _inputs(socket)[-1] == b"\x03"
            assert app.find("mod-ctrl").data["latch"] == "off"
            await app.set_props_and_fire("mod-alt", {}, "click")
            raw = b"\x1b[1;5A"
            channel._deliver(protocol.encode_input(raw, protocol.ORIGIN_KEY))
            channel._deliver(protocol.encode_input(b"ignored", protocol.ORIGIN_MOUSE))
            await app.settle()
            assert _inputs(socket) == ["漢".encode(), "你好".encode(), b"\x03", raw]
            assert app.find("mod-alt").data["latch"] == "once"
            assert session.sessions.keys.pending_modifiers == frozenset({"alt"})
            channel._deliver(protocol.encode_input(b"c", protocol.ORIGIN_TEXT))
            await app.settle()
            await app.set_props_and_fire("key-slash", {}, "click")
            assert _inputs(socket)[-2:] == [b"\x1bc", b"/"]
        finally:
            await session.dispose()

    run(scenario())


@pytest.mark.parametrize("shell", [False, True])
def test_blocked_text_cannot_consume_latch_or_send(shell: bool) -> None:
    async def scenario() -> None:
        app, session, socket, channel = await _mobile(shell)
        try:
            await app.set_props_and_fire("mod-ctrl", {}, "click")
            session.open_selection()
            await app.settle()
            channel._deliver(protocol.encode_input(b"c", protocol.ORIGIN_TEXT))
            await app.settle()
            assert _inputs(socket) == [] and app.find("mod-ctrl").data["latch"] == "once"
            session.close_selection()
            mode = session.controller.mode
            session.controller.mode = "view"
            channel._deliver(protocol.encode_input(b"c", protocol.ORIGIN_TEXT))
            await app.settle()
            assert _inputs(socket) == [] and app.find("mod-ctrl").data["latch"] == "once"
            assert session.sessions.keys.pending_modifiers == frozenset({"ctrl"})
            session.controller.mode = mode
            channel._deliver(protocol.encode_input(b"c", protocol.ORIGIN_TEXT))
            await app.settle()
            assert _inputs(socket) == [b"\x03"]
            assert app.find("mod-ctrl").data["latch"] == "off"
        finally:
            await session.dispose()

    run(scenario())


@pytest.mark.parametrize("shell", [False, True])
def test_queued_old_commit_cannot_rearm_locked_modifier_on_new_writer(shell: bool) -> None:
    async def scenario() -> None:
        app, session, socket, channel = await _mobile(shell)
        release = asyncio.Event()
        started = asyncio.Event()
        try:
            ctrl = app.find("mod-ctrl")
            await app.session.dispatch_event(ctrl._i, "click", "")
            await app.session.dispatch_event(ctrl._i, "click", "")
            await app.settle()
            old_token = session.input_token
            old_keys = session.sessions.keys
            send = old_keys._sender

            async def delayed(data: bytes) -> None:
                started.set()
                await release.wait()
                await send(data)

            old_keys._sender = delayed
            channel._deliver(protocol.encode_input(b"c", protocol.ORIGIN_TEXT))
            channel._deliver(protocol.encode_input(b"d", protocol.ORIGIN_TEXT))
            await asyncio.wait_for(started.wait(), 1)
            session.set_foreground(False)
            session.set_foreground(True)
            assert session.input_token != old_token
            release.set()
            await app.settle()
            # The already accepted first write finishes; the queued old commit is dropped.
            assert _inputs(socket) == [b"\x03"]
            assert app.find("mod-ctrl").data["latch"] == "off"
            assert session.sessions.keys.pending_modifiers == frozenset()
            channel._deliver(protocol.encode_input(b"d", protocol.ORIGIN_TEXT))
            await app.settle()
            assert _inputs(socket) == [b"\x03", b"d"]
        finally:
            release.set()
            await session.dispose()

    run(scenario())


def test_composer_native_text_stays_local_and_does_not_consume_latch() -> None:
    async def scenario() -> None:
        app, session, socket, channel = await _mobile(False)
        try:
            session.on_draft_change("kept draft ")
            await app.set_props_and_fire("mod-ctrl", {}, "click")
            await app.set_props_and_fire("agent-compose-toggle", {}, "click")
            channel._deliver(protocol.encode_input("漢".encode(), protocol.ORIGIN_TEXT))
            await app.settle()
            assert session.store.draft == "kept draft 漢" and _inputs(socket) == []
            assert app.find("mod-ctrl").data["latch"] == "once"
            assert session.sessions.keys.pending_modifiers == frozenset({"ctrl"})
            await app.set_props_and_fire("agent-workflow-toggle", {}, "click")
            channel._deliver(protocol.encode_input(b"c", protocol.ORIGIN_TEXT))
            await app.settle()
            assert _inputs(socket) == [b"\x03"]
            assert session.store.draft == "kept draft 漢"
        finally:
            await session.dispose()

    run(scenario())
