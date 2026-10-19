"""Off-loop durable commands, with loop-owned outbox and lease effects."""

from __future__ import annotations

import asyncio
from typing import Any

from relaydeck.gateway.backend import Backend
from relaydeck.gateway.production import JournalBackend
from relaydeck.offload import settled


async def command_io(backend: Backend, operation: str, *args: Any) -> Any:
    """The in-memory adapter stays synchronous; durable work uses one ordered lane."""
    if not isinstance(backend, JournalBackend):
        return getattr(backend, operation)(*args)
    # Finish loop-side effects even if the HTTP/tunnel waiter disconnects after commit.
    return await settled(asyncio.create_task(_ordered(backend, operation, args)))


async def _ordered(backend: JournalBackend, operation: str, args: tuple[Any, ...]) -> Any:
    async with backend.command_lock.hold():
        journal = backend.journal
        if operation == "submit_command":
            return await _submit(backend, args[0])
        if operation == "drain_outbox":
            await journal.run(journal.expire_sweep, backend.now_ms())
            return backend.take_outbox(args[0], reserve=True)
        if operation == "mark_command_dispatched":
            # Read the deadline clock after queueing, immediately before the durable marker.
            return await journal.run(
                lambda: backend.mark_command_dispatched(args[0], backend.now_ms())
            )
        if operation not in {
            "command_status",
            "command_owner",
            "note_command_ack",
            "note_command_result",
        }:
            raise ValueError("unsupported command operation")
        # These methods touch only the thread-safe journal, never leases/rings/outbox.
        return await journal.run(getattr(backend, operation), *args)


async def _submit(backend: JournalBackend, command: dict[str, Any]) -> dict[str, Any]:
    journal = backend.journal
    at = backend.now_ms()
    await journal.run(journal.expire_sweep, at)
    existing = await journal.run(journal.record, str(command["command_id"]))
    backend.prepare_command(command, existing)
    record, current = await journal.run(backend.persist_command, command, at)
    return backend.accept_command(record, current)
