"""Deadline-bound control-lease renewal.

One cancellable driver renews each live lease. Terminal or non-retryable
failures invalidate the writer once. Transient failures retry only before
the known expiry; both sleep and network awaits are bounded by that deadline.

Successful responses supply the authoritative expiry. Missing expiry uses
the nominal TTL for compatible transports; malformed or past expiry fails
closed. Stop cancels renewal before pause, disconnect or disposal."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from relaydeck.client.features.sync.transport import TransportError
from relaydeck.lease_diagnostics import LeaseTrace

# `stale_revision` means this lease was superseded: it can never renew again.
TERMINAL_RENEWAL_CODES = frozenset(
    {"lease_expired", "lease_conflict", "forbidden", "unauthenticated", "stale_revision"}
)

DEFAULT_RENEWAL_INTERVAL_S = 30.0

# Nominal re-arm only for responses without expires_at. Authoritative stamps
# always win; the initial deadline comes from the lease, never this constant.
LEASE_TTL_S = 60.0


def lease_deadline_monotonic(
    expires_at: str,
    *,
    clock: Callable[[], float] = time.monotonic,
    wall_now: float | None = None,
) -> float | None:
    """Convert a lease `expires_at` ISO-8601 UTC stamp to monotonic-clock
    seconds for the driver deadline. Unparseable stamps return None.
    Never raises: the stamp is untrusted gateway input, so non-string
    and out-of-range values fail closed to None (the driver treats a
    declared-but-unparseable stamp as a dead lease)."""
    if not isinstance(expires_at, str):
        return None
    try:
        stamp = expires_at.strip()
        if stamp.endswith("Z"):
            stamp = stamp[:-1] + "+00:00"
        epoch = datetime.fromisoformat(stamp).timestamp()
    except Exception:  # noqa: BLE001 - untrusted input fails closed
        return None
    now_wall = time.time() if wall_now is None else wall_now
    return clock() + (epoch - now_wall)


def utc_stamp_in(seconds: float) -> str:
    """Test helper: an `expires_at` stamp `seconds` from now (UTC, `Z`,
    millisecond precision so sub-second test leases stay in the future)."""
    moment = datetime.fromtimestamp(time.time() + seconds, UTC)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class LeaseRenewalDriver:
    """One cancellable renewal loop for one live control lease."""

    def __init__(
        self,
        renew: Callable[[str], Awaitable[dict[str, Any]]],
        lease_id: str,
        *,
        interval_s: float = DEFAULT_RENEWAL_INTERVAL_S,
        expires_at: float | None = None,
        clock: Callable[[], float] = time.monotonic,
        renewed_ttl_s: float = LEASE_TTL_S,
        on_terminal_failure: Callable[[str], None] | None = None,
        diagnostics: LeaseTrace | None = None,
        first_delay_s: float | None = None,
    ) -> None:
        if interval_s <= 0:
            raise ValueError("renewal interval must be positive")
        if renewed_ttl_s <= 0:
            raise ValueError("renewed TTL must be positive")
        self._renew = renew
        self._lease_id = lease_id
        self._interval = interval_s
        self._deadline = expires_at
        self._first_delay = first_delay_s
        self._clock = clock
        self._renewed_ttl = renewed_ttl_s
        self._on_terminal_failure = on_terminal_failure
        self._task: asyncio.Task[None] | None = None
        self._invalidated = False
        self._renewals = 0
        self._attempt = 0
        self.failure_source = ""
        self._diagnostics = diagnostics or LeaseTrace.for_session(self, "", "", lease_id)

    @property
    def lease_id(self) -> str:
        return self._lease_id

    @property
    def deadline(self) -> float | None:
        """Known-expiry deadline (clock seconds) bounding retries, or
        None when no expiry is known."""
        return self._deadline

    def extend(self, deadline: float | None) -> None:
        """Adopt a server-provided expiry (monotonic seconds). Sessions
        use this when a renew response carries a fresh `expires_at`."""
        self._deadline = deadline

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def invalidated(self) -> bool:
        """True after a terminal renewal failure (writer revoked)."""
        return self._invalidated

    @property
    def attempts(self) -> int:
        return self._attempt

    @property
    def renewals(self) -> int:
        """Successful renewal calls so far (diagnostics)."""
        return self._renewals

    def start(self) -> None:
        """Start the loop (idempotent while running). Sync: task creation
        needs no await, so sync mount paths can (re)start renewal."""
        if self.running:
            return
        if self._invalidated:
            return
        self._task = asyncio.create_task(self._loop(), name=f"lease-renew-{self._lease_id}")

    def cancel(self) -> asyncio.Task[None] | None:
        """Cancel the loop without waiting (sync unmount paths)."""
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
        return task

    async def stop(self) -> None:
        """Cancel the loop and wait for it; no further renewals happen."""
        task = self.cancel()
        if task is None:
            return
        try:
            await task
        except asyncio.CancelledError:
            pass  # Expected cancellation of the driver, not of its caller.
        finally:
            caller = asyncio.current_task()
            if caller is not None and caller.cancelling():
                raise asyncio.CancelledError

    def _fail_terminal(self, code: str, *, source: str = "server_refusal") -> None:
        """Report one terminal failure exactly once and stop the loop."""
        if self._invalidated:
            return
        self._invalidated = True
        self.failure_source = source
        self._diagnostics.emit("renew_failure", reason=code, source=source, attempt=self._attempt)
        if self._on_terminal_failure is not None:
            self._on_terminal_failure(code)

    async def _renew_before_expiry(self) -> dict[str, Any]:
        # Expiry can pass between the loop's check and coroutine entry (including
        # timeout setup or scheduler preemption), so fence transport dispatch
        # here as well; constructing an awaitable does not dispatch it.
        if self._deadline is not None and self._clock() >= self._deadline:
            raise TimeoutError
        return await self._renew(self._lease_id)

    async def _loop(self) -> None:
        try:
            while True:
                delay = self._interval
                if self._first_delay is not None:
                    # A restarted driver for a live lease renews sooner than a full interval.
                    delay, self._first_delay = min(delay, self._first_delay), None
                if self._deadline is not None:
                    # Wake AT the deadline: expiry must invalidate even
                    # when the next tick would land far beyond it.
                    delay = min(delay, max(0.0, self._deadline - self._clock()))
                await asyncio.sleep(delay)
                if self._deadline is not None and self._clock() >= self._deadline:
                    # Known expiry reached with no successful renewal
                    # since: the lease is dead server-side. Report
                    # `lease_expired` WITHOUT another renew call — the UI
                    # writer must not stay live beyond expiry.
                    self._fail_terminal("lease_expired", source="local_deadline")
                    return
                timeout: float | None = None
                if self._deadline is not None:
                    # A stalled transport must not keep the writer live
                    # past expiry: bound the await to the deadline (the
                    # default HTTP timeout can outlast the TTL).
                    timeout = max(0.0, self._deadline - self._clock())
                self._attempt += 1
                try:
                    result = await asyncio.wait_for(self._renew_before_expiry(), timeout)
                except TimeoutError:
                    self._fail_terminal("lease_expired", source="renew_timeout")
                    return
                except TransportError as exc:
                    if exc.code in TERMINAL_RENEWAL_CODES or not exc.retryable:
                        self._fail_terminal(exc.code)
                        return
                    self._diagnostics.emit(
                        "renew_failure",
                        reason=exc.code,
                        source="server_refusal",
                        attempt=self._attempt,
                    )
                    continue
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 - renewal must never throw
                    self._diagnostics.emit(
                        "renew_failure",
                        reason="receive_failure",
                        source="unexpected",
                        attempt=self._attempt,
                    )
                    continue
                if not self._adopt_response(result):
                    return
                self._diagnostics.emit(
                    "lease_renew", reason="ok", source="server", attempt=self._attempt
                )
        except asyncio.CancelledError:
            pass

    def _adopt_response(self, result: dict[str, Any]) -> bool:
        """Adopt a successful renew response. Returns False when the
        lease must die: a declared-but-unusable `expires_at`
        (malformed, non-string, already past) fails closed with
        `lease_expired` instead of guessing a re-arm."""
        stamp = result.get("expires_at")
        if stamp is None:
            # Legacy transport without the field: nominal re-arm.
            self._renewals += 1
            self._deadline = self._clock() + self._renewed_ttl
            return True
        adopted = lease_deadline_monotonic(stamp, clock=self._clock)
        if adopted is None:
            self._fail_terminal("lease_expired", source="invalid_expiry")
            return False
        self._renewals += 1
        self._deadline = adopted
        if self._clock() >= self._deadline:
            # Authoritative but already past: the lease is dead now.
            self._fail_terminal("lease_expired", source="returned_expired")
            return False
        return True


__all__ = [
    "DEFAULT_RENEWAL_INTERVAL_S",
    "LEASE_TTL_S",
    "TERMINAL_RENEWAL_CODES",
    "LeaseRenewalDriver",
    "lease_deadline_monotonic",
    "utc_stamp_in",
]
