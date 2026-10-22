""" "Close terminal" sends the session.close command (Herdr pane.close), not only
the RelayDeck attachment close. Scripted ops and a fake socket; no network.
"""

from __future__ import annotations

from typing import Any

from relaydeck.client.features.shell import (
    SHELL_EXITED,
    SHELL_LIVE,
    ScriptedShellGatewayOps,
    ShellSession,
)
from relaydeck.client.features.shell.ops import ShellOpError
from tests.support.fakes import FakeClipboard
from tests.support.host_harness import run
from tests.support.terminal.sessions import _SocketFactory


def _shell(ops: ScriptedShellGatewayOps) -> tuple[ShellSession, _SocketFactory]:
    factory = _SocketFactory()
    session = ShellSession(
        ops=ops,
        host_id="home-mac",
        device_id=ops.device_id,
        socket_factory=factory,
        clipboard=FakeClipboard(),
        host_epoch="epoch-7",
    )
    return session, factory


def test_confirmed_close_sends_one_session_close_for_the_terminal() -> None:
    async def scenario() -> None:
        ops = ScriptedShellGatewayOps()
        session, _ = _shell(ops)
        await session.open("term-1")
        session.request_close()
        assert await session.close_terminal() == SHELL_EXITED
        commands: list[dict[str, Any]] = ops.close_commands
        assert len(commands) == 1
        command = commands[0]
        assert command["kind"] == "session.close"
        assert command["target"] == {"terminal_id": "term-1"}
        assert command["payload"] == {}
        assert command["host_id"] == "home-mac"
        assert command["host_epoch"] == "epoch-7"
        assert command["command_id"]
        assert ops.is_closed("att-1")

    run(scenario())


def test_close_without_a_live_attachment_still_sends_the_command() -> None:
    async def scenario() -> None:
        ops = ScriptedShellGatewayOps()

        async def refused_attach(*_args: Any, **_kwargs: Any) -> Any:
            raise ShellOpError("host_offline", "down")

        ops.attach = refused_attach  # type: ignore[method-assign]
        session, _ = _shell(ops)
        await session.open("term-9")
        assert session.attachment is None
        assert await session.close_terminal() == SHELL_EXITED
        assert [c["target"] for c in ops.close_commands] == [{"terminal_id": "term-9"}]

    run(scenario())


def test_failed_close_shows_notice_stays_put_and_is_not_retried() -> None:
    async def scenario() -> None:
        ops = ScriptedShellGatewayOps()
        attempts: list[int] = []

        async def down(command: dict[str, Any]) -> dict[str, Any]:
            attempts.append(1)
            raise ShellOpError("host_offline", "down")

        ops.close_terminal = down  # type: ignore[method-assign]
        session, _ = _shell(ops)
        await session.open("term-1")
        assert await session.close_terminal() == SHELL_LIVE
        assert session.notice == "Close failed (host_offline)."
        assert attempts == [1]

    run(scenario())
