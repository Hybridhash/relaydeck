"""Wait, cancel and output helpers for terminal binding tests."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from relaydeck.client.features.sync.transport import TransportError
from relaydeck.gateway import terminal_wire


def _output(epoch: str, offset: int, data: bytes) -> tuple[str, Any]:
    return ("bytes", terminal_wire.encode_output(epoch, offset, data))


def _control(kind: str, detail: str = "") -> tuple[str, Any]:
    doc: dict[str, Any] = {"kind": kind}
    if detail:
        doc["detail"] = detail
    return ("text", json.dumps(doc))


async def _cancel(task: asyncio.Task[Any]) -> None:
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, TransportError):
        pass


async def _wait_for(predicate: Any, timeout: float = 5.0) -> None:
    async def check() -> bool:
        result = predicate()
        if asyncio.iscoroutine(result):
            result = await result
        return bool(result)

    deadline = asyncio.get_running_loop().time() + timeout
    while not await check():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("timed out waiting for condition")
        await asyncio.sleep(0.01)
