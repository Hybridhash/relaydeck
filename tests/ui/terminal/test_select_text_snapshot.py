"""Select Text engine-snapshot regressions.

Real Flet dispatch through both mounts: opening Select Text fetches a
snapshot from the ACTUAL emulator engine (cursor overwrites applied,
ANSI consumed) and freezes it for the sheet lifetime; dragging in the
sheet updates the CURRENT anchored range, so Copy copies what the
user selected — never the raw output log, never the stale open range.
"""

from __future__ import annotations

import asyncio
import struct
from collections import deque
from typing import Any

from relaydeck_terminal import protocol

from relaydeck.client.features.shell import ScriptedShellGatewayOps, ShellSession
from relaydeck.client.features.shell.screen import mount_shell_screen
from relaydeck.client.features.terminal.agent_session import AGENT_LIVE, AgentSession
from relaydeck.client.features.terminal.screen import SELECT_BUTTON_KEY
from relaydeck.client.features.terminal.selection_sheet import (
    SELECT_AREA_KEY,
    SELECT_CLOSE_KEY,
    SELECT_COPY_KEY,
    SELECT_ERROR_KEY,
    SELECT_FIELD_KEY,
    SELECT_RANGE_KEY,
)
from relaydeck.client.features.terminal.surfaces import mount_agent_screen
from tests.support.fakes import FakeClipboard
from tests.support.host_harness import render_component, run
from tests.support.terminal.binding import _cancel, _control, _output, _wait_for
from tests.support.terminal.dispatch import _SocketFactory
from tests.support.terminal.selection import _mark_ready, _open_with_snapshot, _snapshot_requests

RAW = b"\x1b[31mred\x1b[0m\rX"  # engine truth after SGR + CR overwrite: "Xed"
TRUTH = "Xed"


def _agent_session_with_clipboard() -> tuple[AgentSession, _SocketFactory, FakeClipboard]:
    ops = ScriptedShellGatewayOps()
    factory = _SocketFactory()
    clipboard = FakeClipboard()

    async def submit(command: dict[str, Any]) -> dict[str, Any]:
        return {"command_id": command["command_id"]}

    async def prompt(target: str, text: str) -> dict[str, Any]:
        return {"ok": True, "target": target, "text": text}

    session = AgentSession(
        ops=ops,
        host_id="home-mac",
        device_id=ops.device_id,
        socket_factory=factory,
        submit_command=submit,
        prompt_text=prompt,
        host_epoch="epoch-1",
        agent_id="agent-1",
        target="sess:1",
        initial_status="idle",
        clipboard=clipboard,
    )
    return session, factory, clipboard


async def _live_agent(
    raw: bytes = RAW,
) -> tuple[AgentSession, FakeClipboard, asyncio.Task[Any]]:
    session, factory, clipboard = _agent_session_with_clipboard()
    factory.scripts.append(deque([_output("e1", 0, raw), _control("eof")]))
    session.open()
    assert await session.confirm_takeover() == AGENT_LIVE
    app_task = asyncio.create_task(session.run())
    await _wait_for(lambda: bytes(session.output_log) == raw)
    return session, clipboard, app_task


def test_select_text_shows_engine_snapshot_not_raw_bytes() -> None:
    async def scenario() -> None:
        session, _, runner = await _live_agent()
        try:
            app = await render_component(mount_agent_screen, session)
            term, sent = _mark_ready(session)
            await _open_with_snapshot(app, term, sent, TRUTH)
            field = app.find(SELECT_FIELD_KEY)
            assert field.value == TRUTH
            assert "\x1b" not in field.value
        finally:
            await _cancel(runner)

    run(scenario())


def test_snapshot_frozen_while_selecting() -> None:
    async def scenario() -> None:
        session, _, runner = await _live_agent()
        try:
            app = await render_component(mount_agent_screen, session)
            term, sent = _mark_ready(session)
            await _open_with_snapshot(app, term, sent, TRUTH)
            # More raw output lands and the sheet rerenders (selection
            # drag): the frozen engine text must not change.
            session.output_log.append(b"\x1b[32mlate\x1b[0m")
            await app.set_props_and_fire(
                SELECT_AREA_KEY,
                {},
                "change",
                "X",
            )
            assert app.find(SELECT_FIELD_KEY).value == TRUTH
            # Reopening fetches a FRESH snapshot, not the frozen one.
            await app.set_props_and_fire(SELECT_CLOSE_KEY, {}, "click")
            sent.clear()
            await _open_with_snapshot(app, term, sent, "fresh")
            assert app.find(SELECT_FIELD_KEY).value == "fresh"
        finally:
            await _cancel(runner)

    run(scenario())


