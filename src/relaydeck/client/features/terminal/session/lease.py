"""Identity-bound lease renewal and stale-lease release.

A failure reaches the session only while its captured driver, lease and
controller are current and the session is neither paused nor disposed.
Retired failures cannot act on a replacement. All access uses `LeaseHooks`."""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from relaydeck.client.features.shell.ops import LeaseInfo, ShellOpError
from relaydeck.client.features.sync.transport import TransportError
from relaydeck.client.features.terminal.controller import TerminalController
from relaydeck.client.features.terminal.renewal import (
    DEFAULT_RENEWAL_INTERVAL_S,
    LeaseRenewalDriver,
    lease_deadline_monotonic,
)
from relaydeck.lease_diagnostics import LeaseTrace, opaque

RenewLease = Callable[[str], Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class LeaseHooks:
    """What the owner needs from its session (no session back-reference)."""

    session_id: int  # lease-trace correlation only (hashed, never printed)
    host_id: str
    terminal: Callable[[], str]  # agent: target, shell: terminal id
    lease: Callable[[], LeaseInfo | None]
    clear_lease: Callable[[], None]
    controller: Callable[[], TerminalController | None]
    paused: Callable[[], bool]
    disposed: Callable[[], bool]
    exited: Callable[[], bool]
    close_controller: Callable[[], Awaitable[None]]
    release_remote: Callable[[str], Awaitable[Any]]
    on_terminal: Callable[[str], None]  # session policy for a live terminal failure


class LeaseRenewalOwner:
    def __init__(
        self,
        hooks: LeaseHooks,
        *,
        renew_lease: RenewLease | None = None,
        interval_s: float = DEFAULT_RENEWAL_INTERVAL_S,
    ) -> None:
        self._hooks = hooks
        self._renew_lease = renew_lease
        self._interval_s = interval_s
        self.driver: LeaseRenewalDriver | None = None
        # The newest expiry a renewal adopted, per lease id. LeaseInfo is frozen at issue, so a
        # driver rebuilt on rebind must start from this deadline, not the original one.
        self._renewed: tuple[str, float] | None = None

    def _remember(self, driver: LeaseRenewalDriver | None) -> None:
        # Best effort: a stand-in driver without these attributes simply records nothing.
        deadline = getattr(driver, "deadline", None)
        lease_id = getattr(driver, "lease_id", None)
        if (
            isinstance(deadline, float)
            and isinstance(lease_id, str)
            and not getattr(driver, "invalidated", True)
        ):
            self._renewed = (lease_id, deadline)

    @property
    def running(self) -> bool:
        return self.driver is not None and self.driver.running

    def start(self, interval_s: float, *, renew_lease: RenewLease | None = None) -> None:
        """(Re)start renewal for the held control lease. Restart
        semantics: any previous driver is cancelled first. No lease, no
        renew callable, paused, or disposed: no driver runs. A passed
        `renew_lease` replaces the constructor callable from now on."""
        if renew_lease is not None:
            self._renew_lease = renew_lease
        self._interval_s = interval_s
        self.restart()

    def trace(self, lease: LeaseInfo | None = None) -> LeaseTrace:
        hooks = self._hooks
        lease = lease or hooks.lease()
        return LeaseTrace(
            opaque("session", hooks.session_id),
            opaque("terminal", hooks.host_id, hooks.terminal()),
            lease.lease_id if lease is not None else "",
            lease.mode if lease is not None else "",
            lease.generation if lease is not None else 0,
        )

    def cancel(self) -> None:
        """Cancel the driver without waiting for its task."""
        if self.driver is not None:
            self._remember(self.driver)
            self.driver.cancel()
            self.driver = None

    def restart(self) -> None:
        hooks = self._hooks
        self.cancel()
        if (
            self._renew_lease is None
            or hooks.lease() is None
            or hooks.paused()
            or hooks.disposed()
            or hooks.exited()
        ):
            return
        lease, controller = hooks.lease(), hooks.controller()
        assert lease is not None  # noqa: S101
        trace = self.trace(lease)

        def failed(code: str) -> None:
            # A retired lease's failure has no authority over its successor.
            live = (
                self.driver is driver
                and hooks.lease() is lease
                and hooks.controller() is controller
                and not hooks.disposed()
                and not hooks.paused()
            )
            current = hooks.lease()
            trace.emit(
                "renew_callback",
                reason="current" if live else "superseded",
                current_lease_id=current.lease_id if current is not None else "",
                source=driver.failure_source,
                attempt=driver.attempts,
            )
            if live:
                hooks.on_terminal(code)

        deadline = lease_deadline_monotonic(lease.expires_at)
        first_delay: float | None = None
        renewed = self._renewed
        if renewed is not None and renewed[0] != lease.lease_id:
            self._renewed = renewed = None  # a genuinely new lease starts from its own expiry
        if renewed is not None and (deadline is None or renewed[1] > deadline):
            deadline = renewed[1]
            # Renew well before that deadline even if rebinds keep restarting the driver.
            first_delay = max(0.0, (deadline - time.monotonic()) / 2)
        driver = LeaseRenewalDriver(
            self._renew_lease,
            lease.lease_id,
            interval_s=self._interval_s,
            expires_at=deadline,
            on_terminal_failure=failed,
            diagnostics=trace,
            first_delay_s=first_delay,
        )
        self.driver = driver
        driver.start()

    async def stop(self) -> None:
        """Cancel renewal and wait for the task (pause/dispose paths)."""
        driver, self.driver = self.driver, None
        self._remember(driver)
        if driver is not None:
            await driver.stop()

    async def release_stale(self, lease: LeaseInfo) -> None:
        hooks = self._hooks
        if hooks.lease() is lease:
            controller = hooks.controller()
            if controller is not None:
                controller.pause_writer()
            await hooks.close_controller()
            hooks.clear_lease()
        try:
            await hooks.release_remote(lease.lease_id)
        except (ShellOpError, TransportError):
            pass  # Known lease expires server-side; never replay the acquisition.
