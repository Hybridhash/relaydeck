"""Pause-invalidates-writer gate. Flet-free.

Backgrounding must
invalidate the writer immediately. The rule is enforced by binding
every emit to a monotonically increasing binding id that pause (and
death) retires; resume binds a FRESH id explicitly and can never
restore a retired one.

Production note: the shipped pause path enforces this rule with the
controller pause flag plus session lease release; `WriterGate` is the
isolated gate pattern proving the binding discipline headless. It has
zero production callers and stays unwired (deliberate: no new wiring in
this round).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

#: The exact pause rule. Quoted by tests and the device-suite script.
PAUSE_RULE = (
    "PAUSE invalidates the control writer immediately: pause() retires the "
    "current writer binding, so any emit bound to it fails with "
    "StaleWriterError and no byte is delivered. A paused writer can never "
    "emit again under its old binding. Resume re-acquires explicitly via "
    "acquire(), which binds a FRESH binding id to the current lease "
    "generation; acquire() never restores a retired binding and never "
    "changes the lease generation itself, so resume can never silently steal "
    "control held elsewhere (a taken-over lease fails at ticket mint and "
    "resurfaces as disconnected -- 'Could not stream' -- never as "
    "conflict). Process death drops the sender entirely: "
    "post-death emits fail and unacked input becomes unknown, never "
    "auto-resent."
)


class StaleWriterError(Exception):
    """An emit was attempted on a retired writer binding.

    `reason` is one of `paused` (pause retired the binding), `superseded`
    (a newer binding was acquired), or `dead` (the process died and the
    sender is gone). All three fail closed: no byte is delivered.
    """

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


class WriterGate:
    """Generation-disciplined control-writer gate.

    `acquire()` binds the one current writer (lease generation + sender)
    and returns its binding id. `emit()` delivers only when the caller's
    binding id is still current AND the gate is live. `pause()` retires
    the binding immediately; `drop()` additionally forgets the sender
    (process death). Both make every outstanding binding fail closed.
    """

    def __init__(self) -> None:
        self._binding = 0
        self._live = False
        self._generation: int | None = None
        self._sender: Callable[[bytes], Awaitable[None]] | None = None

    @property
    def is_live(self) -> bool:
        return self._live

    @property
    def binding(self) -> int:
        """Current binding id (0 when nothing was ever acquired)."""
        return self._binding

    @property
    def generation(self) -> int | None:
        """Lease generation of the current binding, if any."""
        return self._generation

    def acquire(
        self,
        generation: int,
        sender: Callable[[bytes], Awaitable[None]],
    ) -> int:
        """Explicitly (re-)acquire the writer after the resume flow.

        Always mints a FRESH binding id: retired bindings stay retired, so
        a paused writer can never emit again and resume cannot silently
        re-enable a stale handle. The generation comes from the current
        lease; this gate never invents one.
        """
        self._binding += 1
        self._live = True
        self._generation = generation
        self._sender = sender
        return self._binding

    def pause(self) -> None:
        """Retire the current binding immediately (pause policy).

        Idempotent: pausing twice retires once. Outstanding emits fail
        with reason `paused`.
        """
        if self._live:
            self._binding += 1
            self._live = False
            self._generation = None

    def drop(self) -> None:
        """Forget everything (process death). Outstanding emits fail with
        reason `dead`; the sender is gone, so no later emit can deliver."""
        self._binding += 1
        self._live = False
        self._generation = None
        self._sender = None

    async def emit(self, binding: int, data: bytes) -> None:
        """Deliver one chunk iff `binding` is the current live binding.

        Raises StaleWriterError otherwise; no byte is delivered on any
        failure path.
        """
        if self._sender is None:
            raise StaleWriterError("dead", "writer dead: process state was dropped")
        if not self._live or binding != self._binding:
            reason = "paused" if not self._live else "superseded"
            raise StaleWriterError(reason, f"stale writer binding {binding}")
        # Check-then-await race (disclosed): a pause() landing during a
        # slow `await self._sender(data)` cannot recall the in-flight
        # chunk -- the binding was live at check time, so those bytes may
        # still deliver. Pause only guarantees post-pause emits fail.
        await self._sender(data)