def test_copy_uses_current_dragged_range() -> None:
    async def scenario() -> None:
        session, clipboard, runner = await _live_agent()
        try:
            app = await render_component(mount_agent_screen, session)
            term, sent = _mark_ready(session)
            await _open_with_snapshot(app, term, sent, TRUTH)
            assert app.find(SELECT_RANGE_KEY).value == "All text (3 chars)"
            await app.set_props_and_fire(
                SELECT_AREA_KEY,
                {},
                "change",
                "X",
            )
            assert app.find(SELECT_RANGE_KEY).value == "Chars 0-1 of 3 (1 lines)"
            await app.set_props_and_fire(SELECT_COPY_KEY, {}, "click")
            assert clipboard.writes == ["X"]
            # Change the selection again before the next Copy.
            await app.set_props_and_fire(
                SELECT_AREA_KEY,
                {},
                "change",
                "ed",
            )
            await app.set_props_and_fire(SELECT_COPY_KEY, {}, "click")
            assert clipboard.writes == ["X", "ed"]
        finally:
            await _cancel(runner)

    run(scenario())


def test_snapshot_failure_shows_error_with_raw_fallback() -> None:
    async def scenario() -> None:
        session, _, runner = await _live_agent()
        try:
            app = await render_component(mount_agent_screen, session)
            # Channel never attached: honest error + raw fallback, no hang.
            await app.set_props_and_fire(SELECT_BUTTON_KEY, {}, "click")
            assert app.find(SELECT_FIELD_KEY).value == RAW.decode("utf-8", errors="replace")
            assert app.find(SELECT_ERROR_KEY).value.startswith("Live snapshot unavailable")
        finally:
            await _cancel(runner)

    run(scenario())


def test_broken_renderer_snapshot_reports_error() -> None:
    async def scenario() -> None:
        session, _, runner = await _live_agent()
        try:
            app = await render_component(mount_agent_screen, session)

            class _Broken:
                def write(self, data: bytes) -> None:
                    pass

                def clear_selection(self) -> None:
                    pass

                async def get_snapshot_text(self, timeout: float = 5.0) -> str:
                    raise TimeoutError("dart too slow")

            session.bind_view(_Broken(), write=lambda term, data: True)
            # Dispatch the replacement viewer's callbacks after the declared
            # subscription rerender, rather than invoking the stale binding.
            await app.settle()
            await app.set_props_and_fire(SELECT_BUTTON_KEY, {}, "click")
            assert app.find(SELECT_FIELD_KEY).value == RAW.decode("utf-8", errors="replace")
            error = app.find(SELECT_ERROR_KEY).value
            assert error.startswith("Live snapshot unavailable")
            assert "dart too slow" in error
        finally:
            await _cancel(runner)

    run(scenario())


def test_shell_select_text_snapshot_and_current_copy() -> None:
    async def scenario() -> None:
        ops = ScriptedShellGatewayOps()
        factory = _SocketFactory()
        factory.scripts.append(deque([_output("e1", 0, RAW), _control("eof")]))
        clipboard = FakeClipboard()
        session = ShellSession(
            ops=ops,
            host_id="home-mac",
            device_id=ops.device_id,
            socket_factory=factory,
            clipboard=clipboard,
        )
        assert await session.open("term-1") == "live"
        runner = asyncio.create_task(session.run())
        try:
            await _wait_for(lambda: bytes(session.output_log) == RAW)
            app = await render_component(mount_shell_screen, session, lambda: None, lambda: None)
            term = session.current_term
            assert term is not None
            sent: list[bytes] = []
            term._test_mark_ready(sent)
            opener = asyncio.create_task(app.set_props_and_fire(SELECT_BUTTON_KEY, {}, "click"))
            try:
                await _wait_for(lambda: len(_snapshot_requests(sent)) == 1)
                (req_id,) = struct.unpack_from(">I", _snapshot_requests(sent)[0], 1)
                term._test_inject_dart_message(protocol.encode_snapshot_chunk(req_id, 3, 0, b"Xed"))
                await opener
            finally:
                if not opener.done():
                    opener.cancel()
            assert app.find(SELECT_FIELD_KEY).value == "Xed"
            await app.set_props_and_fire(
                SELECT_AREA_KEY,
                {},
                "change",
                "ed",
            )
            await app.set_props_and_fire(SELECT_COPY_KEY, {}, "click")
            assert clipboard.writes == ["ed"]
        finally:
            await _cancel(runner)

    run(scenario())
