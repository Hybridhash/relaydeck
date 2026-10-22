"""Scripted SessionFactory for Root-level dispatched tests.

Builds a `SessionFactory` over `ScriptedShellGatewayOps`, scripted
stream sockets, and `FakeClipboard`, exposing every scripted piece for
assertions. No network, no real credentials.
"""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from relaydeck.client.features.shell.ops import ScriptedShellGatewayOps
from relaydeck.client.features.sync.transport import TerminalSocket, TransportError
from relaydeck.client.sessions import SessionFactory
from tests.support.fakes import FakeClipboard


class ScriptedTerminalSocket(TerminalSocket):
    """Single-consumer scripted stream: queued frames, recorded sends,
    close unblocks a parked receive with a transport error (like a real
    socket close surfacing through `WebsocketTerminalSocket`)."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, Any]] = []
        self.connected = False
        self.closed = False
        self._frames: deque[tuple[str, Any]] = deque()
        self._wake = asyncio.Event()

    def queue(self, *frames: tuple[str, Any]) -> None:
        self._frames.extend(frames)
        self._wake.set()

    async def connect(self) -> None:
        self.connected = True

    async def send_bytes(self, data: bytes) -> None:
        if self.closed:
            raise TransportError("host_offline", "socket closed", True)
        self.sent.append(("bytes", data))

    async def send_text(self, data: str) -> None:
        if self.closed:
            raise TransportError("host_offline", "socket closed", True)
        self.sent.append(("text", data))

    async def receive(self) -> tuple[str, Any]:
        while True:
            if self._frames:
                return self._frames.popleft()
            if self.closed:
                raise TransportError("host_offline", "socket closed", True)
            await self._wake.wait()
            self._wake.clear()

    async def close(self) -> None:
        self.closed = True
        self._wake.set()


@dataclass
class ScriptedStreams:
    """`open_stream` fake: records (lease_id, ticket) per socket, and the resume position a
    reopen of the same lease names ("" for a first open)."""

    opened: list[tuple[str, str, ScriptedTerminalSocket]] = field(default_factory=list)
    resumes: list[str] = field(default_factory=list)

    def __call__(self, lease_id: str, ticket: str, resume: str = "") -> TerminalSocket:
        sock = ScriptedTerminalSocket()
        self.opened.append((lease_id, ticket, sock))
        self.resumes.append(resume)
        return sock


@dataclass
class RecordingSessionFactory(SessionFactory):
    """SessionFactory that records every session it builds (Root holds
    sessions in refs; dispatched tests observe them here)."""

    created: list[Any] = field(default_factory=list)

    def make_shell(self, *, host_id: str, host_epoch: str = "") -> Any:
        session = super().make_shell(host_id=host_id, host_epoch=host_epoch)
        self.created.append(session)
        return session

    def make_agent(
        self,
        *,
        host_id: str,
        host_epoch: str,
        agent_id: str,
        target: str,
        initial_status: str = "idle",
        expected_revision: int = 0,
    ) -> Any:
        session = super().make_agent(
            host_id=host_id,
            host_epoch=host_epoch,
            agent_id=agent_id,
            target=target,
            initial_status=initial_status,
            expected_revision=expected_revision,
        )
        self.created.append(session)
        return session


@dataclass
class ScriptedSessions:
    factory: RecordingSessionFactory
    ops: ScriptedShellGatewayOps
    streams: ScriptedStreams
    clipboard: FakeClipboard
    submitted: list[dict[str, Any]]
    prompted: list[tuple[str, str]]
    renewed: list[str]


def make_scripted_sessions(device_id: str = "dev-1") -> ScriptedSessions:
    ops = ScriptedShellGatewayOps(device_id=device_id)
    streams = ScriptedStreams()
    clipboard = FakeClipboard()
    submitted: list[dict[str, Any]] = []
    prompted: list[tuple[str, str]] = []
    renewed: list[str] = []

    async def submit(command: dict[str, Any]) -> dict[str, Any]:
        submitted.append(command)
        return {"command_id": command["command_id"]}

    async def prompt(target: str, text: str) -> dict[str, Any]:
        prompted.append((target, text))
        return {"ok": True}

    async def renew(lease_id: str) -> dict[str, Any]:
        renewed.append(lease_id)
        return {"lease_id": lease_id}

    factory = RecordingSessionFactory(
        ops=ops,
        open_stream=streams,
        submit_command=submit,
        prompt_text=prompt,
        renew_lease=renew,
        clipboard=clipboard,
        device_id=device_id,
    )
    return ScriptedSessions(
        factory=factory,
        ops=ops,
        streams=streams,
        clipboard=clipboard,
        submitted=submitted,
        prompted=prompted,
        renewed=renewed,
    )
