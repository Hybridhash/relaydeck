"""Q5c: LeaseRenewalOwner alone, driven through recording hooks (no session)."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, cast

import pytest

from relaydeck.client.features.shell.ops import LeaseInfo, ShellOpError
from relaydeck.client.features.sync.transport import TransportError
from relaydeck.client.features.terminal.controller import TerminalController
from relaydeck.client.features.terminal.renewal import utc_stamp_in
from relaydeck.client.features.terminal.session.lease import LeaseHooks, LeaseRenewalOwner
from relaydeck.lease_diagnostics import LeaseTrace

LOG = "relaydeck.lease_lifecycle"


def make_lease(lease_id: str, generation: int = 1, mode: str = "control") -> LeaseInfo:
    return LeaseInfo(
        lease_id=lease_id,
        attachment_id="att",
        host_id="mac",
        terminal_id="term",
        mode=mode,
        generation=generation,
        epoch="e1",
        expires_at=utc_stamp_in(60),
    )


class FakeController:
    def __init__(self) -> None:
        self.paused_writer = 0

    def pause_writer(self) -> None:
        self.paused_writer += 1


class Rig:
    def __init__(self, lease: LeaseInfo | None = None) -> None:
        self.lease: LeaseInfo | None = lease
        self.controller: FakeController | None = FakeController()
        self.paused = False
        self.disposed = False
        self.exited = False
        self.terminal = "agent-1"
        self.terminal_calls: list[str] = []
        self.closed = 0
        self.released: list[str] = []
        self.release_error: Exception | None = None
        self.hooks = LeaseHooks(
            session_id=4242,
            host_id="mac",
            terminal=lambda: self.terminal,
            lease=lambda: self.lease,
            clear_lease=self._clear,
            controller=lambda: cast("TerminalController | None", self.controller),
            paused=lambda: self.paused,
            disposed=lambda: self.disposed,
            exited=lambda: self.exited,
            close_controller=self._close,
            release_remote=self._release,
            on_terminal=self.terminal_calls.append,
        )

    def _clear(self) -> None:
        self.lease = None

    async def _close(self) -> None:
        self.closed += 1

    async def _release(self, lease_id: str) -> None:
        self.released.append(lease_id)
        if self.release_error is not None:
            raise self.release_error

    def owner(self, renew: Any = None, **kw: Any) -> LeaseRenewalOwner:
        return LeaseRenewalOwner(self.hooks, renew_lease=renew, **kw)


async def ok_renew(lease_id: str) -> dict[str, Any]:
    return {"lease_id": lease_id}


def test_trace_matches_the_session_trace_format_exactly() -> None:
    class Holder:
        pass

    holder = Holder()
    rig = Rig(make_lease("L1", generation=3))
    rig.hooks = LeaseHooks(**{**rig.hooks.__dict__, "session_id": id(holder)})
    owner = rig.owner()
    expected = LeaseTrace.for_session(holder, "mac", "agent-1", "L1", "control", 3)
    assert owner.trace() == expected
    other = make_lease("L2", generation=9, mode="view")
    assert owner.trace(other) == LeaseTrace.for_session(holder, "mac", "agent-1", "L2", "view", 9)
    rig.lease = None
    assert owner.trace() == LeaseTrace.for_session(holder, "mac", "agent-1", "", "", 0)
    rig.terminal = "renamed"  # evaluated lazily, like the session's own attribute
    assert owner.trace() == LeaseTrace.for_session(holder, "mac", "renamed", "", "", 0)


@pytest.mark.parametrize(
    "block",
    [
        {"lease": None},
        {"paused": True},
        {"disposed": True},
        {"exited": True},
    ],
)
def test_no_driver_runs_when_renewal_is_not_allowed(block: dict[str, Any]) -> None:
    async def go() -> None:
        rig = Rig(make_lease("L1"))
        for name, value in block.items():
            setattr(rig, name, value)
        owner = rig.owner(ok_renew)
        owner.restart()
        assert owner.driver is None and not owner.running

    asyncio.run(go())


def test_no_driver_without_a_renew_callable() -> None:
    async def go() -> None:
        owner = Rig(make_lease("L1")).owner(None)
        owner.restart()
        assert owner.driver is None

    asyncio.run(go())


def test_start_replaces_driver_and_swaps_the_renew_callable() -> None:
    async def go() -> None:
        calls: list[str] = []

        async def second(lease_id: str) -> dict[str, Any]:
            calls.append(lease_id)
            return {"lease_id": lease_id}

        rig = Rig(make_lease("L1"))
        owner = rig.owner(ok_renew)
        owner.start(30.0)
        first = owner.driver
        assert first is not None and owner.running
        owner.start(0.001, renew_lease=second)
        assert owner.driver is not first and not first.running
        async with asyncio.timeout(2):
            while not calls:
                await asyncio.sleep(0.001)
        assert calls[0] == "L1"
        await owner.stop()
        assert owner.driver is None and not owner.running

    asyncio.run(go())


def test_cancel_drops_the_driver_without_waiting() -> None:
    async def go() -> None:
        owner = Rig(make_lease("L1")).owner(ok_renew)
        owner.start(30.0)
        driver = owner.driver
        owner.cancel()
        assert owner.driver is None
        assert driver is not None
        await driver.stop()

    asyncio.run(go())


def test_live_terminal_failure_reaches_the_session_once() -> None:
    async def go() -> None:
        async def renew(_lease_id: str) -> dict[str, Any]:
            raise TransportError("lease_conflict", "taken", False)

        rig = Rig(make_lease("L1"))
        owner = rig.owner(renew)
        owner.start(0.001)
        async with asyncio.timeout(2):
            while not rig.terminal_calls:
                await asyncio.sleep(0.001)
        await asyncio.sleep(0.02)
        assert rig.terminal_calls == ["lease_conflict"]
        await owner.stop()

    asyncio.run(go())


def _renew_callback_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if "event=renew_callback" in r.getMessage()]


@pytest.mark.parametrize("late_code", ["lease_expired", "unauthenticated", "forbidden"])
@pytest.mark.parametrize("successor", ["lease", "controller", "driver"])
def test_late_failure_of_a_retired_lease_never_acts_on_its_successor(
    late_code: str, successor: str, caplog: pytest.LogCaptureFixture
) -> None:
    """Identity binding of driver, lease and controller: a late failure
    of a retired lease never acts on its successor.
    """
    caplog.set_level(logging.INFO, logger=LOG)

    async def go() -> None:
        first = make_lease("L1")
        rig = Rig(first)
        owner = rig.owner(ok_renew)
        owner.start(30.0)
        old_driver = owner.driver
        assert old_driver is not None
        callback = old_driver._on_terminal_failure
        assert callback is not None
        # The session replaces the lease/controller/driver; the old callback
        # is then delivered late (e.g. during the new connect).
        if successor == "lease":
            rig.lease = make_lease("L2", generation=2)
        elif successor == "controller":
            rig.controller = FakeController()
        else:
            owner.restart()  # a new driver for the same lease and controller
        caplog.clear()
        callback(late_code)
        assert rig.terminal_calls == []
        lines = _renew_callback_lines(caplog)
        assert len(lines) == 1 and "reason=superseded" in lines[0]
        fields = dict(part.split("=", 1) for part in lines[0].split() if "=" in part)
        assert (fields["lease"] == fields["current_lease"]) is (successor != "lease")
        assert "L1" not in lines[0] and "L2" not in lines[0]  # ids are hashed, never printed
        await owner.stop()
        await old_driver.stop()

    asyncio.run(go())


@pytest.mark.parametrize("state", ["disposed", "paused"])
def test_failure_is_ignored_once_the_session_is_disposed_or_paused(
    state: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger=LOG)

    async def go() -> None:
        rig = Rig(make_lease("L1"))
        owner = rig.owner(ok_renew)
        owner.start(30.0)
        assert owner.driver is not None
        callback = owner.driver._on_terminal_failure
        assert callback is not None
        setattr(rig, state, True)
        callback("lease_expired")
        assert rig.terminal_calls == []
        assert "reason=superseded" in _renew_callback_lines(caplog)[-1]
        await owner.stop()

    asyncio.run(go())


def test_current_failure_is_traced_as_current_and_delivered(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger=LOG)

    async def go() -> None:
        rig = Rig(make_lease("L1"))
        owner = rig.owner(ok_renew)
        owner.start(30.0)
        assert owner.driver is not None
        callback = owner.driver._on_terminal_failure
        assert callback is not None
        callback("lease_expired")
        assert rig.terminal_calls == ["lease_expired"]
        assert "reason=current" in _renew_callback_lines(caplog)[-1]
        await owner.stop()

    asyncio.run(go())


def test_release_stale_current_lease_closes_the_controller_then_releases() -> None:
    async def go() -> None:
        lease = make_lease("L1")
        rig = Rig(lease)
        controller = rig.controller
        await rig.owner().release_stale(lease)
        assert isinstance(controller, FakeController) and controller.paused_writer == 1
        assert rig.closed == 1 and rig.lease is None and rig.released == ["L1"]

    asyncio.run(go())


def test_release_stale_for_a_superseded_lease_leaves_the_current_one_alone() -> None:
    async def go() -> None:
        stale, current = make_lease("L1"), make_lease("L2")
        rig = Rig(current)
        await rig.owner().release_stale(stale)
        assert rig.lease is current and rig.closed == 0
        assert rig.controller is not None and rig.controller.paused_writer == 0
        assert rig.released == ["L1"]

    asyncio.run(go())


def test_release_failure_is_swallowed_and_never_retried() -> None:
    async def go() -> None:
        lease = make_lease("L1")
        rig = Rig(lease)
        rig.release_error = ShellOpError("gone", "release failed")
        await rig.owner().release_stale(lease)
        assert rig.released == ["L1"]

    asyncio.run(go())


def test_rebind_keeps_the_renewed_deadline_instead_of_the_issue_expiry() -> None:
    """A lease issued over 60 s ago but renewed since must survive a view rebind."""

    async def go() -> None:
        lease = LeaseInfo(
            lease_id="L1",
            attachment_id="att",
            host_id="mac",
            terminal_id="term",
            mode="control",
            generation=1,
            epoch="e1",
            expires_at=utc_stamp_in(-8),  # the issue-time expiry is already past
        )
        rig = Rig(lease)

        async def renew(lease_id: str) -> dict[str, Any]:
            return {"lease_id": lease_id, "expires_at": utc_stamp_in(60)}

        owner = rig.owner(renew, interval_s=30.0)
        owner.restart()
        assert owner.driver is not None
        owner.driver.extend(time.monotonic() + 52)  # what a successful renewal adopted
        owner.restart()  # bind_view restarts renewal
        await asyncio.sleep(0.05)
        assert rig.terminal_calls == []
        assert owner.running
        assert owner.driver is not None and owner.driver.deadline is not None
        assert owner.driver.deadline > time.monotonic() + 40
        owner.cancel()

    asyncio.run(go())


def test_a_new_lease_does_not_inherit_the_previous_lease_deadline() -> None:
    async def go() -> None:
        rig = Rig(make_lease("L1"))
        owner = rig.owner(ok_renew)
        owner.restart()
        assert owner.driver is not None
        owner.driver.extend(time.monotonic() + 500)
        rig.lease = make_lease("L2", generation=2)
        owner.restart()
        assert owner.driver is not None and owner.driver.deadline is not None
        assert owner.driver.deadline < time.monotonic() + 120
        owner.cancel()

    asyncio.run(go())


def test_a_superseded_lease_stops_renewing() -> None:
    async def go() -> None:
        rig = Rig(make_lease("L1"))
        calls: list[str] = []

        async def refused(lease_id: str) -> dict[str, Any]:
            calls.append(lease_id)
            raise TransportError("stale_revision", "superseded", True)

        owner = rig.owner(refused, interval_s=0.01)
        owner.restart()
        await asyncio.sleep(0.1)
        assert calls == ["L1"]
        assert rig.terminal_calls == ["stale_revision"]
        assert not owner.running

    asyncio.run(go())
