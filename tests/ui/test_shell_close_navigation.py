"""a confirmed "Close terminal" sends one session.close and returns to the terminal
list; a failed close keeps the screen. Scripted ops; no network.
"""

from __future__ import annotations

from typing import Any

from relaydeck.client.adapters.fake import FakeClipboard
from relaydeck.client.features.shell import SHELL_EXITED, SHELL_LIVE, ScriptedShellGatewayOps
from relaydeck.client.features.shell.ops import ShellOpError
from relaydeck.client.features.shell.screen import (
    SHELL_CLOSE_CONFIRM_BUTTON_KEY,
    close_and_leave,
    mount_shell_screen,
)
from relaydeck.client.features.shell.session import ShellSession
from tests.support.host_harness import render_component, run
from tests.support.terminal.sessions import _SocketFactory


def _shell(ops: ScriptedShellGatewayOps) -> ShellSession:
    return ShellSession(
        ops=ops,
        host_id="home-mac",
        device_id=ops.device_id,
        socket_factory=_SocketFactory(),
        clipboard=FakeClipboard(),
        host_epoch="epoch-7",
    )


def test_confirmed_close_sends_one_command_and_returns_to_the_list() -> None:
    async def scenario() -> None:
        ops = ScriptedShellGatewayOps()
        session = _shell(ops)
        await session.open("term-1")
        navigated: list[int] = []
        phase = await close_and_leave(session, lambda: navigated.append(1))
        assert phase == SHELL_EXITED
        assert [c["kind"] for c in ops.close_commands] == ["session.close"]
        assert navigated == [1]

    run(scenario())


def test_failed_close_keeps_the_screen_and_never_navigates() -> None:
    async def scenario() -> None:
        ops = ScriptedShellGatewayOps()

        async def down(command: dict[str, Any]) -> dict[str, Any]:
            raise ShellOpError("host_offline", "down")

        ops.close_terminal = down  # type: ignore[method-assign]
        session = _shell(ops)
        await session.open("term-1")
        navigated: list[int] = []
        phase = await close_and_leave(session, lambda: navigated.append(1))
        assert phase == SHELL_LIVE
        assert navigated == []

    run(scenario())


def test_confirm_button_in_the_mounted_screen_closes_and_returns_to_the_list() -> None:
    """The real confirm button calls its action without an event argument (device regression)."""

    async def scenario() -> None:
        ops = ScriptedShellGatewayOps()
        session = _shell(ops)
        await session.open("term-1")
        navigated: list[int] = []
        app = await render_component(
            mount_shell_screen, session, lambda: navigated.append(1), lambda: None
        )
        session.request_close()
        await app.settle()
        await app.set_props_and_fire(SHELL_CLOSE_CONFIRM_BUTTON_KEY, {}, "click")
        await app.settle()
        assert [c["kind"] for c in ops.close_commands] == ["session.close"]
        assert session.phase == SHELL_EXITED
        assert navigated == [1]

    run(scenario())
