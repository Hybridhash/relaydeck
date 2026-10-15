"""Control-lease renewal driver.

While a control lease is held and the session is foreground, the
driver periodically calls `renew_lease(lease_id) -> dict`. Terminal
renewal failures (lease_expired/lease_conflict/forbidden) invalidate
the writer through the revoke path — never silently. Stopping cancels
the task; no renewal runs after invalidation.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from typing import Any

import pytest

from relaydeck.client.features.sync.transport import TransportError
from relaydeck.client.features.terminal.renewal import (
    TERMINAL_RENEWAL_CODES,
    LeaseRenewalDriver,
)


def test_terminal_codes_cover_revoke_outcomes() -> None:
    assert TERMINAL_RENEWAL_CODES == frozenset(
        {"lease_expired", "lease_conflict", "forbidden", "unauthenticated", "stale_revision"}
    )


def test_driver_renews_until_stopped() -> None:
    async def scenario() -> None:
        calls: list[str] = []

        async def renew(lease_id: str) -> dict[str, Any]:
            calls.append(lease_id)
            return {"lease_id": lease_id}

        driver = LeaseRenewalDriver(renew, "lease-1", interval_s=0.01)
        driver.start()
        await asyncio.sleep(0.05)
        await driver.stop()
        first = len(calls)
        assert first >= 2
        assert set(calls) == {"lease-1"}
        await asyncio.sleep(0.03)
        assert len(calls) == first  # stopped: no further renewals

    asyncio.run(scenario())


def test_driver_terminal_failure_invalidates_and_stops() -> None:
    async def scenario() -> None:
        calls: list[str] = []
        invalidated: list[str] = []

        async def renew(lease_id: str) -> dict[str, Any]:
            calls.append(lease_id)
            raise TransportError("lease_expired", "taken over elsewhere")

        driver = LeaseRenewalDriver(
            renew,
            "lease-9",
            interval_s=0.01,
            on_terminal_failure=invalidated.append,
        )
        driver.start()
        await asyncio.sleep(0.05)
        assert invalidated == ["lease_expired"]
        count = len(calls)
        await asyncio.sleep(0.03)
        assert len(calls) == count  # no renewal after invalidation
        await driver.stop()

    asyncio.run(scenario())


def test_driver_transient_failure_keeps_renewing() -> None:
    async def scenario() -> None:
        calls: list[str] = []
        invalidated: list[str] = []

        async def renew(lease_id: str) -> dict[str, Any]:
            calls.append(lease_id)
            if len(calls) == 1:
                raise TransportError("host_offline", "blip")
            return {"lease_id": lease_id}

        driver = LeaseRenewalDriver(
            renew,
            "lease-2",
            interval_s=0.01,
            on_terminal_failure=invalidated.append,
        )
        driver.start()
        await asyncio.sleep(0.05)
        await driver.stop()
        assert invalidated == []
        assert len(calls) >= 2

    asyncio.run(scenario())


def test_driver_token_loss_invalidates_without_retry() -> None:
    """The exact `_token` failure triple: non-retryable auth loss stops
    after ONE call with the writer revoked (the original repro retried 11
    times before this fix)."""

    async def scenario() -> None:
        calls: list[str] = []
        invalidated: list[str] = []

        async def renew(lease_id: str) -> dict[str, Any]:
            calls.append(lease_id)
            raise TransportError("unauthenticated", "no access token", False)

        driver = LeaseRenewalDriver(
            renew,
            "lease-3",
            interval_s=0.001,
            on_terminal_failure=invalidated.append,
        )
        driver.start()
        await asyncio.sleep(0.015)
        assert calls == ["lease-3"]
        assert invalidated == ["unauthenticated"]
        assert driver.invalidated is True
        await asyncio.sleep(0.01)
        assert calls == ["lease-3"]
        await driver.stop()

    asyncio.run(scenario())


def test_driver_non_retryable_code_never_retries() -> None:
    async def scenario() -> None:
        calls: list[str] = []
        invalidated: list[str] = []

        async def renew(lease_id: str) -> dict[str, Any]:
            calls.append(lease_id)
            raise TransportError("too_large", "oversize", False)

        driver = LeaseRenewalDriver(
            renew,
            "lease-4",
            interval_s=0.005,
            on_terminal_failure=invalidated.append,
        )
        driver.start()
        await asyncio.sleep(0.03)
        assert calls == ["lease-4"]
        assert invalidated == ["too_large"]
        await driver.stop()

    asyncio.run(scenario())


def test_driver_transient_retries_stop_at_known_expiry() -> None:
    """Retryable failures retry while the lease is live, then report
    `lease_expired` WITHOUT another renew call once the known expiry
    passes — the writer cannot stay live beyond expiry."""

    async def scenario() -> None:
        import time as time_module

        calls: list[float] = []
        invalidated: list[str] = []
        started = time_module.monotonic()

        async def renew(lease_id: str) -> dict[str, Any]:
            calls.append(time_module.monotonic() - started)
            raise TransportError("host_offline", "down", True)

        driver = LeaseRenewalDriver(
            renew,
            "lease-5",
            interval_s=0.005,
            expires_at=started + 0.03,
            on_terminal_failure=invalidated.append,
        )
        driver.start()
        await asyncio.sleep(0.08)
        assert invalidated == ["lease_expired"]
        assert driver.invalidated is True
        assert len(calls) >= 1
        assert all(call < 0.03 for call in calls)  # no call at/after expiry
        count = len(calls)
        await asyncio.sleep(0.02)
        assert len(calls) == count
        await driver.stop()

    asyncio.run(scenario())


def test_scheduled_renewal_cannot_dispatch_after_expiry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Expiry can cross after timeout calculation and before transport entry."""

    async def scenario() -> None:
        now = [0.0]
        calls: list[str] = []
        invalidated: list[str] = []
        finished = asyncio.Event()

        async def renew(lease_id: str) -> dict[str, Any]:
            calls.append(lease_id)
            raise TransportError("host_offline", "down", True)

        def fail(code: str) -> None:
            invalidated.append(code)
            finished.set()

        driver = LeaseRenewalDriver(
            renew,
            "lease-delayed",
            interval_s=0.001,
            expires_at=1,
            clock=lambda: now[0],
            on_terminal_failure=fail,
        )
        loop = asyncio.get_running_loop()
        original_wait_for = asyncio.wait_for
        scheduled: list[bool] = []

        async def delayed_dispatch(coro: Awaitable[Any], timeout: float | None) -> Any:
            scheduled.append(True)
            assert timeout == 1
            now[0] = 2  # expiry crosses between timeout setup and coroutine entry
            return await original_wait_for(coro, timeout)

        monkeypatch.setattr(asyncio, "wait_for", delayed_dispatch)
        driver.start()
        timeout = loop.call_later(1, finished.set)
        try:
            await finished.wait()
            assert scheduled == [True]
            assert calls == []
            assert invalidated == ["lease_expired"]
            assert driver.invalidated
        finally:
            timeout.cancel()
            await driver.stop()

    asyncio.run(scenario())


def test_driver_success_rearms_retry_deadline() -> None:
    async def scenario() -> None:
        import time as time_module

        async def renew(lease_id: str) -> dict[str, Any]:
            return {"lease_id": lease_id}

        started = time_module.monotonic()
        driver = LeaseRenewalDriver(renew, "lease-6", interval_s=0.005, expires_at=started + 1000.0)
        before = driver.deadline
        driver.start()
        await asyncio.sleep(0.02)
        await driver.stop()
        assert driver.renewals >= 1
        assert driver.deadline != before
        assert driver.deadline is not None and driver.deadline > started

    asyncio.run(scenario())
