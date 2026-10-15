"""Recording sources, inventory states and polling for sync transport tests."""

from __future__ import annotations

import asyncio
import json
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any

from relaydeck.connector.attach import AttachSession
from relaydeck.connector.herdr import HerdrAdapter
from relaydeck.connector.source import HerdrSource
from relaydeck.gateway.app import GatewayState
from relaydeck.gateway.production import JournalBackend
from relaydeck.journal import CommandJournal
from tests.security.helpers import NOW, SECRET
from tests.support.paths import TESTS_DIR

UPSTREAM = TESTS_DIR / "fixtures" / "upstream"


class _OutputQueue(deque[bytes]):
    def __init__(self, wake: Callable[[], None]) -> None:
        super().__init__()
        self.wake = wake

    def append(self, data: bytes) -> None:
        super().append(data)
        self.wake()


class RecordingPty(AttachSession):
    def __init__(self, wake: Callable[[], None] = lambda: None) -> None:
        super().__init__(argv=[])
        self._active = True
        self._wake = wake
        self.writes: list[bytes] = []
        self.sizes: list[tuple[int, int]] = []
        self.output = _OutputQueue(wake)
        self.fail_write = False
        self.read_only = False

    @property
    def active(self) -> bool:
        return self._active

    @active.setter
    def active(self, value: bool) -> None:
        self._active = value
        self._wake()  # OS fd readiness also wakes on a real PTY's EOF.

    @property
    def running(self) -> bool:
        return self.active

    async def write_async(self, data: bytes) -> int:
        if self.read_only:
            raise OSError("synthetic observer cannot write")
        self.writes.append(data)
        if self.fail_write:
            raise OSError("synthetic partial-write failure")
        return len(data)

    def resize(self, cols: int, rows: int) -> None:
        if self.read_only:
            raise OSError("synthetic observer cannot resize")
        self.sizes.append((cols, rows))

    def read(self, max_bytes: int = 65536, timeout: float = 5.0) -> bytes:
        return self.output.popleft() if self.output else b""

    def close(self, timeout: float = 5.0) -> int | None:
        self.active = False
        return 0


class RecordingSource(HerdrSource):
    def __init__(self) -> None:
        super().__init__(HerdrAdapter(), clock=lambda: NOW, boot_tag="c2")
        self._snapshot = json.loads((UPSTREAM / "session_snapshot.json").read_text())["result"][
            "snapshot"
        ]
        self._agents = json.loads((UPSTREAM / "agent_list.json").read_text())["result"]["agents"]
        self.created: list[RecordingPty] = []

    def _attach(self, terminal_id: str, kind: str) -> RecordingPty:
        def factory() -> RecordingPty:
            pty = RecordingPty(self._output_changed.set)
            self.created.append(pty)
            return pty

        held = self._pool.acquire(f"{kind}:{terminal_id}", factory, NOW)
        assert isinstance(held, RecordingPty)
        self._attached[f"{kind}:{terminal_id}"] = (held, terminal_id, "")
        self._output_changed.set()
        return held

    def attach_shell(self, terminal_id: str, *, cols: int = 80, rows: int = 24) -> AttachSession:
        return self._attach(terminal_id, "shell")

    def attach_agent(self, target: str, *, cols: int = 80, rows: int = 24) -> AttachSession:
        return self._attach(target, "agent")

    def observe_terminal(self, terminal_id: str, kind: str) -> AttachSession:
        # Never spawn the real observer from this synthetic fixture.
        held = self._attach(terminal_id, kind)
        held.read_only = True
        return held

    async def wait_output_ready(self) -> None:
        # The queue wakes instead of an OS fd; production still uses fd readers.
        if any(
            held.running and held.output
            for held, terminal, _ in self._attached.values()
            if self._epoch_for(terminal)
        ):
            return
        await self._output_changed.wait()
        self._output_changed.clear()

    async def collect_live(self, timeout: float) -> list[dict[str, Any]]:
        return []

    async def resync(self) -> None:
        pass


def _state(tmp_path: Path) -> tuple[GatewayState, JournalBackend, RecordingSource]:
    source = RecordingSource()
    backend = JournalBackend(
        CommandJournal(tmp_path / "gw.db", clock=lambda: NOW),
        now_ms=lambda: NOW,
        snapshots={"home-mac": source.snapshot_document("home-mac")},
    )
    return GatewayState(backend=backend, cursor_secret=SECRET, now_ms=lambda: NOW), backend, source


async def _wait(predicate: Any) -> None:
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0.005)
