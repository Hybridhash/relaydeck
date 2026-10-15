"""Renderer binding regressions.

Real Flet dispatch + the mounted stable terminal instance: byte
transport must reach the mounted renderer, input mode must follow the
workflow (Composer display-only vs Direct Input), selection close must
clear the engine, reset must reset the emulator, and unmount must
invalidate stale callbacks. Failing before the fix: nothing connects
`deliver_output` to the mounted renderer and `input_enabled` is False
forever.
"""

from __future__ import annotations

import asyncio
from collections import deque

from relaydeck.client.adapters.fake import FakeClipboard
from relaydeck.client.features.shell import ScriptedShellGatewayOps, ShellSession
from relaydeck.client.features.shell.screen import SHELL_MODE_KEYS_KEY, mount_shell_screen
from relaydeck.client.features.terminal.agent_session import AGENT_LIVE
from relaydeck.client.features.terminal.keys import KEYS_DOCK_KEY
from relaydeck.client.features.terminal.screen import (
    SELECT_BUTTON_KEY,
    STATUS_KEY,
    SURFACE_KEY,
)
from relaydeck.client.features.terminal.selection_sheet import SELECT_CLOSE_KEY
from relaydeck.client.features.terminal.surfaces import (
    AGENT_KEYBOARD_KEYS_KEY,
    AGENT_WORKFLOW_DIRECT_KEY,
    mount_agent_screen,
)
from tests.support.host_harness import render_component
from tests.support.terminal.binding import _cancel, _control, _output, _wait_for
from tests.support.terminal.dispatch import _agent_session, _SocketFactory, run_clean


def test_mounted_renderer_receives_live_bytes() -> None:
    async def scenario() -> None:
        session, factory, _ = _agent_session()
        factory.scripts.append(deque([_output("e1", 0, b"hello-agent")]))
        session.open()
        assert await session.confirm_takeover() == AGENT_LIVE
        app = await render_component(mount_agent_screen, session)
        term = app.find(SURFACE_KEY)
        assert term._sent_bytes == 0
        task = asyncio.create_task(session.run())
        try:
            await _wait_for(lambda: term._sent_bytes == len(b"hello-agent"))
            await app.settle()
            assert "live" in app.find(STATUS_KEY).value
        finally:
            await _cancel(task)

    run_clean(scenario())


def test_input_enabled_follows_workflow() -> None:
    async def scenario() -> None:
        session, _, _ = _agent_session()
        session.open()
        assert await session.confirm_takeover() == AGENT_LIVE
        app = await render_component(mount_agent_screen, session)
        # Composer: display-only, typing stays local.
        assert app.find(SURFACE_KEY).input_enabled is False
        await app.set_props_and_fire(AGENT_WORKFLOW_DIRECT_KEY, {}, "click")
        assert app.find(SURFACE_KEY).input_enabled is True
        # Tools keys are explicit terminal tools over the Python sender;
        # the emulator gate stays open in Direct mode either way.
        await app.set_props_and_fire(AGENT_KEYBOARD_KEYS_KEY, {}, "click")
        assert app.find(KEYS_DOCK_KEY) is not None
        assert app.find(SURFACE_KEY).input_enabled is True

    run_clean(scenario())


def test_selection_close_clears_engine_selection() -> None:
    async def scenario() -> None:
        session, _, _ = _agent_session()
        session.open()
        assert await session.confirm_takeover() == AGENT_LIVE
        app = await render_component(mount_agent_screen, session)
        term = app.find(SURFACE_KEY)
        sent: list[bytes] = []
        term._test_mark_ready(sent)
        await app.set_props_and_fire(SELECT_BUTTON_KEY, {}, "click")
        assert session.term_state.selection.open is True
        await app.set_props_and_fire(SELECT_CLOSE_KEY, {}, "click")
        assert session.term_state.selection.open is False
        assert b"\x07" in sent  # CLEAR_SELECTION reached the engine

    run_clean(scenario())


def test_reset_status_resets_stable_emulator() -> None:
    async def scenario() -> None:
        session, factory, _ = _agent_session()
        factory.scripts.append(
            deque(
                [
                    _output("e1", 0, b"stale-screen"),
                    _control("reset", "epoch rotated"),
                ]
            )
        )
        session.open()
        assert await session.confirm_takeover() == AGENT_LIVE
        app = await render_component(mount_agent_screen, session)
        term = app.find(SURFACE_KEY)
        sent: list[bytes] = []
        term._test_mark_ready(sent)
        task = asyncio.create_task(session.run())
        try:
            await _wait_for(lambda: session.term_state.status == "reset")
            await app.settle()
            assert b"\x08" in sent  # RESET reached the stable instance
        finally:
            await _cancel(task)

    run_clean(scenario())


def test_unmount_invalidates_stale_input_callbacks() -> None:
    async def scenario() -> None:
        session, factory, _ = _agent_session()
        session.open()
        assert await session.confirm_takeover() == AGENT_LIVE
        app = await render_component(mount_agent_screen, session)
        assert app.find(SURFACE_KEY) is not None
        token = session.input_token
        assert token is not None
        session.unbind_view()
        # Stale generation/token writes fail closed: nothing is sent.
        assert await session.handle_view_input(1, b"stale", token=token) == "dropped"
        assert await session.press_key("a") is False
        assert await session.type_text("b") == "dropped"
        assert factory.sockets[0].sent_bytes == []
        # Remount rebinds the live writer; new input flows again.
        session.bind_view(app.find(SURFACE_KEY))
        session.set_workflow("agent-direct")
        assert await session.type_text("c") == "pty"
        assert len(factory.sockets[0].sent_bytes) == 1

    run_clean(scenario())


def test_shell_mount_binds_bytes_and_enables_input() -> None:
    async def scenario() -> None:
        ops = ScriptedShellGatewayOps()
        factory = _SocketFactory()
        factory.scripts.append(deque([_output("e1", 0, b"shell-bytes")]))
        session = ShellSession(
            ops=ops,
            host_id="home-mac",
            device_id=ops.device_id,
            socket_factory=factory,
            clipboard=FakeClipboard(),
        )
        assert await session.open("term-1") == "live"
        nav: list[str] = []
        app = await render_component(
            mount_shell_screen,
            session,
            lambda: nav.append("back"),
            lambda: nav.append("new"),
        )
        term = app.find(SURFACE_KEY)
        # Shells are always direct: the renderer accepts input.
        assert term.input_enabled is True
        # No agent UI on the shell surface.
        try:
            app.find("composer-box")
            raise AssertionError("shell must not mount a composer")
        except KeyError:
            pass
        task = asyncio.create_task(session.run())
        try:
            await _wait_for(lambda: term._sent_bytes == len(b"shell-bytes"))
        finally:
            await _cancel(task)
        # Keys mode renders the dock through real dispatch.
        await app.set_props_and_fire(SHELL_MODE_KEYS_KEY, {}, "click")
        assert app.find(KEYS_DOCK_KEY) is not None

    run_clean(scenario())
