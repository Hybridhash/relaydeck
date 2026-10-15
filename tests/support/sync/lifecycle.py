"""Lifecycle agent and shell builders."""

from __future__ import annotations

import asyncio
from collections import deque
from typing import Any

from relaydeck.client.adapters.fake import FakeClipboard
from relaydeck.client.adapters.ports import CredentialStore
from relaydeck.client.features.shell.ops import ScriptedShellGatewayOps
from relaydeck.client.features.shell.session import ShellSession
from relaydeck.client.features.sync.transport import TerminalSocket, TransportError
from relaydeck.client.features.terminal.agent_session import AgentSession


class _Socket(TerminalSocket):
    def __init__(self, script: deque[tuple[str, Any]] | None = None) -> None:
        self._script = script if script is not None else deque()
        self.sent_bytes: list[bytes] = []
        self.sent_text: list[str] = []
        self.connected = False
        self.closed = False
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
        self.sockets: list[_Socket] = []
        self._scripts: deque[deque[tuple[str, Any]]] = deque()

    def queue(self, *frames: tuple[str, Any]) -> None:
        self._scripts.append(deque(frames))

    def __call__(self, ticket: str) -> TerminalSocket:
        _ = ticket
        script = self._scripts.popleft() if self._scripts else deque()
        sock = _Socket(script)
        self.sockets.append(sock)
        return sock


def _shell() -> tuple[ShellSession, ScriptedShellGatewayOps, _SocketFactory]:
    ops = ScriptedShellGatewayOps()
    factory = _SocketFactory()
    session = ShellSession(
        ops=ops,
        host_id="home-mac",
        device_id=ops.device_id,
        socket_factory=factory,
        clipboard=FakeClipboard(),
    )
    return session, ops, factory


def _agent(
    initial_status: str = "idle",
    draft_store: CredentialStore | None = None,
) -> tuple[AgentSession, ScriptedShellGatewayOps, _SocketFactory, list[dict[str, Any]]]:
    ops = ScriptedShellGatewayOps()
    factory = _SocketFactory()
    submitted: list[dict[str, Any]] = []

    async def submit(command: dict[str, Any]) -> dict[str, Any]:
        submitted.append(command)
        return {"status": "accepted"}

    async def prompt(target: str, text: str) -> dict[str, Any]:
        _ = (target, text)
        raise AssertionError("prompt must not run on the blocked path")

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
        initial_status=initial_status,
        clipboard=FakeClipboard(),
        draft_store=draft_store,
    )
    return session, ops, factory, submitted
