"""Renewal expiry-adoption regressions.

Two fail-closed gaps: a stalled renew await outlived the known lease
deadline without invalidating the writer, and a successful response's
authoritative `expires_at` was discarded in favor of an unconditional
now+60 re-arm. The driver must bound both sleep and the renew await to
the deadline, adopt the authoritative expiry (short or extended), and
invalidate on stalled/malformed/past expiries.
"""

from __future__ import annotations

import asyncio
from typing import Any

from relaydeck.client.features.terminal.renewal import (
    LEASE_TTL_S,
    LeaseRenewalDriver,
    utc_stamp_in,
)


def test_stalled_renew_invalidates_at_deadline() -> None:
    async def scenario() -> None:
        gate = asyncio.Event()
        invalidated: list[str] = []

        async def renew(lease_id: str) -> dict[str, Any]:
            await gate.wait()
            return {"lease_id": lease_id}

        import time

        driver = LeaseRenewalDriver(
            renew,
            "lease-stall",
            interval_s=0.005,
            expires_at=time.monotonic() + 0.015,
            on_terminal_failure=invalidated.append,
        )
        driver.start()
        await asyncio.sleep(0.04)
        assert invalidated == ["lease_expired"]
        assert driver.running is False
        gate.set()
        await driver.stop()

    asyncio.run(scenario())


def test_short_authoritative_expiry_adopted_not_overridden() -> None:
    async def scenario() -> None:
        import time

        calls: list[str] = []
        invalidated: list[str] = []

        async def renew(lease_id: str) -> dict[str, Any]:
            calls.append(lease_id)
            if len(calls) == 1:
                return {"lease_id": lease_id, "expires_at": utc_stamp_in(0.02)}
            await asyncio.sleep(10)  # stall: cancelled at the adopted deadline
            return {"lease_id": lease_id}

        driver = LeaseRenewalDriver(
            renew,
            "lease-short",
            interval_s=0.005,
            expires_at=time.monotonic() + 60.0,
            on_terminal_failure=invalidated.append,
        )
        driver.start()
        await asyncio.sleep(0.01)
        assert len(calls) >= 1
        assert driver.deadline is not None
        # Adopted +.02, not the nominal +60 re-arm.
        assert driver.deadline - time.monotonic() < 1.0
        await asyncio.sleep(0.06)
        assert invalidated == ["lease_expired"]
        count = len(calls)
        await asyncio.sleep(0.03)
        assert len(calls) == count  # no renew call past the adopted expiry
        await driver.stop()

    asyncio.run(scenario())


def test_extended_expiry_adopted_without_clamp() -> None:
    async def scenario() -> None:
        import time

        async def renew(lease_id: str) -> dict[str, Any]:
            return {"lease_id": lease_id, "expires_at": utc_stamp_in(3600.0)}

        driver = LeaseRenewalDriver(renew, "lease-long", interval_s=0.005)
        driver.start()
        await asyncio.sleep(0.02)
        assert driver.renewals >= 1
        assert driver.deadline is not None
        remaining = driver.deadline - time.monotonic()
        assert remaining > LEASE_TTL_S  # extension kept, not clamped to 60
        assert driver.invalidated is False
        await driver.stop()

    asyncio.run(scenario())


def test_malformed_expiry_fails_closed() -> None:
    async def scenario() -> None:
        invalidated: list[str] = []

        async def renew(lease_id: str) -> dict[str, Any]:
            return {"lease_id": lease_id, "expires_at": "not-a-stamp!!"}

        driver = LeaseRenewalDriver(
            renew, "lease-bad", interval_s=0.005, on_terminal_failure=invalidated.append
        )
        driver.start()
        task = driver._task
        await asyncio.sleep(0.03)
        assert invalidated == ["lease_expired"]
        assert driver.renewals == 0
        assert task is not None and task.done()
        assert task.exception() is None  # fail-closed, never throws
        await driver.stop()

    asyncio.run(scenario())


def test_non_string_expiry_fails_closed_without_throw() -> None:
    async def scenario() -> None:
        invalidated: list[str] = []

        async def renew(lease_id: str) -> dict[str, Any]:
            return {"lease_id": lease_id, "expires_at": 12345}

        driver = LeaseRenewalDriver(
            renew, "lease-int", interval_s=0.005, on_terminal_failure=invalidated.append
        )
        driver.start()
        task = driver._task
        await asyncio.sleep(0.03)
        assert invalidated == ["lease_expired"]
        assert task is not None and task.done()
        assert task.exception() is None
        await driver.stop()

    asyncio.run(scenario())


def test_missing_expiry_keeps_legacy_rearm() -> None:
    async def scenario() -> None:
        import time

        async def renew(lease_id: str) -> dict[str, Any]:
            return {"lease_id": lease_id}  # legacy transport: no field

        driver = LeaseRenewalDriver(renew, "lease-legacy", interval_s=0.005)
        driver.start()
        await asyncio.sleep(0.02)
        assert driver.renewals >= 1
        assert driver.deadline is not None
        remaining = driver.deadline - time.monotonic()
        assert LEASE_TTL_S - 5.0 < remaining <= LEASE_TTL_S
        assert driver.invalidated is False
        await driver.stop()

    asyncio.run(scenario())


def test_past_authoritative_expiry_fails_immediately() -> None:
    async def scenario() -> None:
        invalidated: list[str] = []

        async def renew(lease_id: str) -> dict[str, Any]:
            return {"lease_id": lease_id, "expires_at": utc_stamp_in(-5.0)}

        driver = LeaseRenewalDriver(
            renew,
            "lease-past",
            interval_s=0.005,
            on_terminal_failure=invalidated.append,
        )
        driver.start()
        await asyncio.sleep(0.03)
        assert invalidated == ["lease_expired"]
        await driver.stop()

    asyncio.run(scenario())
