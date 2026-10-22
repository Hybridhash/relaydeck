"""Agent session, socket factory and clean-run helpers."""

from __future__ import annotations

import asyncio
import gc
import warnings
from collections import deque
from collections.abc import Coroutine
from typing import Any

from relaydeck.client.features.shell import ScriptedShellGatewayOps
from relaydeck.client.features.sync.transport import TerminalSocket, TransportError
from relaydeck.client.features.terminal.agent_session import AgentSession
from tests.support.fakes import FakeClipboard
from tests.support.host_harness import run


def run_clean(coro: Coroutine[Any, Any, Any]) -> Any:
    """Run a scenario and fail on any unawaited-coroutine warning.

    Covers "No 'coroutine was never awaited' warnings": every
    dispatched async callback must actually be awaited by the handler.
    """
    with warnings.catch_warnings(record=True) as records:
        warnings.simplefilter("always")
        result = run(coro)
        gc.collect()
    bad = [w for w in records if "never awaited" in str(w.message)]
    assert not bad, f"unawaited coroutines: {[str(w.message) for w in bad]}"
    return result


class _ScriptedSocket(TerminalSocket):
    def __init__(self, script: deque[tuple[str, Any]] | None = None) -> None:
        self._script = script if script is not None else deque()
        self.sent_bytes: list[bytes] = []
        self.sent_text: list[str] = []
        self._wake = asyncio.Event()

    async def connect(self) -> None:
        pass

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
        self._wake.set()


class _SocketFactory:
    def __init__(self) -> None:
        self.sockets: list[_ScriptedSocket] = []
        self.scripts: deque[deque[tuple[str, Any]]] = deque()

    def queue(self, *frames: tuple[str, Any]) -> None:
        self.scripts.append(deque(frames))

    def __call__(self, ticket: str) -> TerminalSocket:
        script = self.scripts.popleft() if self.scripts else deque()
        sock = _ScriptedSocket(script)
        self.sockets.append(sock)
        return sock


def _agent_session() -> tuple[AgentSession, _SocketFactory, list[dict[str, Any]]]:
    ops = ScriptedShellGatewayOps()
    factory = _SocketFactory()
    submitted: list[dict[str, Any]] = []

    async def submit(command: dict[str, Any]) -> dict[str, Any]:
        submitted.append(command)
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
        clipboard=FakeClipboard(),
    )
    return session, factory, submitted
