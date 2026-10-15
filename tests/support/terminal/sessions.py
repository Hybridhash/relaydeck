"""Agent and shell session builders and socket factory."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Any

from relaydeck.client.adapters.fake import FakeClipboard
from relaydeck.client.features.shell import ScriptedShellGatewayOps, ShellSession
from relaydeck.client.features.sync.transport import TerminalSocket, TransportError
from relaydeck.client.features.terminal.agent_session import AgentSession


class _ScriptedSocket(TerminalSocket):
    """Scripted stream socket: plays queued frames, then parks until the
    run task is cancelled or the socket closes."""

    def __init__(self, script: deque[tuple[str, Any]] | None = None) -> None:
        self._script = script if script is not None else deque()
        self.sent_bytes: list[bytes] = []
        self.sent_text: list[str] = []
        self.connected = False
        self.closed = False
        self.ticket = ""
        self._wake = asyncio.Event()

    async def connect(self) -> None:
        self.connected = True

    async def send_bytes(self, data: bytes) -> None:
        self.sent_bytes.append(data)

    async def send_text(self, data: str) -> None:
        self.sent_text.append(data)

    async def receive(self) -> tuple[str, Any]:
        if self._script:
            return self._script.popleft()
        await self._wake.wait()
        raise TransportError("host_offline", "socket closed")

    async def close(self) -> None:
        self.closed = True
        self._wake.set()


class _SocketFactory:
    def __init__(self) -> None:
        self.sockets: list[_ScriptedSocket] = []
        self._scripts: deque[deque[tuple[str, Any]]] = deque()

    def queue(self, *frames: tuple[str, Any]) -> None:
        self._scripts.append(deque(frames))

    def __call__(self, ticket: str) -> TerminalSocket:
        script = self._scripts.popleft() if self._scripts else deque()
        sock = _ScriptedSocket(script)
        sock.ticket = ticket
        self.sockets.append(sock)
        return sock


def _session(
    ops: ScriptedShellGatewayOps | None = None,
    factory: _SocketFactory | None = None,
    clipboard: FakeClipboard | None = None,
) -> tuple[ShellSession, ScriptedShellGatewayOps, _SocketFactory]:
    ops = ops or ScriptedShellGatewayOps()
    factory = factory or _SocketFactory()
    session = ShellSession(
        ops=ops,
        host_id="home-mac",
        device_id=ops.device_id,
        socket_factory=factory,
        clipboard=clipboard or FakeClipboard(),
    )
    return session, ops, factory


def _agent_session(
    ops: ScriptedShellGatewayOps | None = None,
    factory: _SocketFactory | None = None,
    clipboard: FakeClipboard | None = None,
    initial_status: str = "idle",
    prompt: Callable[[str, str], Awaitable[dict[str, Any]]] | None = None,
) -> tuple[AgentSession, ScriptedShellGatewayOps, _SocketFactory, list[dict[str, Any]]]:
    ops = ops or ScriptedShellGatewayOps()
    factory = factory or _SocketFactory()
    submitted: list[dict[str, Any]] = []

    async def submit(command: dict[str, Any]) -> dict[str, Any]:
        submitted.append(command)
        return {"command_id": command["command_id"]}

    async def default_prompt(target: str, text: str) -> dict[str, Any]:
        return {"ok": True, "target": target, "text": text}

    session = AgentSession(
        ops=ops,
        host_id="home-mac",
        device_id=ops.device_id,
        socket_factory=factory,
        submit_command=submit,
        prompt_text=prompt or default_prompt,
        host_epoch="epoch-1",
        agent_id="agent-1",
        target="sess:1",
        initial_status=initial_status,
        clipboard=clipboard or FakeClipboard(),
    )
    return session, ops, factory, submitted
