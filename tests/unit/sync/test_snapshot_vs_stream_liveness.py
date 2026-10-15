"""Snapshot success is not proof of metadata stream liveness."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from relaydeck.client.features.sync.backoff import Backoff
from relaydeck.client.features.sync.controller import SyncController
from relaydeck.client.features.sync.transport import HttpxGatewayTransport, TransportError
from relaydeck.domain import reducers
from relaydeck.domain.models import ConnectionState
from tests.support.sync.scripted_sync import _meta, _ScriptedTransport, _snapshot_doc


@pytest.mark.parametrize(
    "error",
    [
        TransportError("unauthenticated", "rejected", False),
        TransportError("host_offline", "drop", True),
        None,
    ],
)
def test_snapshot_without_open_stream_never_claims_live(error: TransportError | None) -> None:
    async def scenario() -> None:
        transport = _ScriptedTransport()
        transport.scripts = [[error] if error is not None else []]
        seen: list[ConnectionState] = []
        sleeps: list[float] = []

        async def sleep(delay: float) -> None:
            sleeps.append(delay)

        controller = SyncController(transport=transport, sleep=sleep)
        controller.subscribe(lambda state: seen.append(state.connection.state))
        await controller.run("h1", stop=lambda: bool(sleeps))
        assert transport.snapshots == 1
        assert ConnectionState.LIVE not in seen
        assert controller.state.revoked is (error is not None and error.code == "unauthenticated")
        if not controller.state.revoked:
            assert controller.state.stale

    asyncio.run(scenario())


def test_resync_during_backoff_is_covered_by_next_fresh_snapshot() -> None:
    async def scenario() -> None:
        transport = _ScriptedTransport()
        transport.scripts = [[TransportError("host_offline", "drop")], [_meta(1)]]
        controller: SyncController

        async def sleep(_: float) -> None:
            controller.request_resync()
            # Deliver the same thread-safe callback used in production.
            await asyncio.sleep(0)

        controller = SyncController(transport=transport, backoff=Backoff.seeded(1), sleep=sleep)
        await asyncio.wait_for(
            controller.run(
                "h1",
                stop=lambda: controller.state.connection.last_sequence == 1
                or transport.snapshots >= 3,
            ),
            2,
        )
        assert transport.snapshots == 2
        assert controller.state.connection.last_sequence == 1
        assert controller.state.connection.state is ConnectionState.LIVE

    asyncio.run(scenario())


def test_outage_clock_and_attempt_survive_snapshots_and_backoff_saturation() -> None:
    async def scenario() -> None:
        transport = _ScriptedTransport()
        transport.scripts = [[TransportError("stream_closed", "closed")] for _ in range(40)]
        now = [100.0]
        seen: list[tuple[float, int]] = []
        controller: SyncController

        async def sleep(_: float) -> None:
            connection = controller.state.connection
            seen.append((connection.not_live_since, connection.retry_attempt))
            now[0] += 4

        controller = SyncController(transport=transport, sleep=sleep, clock=lambda: now[0])
        await controller.run("h1", stop=lambda: len(seen) == 40)
        assert seen == [(100.0, attempt) for attempt in range(1, 41)]
        assert controller.backoff.attempts == 32
        assert controller.state.stale

    asyncio.run(scenario())


def test_real_http_idle_stream_is_live_after_acceptance_without_metadata() -> None:
    async def scenario() -> None:
        opened, hold = asyncio.Event(), asyncio.Event()

        class Idle(httpx.AsyncByteStream):
            async def __aiter__(self) -> Any:
                opened.set()
                yield b": heartbeat\n\n"
                await hold.wait()

        async def response(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/snapshot"):
                return httpx.Response(200, json=_snapshot_doc())
            assert request.url.path == "/v1/events"
            return httpx.Response(200, stream=Idle())

        async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:

            async def token() -> str:
                return "synthetic-test-only"

            controller = SyncController(
                transport=HttpxGatewayTransport("https://private.test", token, client)
            )
            task = asyncio.create_task(controller.run("h1"))
            try:
                await asyncio.wait_for(opened.wait(), 1)
                await asyncio.sleep(0.02)
                assert controller.state.connection.state is ConnectionState.LIVE
                assert not controller.state.stale
                assert controller.state.connection.last_sequence == 0
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


@pytest.mark.parametrize("status, code", [(401, "unauthenticated"), (503, "host_offline")])
def test_real_http_rejected_stream_keeps_snapshot_stale(status: int, code: str) -> None:
    async def scenario() -> None:
        async def response(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/snapshot"):
                return httpx.Response(200, json=_snapshot_doc())
            return httpx.Response(status, json={"code": code, "message": "synthetic rejection"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:

            async def token() -> str:
                return "synthetic-test-only"

            slept: list[float] = []

            async def sleep(delay: float) -> None:
                slept.append(delay)

            controller = SyncController(
                transport=HttpxGatewayTransport("https://private.test", token, client), sleep=sleep
            )
            seen: list[ConnectionState] = []
            controller.subscribe(lambda state: seen.append(state.connection.state))
            await controller.run("h1", stop=lambda: bool(slept))
            assert ConnectionState.LIVE not in seen
            assert controller.state.stale
            assert controller.state.revoked is (status == 401)

    asyncio.run(scenario())


def test_late_open_signal_cannot_clear_revoked_connection() -> None:
    controller = SyncController(transport=_ScriptedTransport())
    controller._set(reducers.revoke(controller.state))
    controller._stream_opened()
    assert controller.state.revoked
    assert controller.state.connection.state is ConnectionState.ACCESS_REVOKED
