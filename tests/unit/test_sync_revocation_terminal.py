"""Once access is revoked, the sync controller publishes nothing but the revoked status."""

from __future__ import annotations

import asyncio
from typing import Any

from relaydeck.client.features.sync.backoff import Backoff
from relaydeck.client.features.sync.controller import SyncController
from relaydeck.client.features.sync.transport import TransportError
from relaydeck.domain import reducers
from relaydeck.domain.models import AppState, ConnectionState
from tests.support.sync.scripted_sync import _meta, _ScriptedTransport


class _GatedSnapshots(_ScriptedTransport):
    """Holds the snapshot call number `hold` until `release` is set."""

    def __init__(self, hold: int) -> None:
        super().__init__()
        self.hold = hold
        self.waiting = asyncio.Event()
        self.release = asyncio.Event()

    async def snapshot(self, host_id: str) -> dict[str, Any]:
        doc = await super().snapshot(host_id)
        if self.snapshots == self.hold:
            self.waiting.set()
            await self.release.wait()
        return doc


def _watch(controller: SyncController) -> list[AppState]:
    after_revoke: list[AppState] = []

    def listener(state: AppState) -> None:
        if controller.state.revoked:
            after_revoke.append(state)

    controller.subscribe(listener)
    return after_revoke


def _assert_terminal(controller: SyncController, published: list[AppState]) -> None:
    assert controller.state.revoked is True
    assert controller.state.connection.state == ConnectionState.ACCESS_REVOKED
    assert {s.connection.state for s in published} <= {ConnectionState.ACCESS_REVOKED}
    assert all(s.revoked for s in published)


def _revoke(controller: SyncController) -> None:
    controller._set(reducers.revoke(controller.state))


def test_revocation_during_the_snapshot_await_is_not_overwritten() -> None:
    async def scenario() -> None:
        transport = _GatedSnapshots(hold=1)
        transport.scripts.append([_meta(1)])
        controller = SyncController(transport=transport, backoff=Backoff.seeded(3))
        published = _watch(controller)
        task = asyncio.ensure_future(controller.run("h1"))
        await transport.waiting.wait()
        _revoke(controller)
        transport.release.set()
        await asyncio.wait_for(task, 2)
        _assert_terminal(controller, published)
        assert transport.cursors == []  # no stream was opened for a revoked identity

    asyncio.run(scenario())


def test_revocation_during_the_retry_sleep_schedules_no_reconnect() -> None:
    async def scenario() -> None:
        transport = _ScriptedTransport()
        transport.scripts.append([TransportError("host_offline", "drop", True)])
        transport.scripts.append([_meta(1)])
        controller = SyncController(transport=transport, backoff=Backoff.seeded(3))
        published = _watch(controller)

        async def sleep(_delay: float) -> None:
            _revoke(controller)

        controller.sleep = sleep
        await asyncio.wait_for(controller.run("h1"), 2)
        _assert_terminal(controller, published)
        assert transport.snapshots == 1  # the retry never started

    asyncio.run(scenario())


def test_revocation_during_a_reconnect_attempt_is_not_overwritten() -> None:
    async def scenario() -> None:
        transport = _GatedSnapshots(hold=2)
        transport.scripts.append([TransportError("host_offline", "drop", True)])
        transport.scripts.append([_meta(1)])
        controller = SyncController(transport=transport, backoff=Backoff.seeded(3))
        controller.sleep = _no_sleep
        published = _watch(controller)
        task = asyncio.ensure_future(controller.run("h1"))
        await transport.waiting.wait()
        assert controller.state.connection.state == ConnectionState.RECONNECTING
        _revoke(controller)
        transport.release.set()
        await asyncio.wait_for(task, 2)
        _assert_terminal(controller, published)
        assert transport.cursors == ["c0"]  # only the first stream was ever opened

    asyncio.run(scenario())


def test_late_work_after_revocation_is_dropped() -> None:
    controller = SyncController(transport=_ScriptedTransport())
    _revoke(controller)
    controller._reconnecting("late")
    controller._set(reducers.set_connection(controller.state, ConnectionState.SYNCHRONIZING))
    controller.apply_grants([])
    assert controller.state.connection.state == ConnectionState.ACCESS_REVOKED
    assert controller._retry_attempt == 0


async def _no_sleep(_delay: float) -> None:
    return None
