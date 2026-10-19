"""cursor expiry and duplicate reorder, live.

A rotated cursor secret proves stale cursors fail closed with a
retryable stale_epoch and recover through a fresh snapshot; duplicate
and out-of-order journal deliveries converge without resync; a gapped
journal heals into convergence once the missing sequences arrive.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from relaydeck.client.features.sync.backoff import Backoff
from relaydeck.client.features.sync.controller import SyncController
from relaydeck.client.features.sync.sse import SseFrame
from relaydeck.client.features.sync.transport import (
    HttpxGatewayTransport,
    TransportError,
)
from relaydeck.domain.models import ConnectionState
from relaydeck.gateway.app import GatewayState, create_app
from relaydeck.gateway.backend import FakeBackend
from relaydeck.gateway.cursors import encode_cursor
from tests.support.live_server import LiveGateway

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "gateway"
SECRET = b"live-replay-test-secret-32bytes!!!"
CAPS = frozenset(
    {
        "sessions.read",
        "terminal.view",
        "terminal.control",
        "agents.prompt",
        "agents.start",
        "shells.create",
        "terminal.attach",
    }
)


def _seed() -> tuple[GatewayState, FakeBackend]:
    backend = FakeBackend(now_ms=lambda: 1_000_000)
    snapshot: dict[str, Any] = json.loads((FIXTURES / "snapshot.json").read_text())
    snapshot["cursor"] = encode_cursor(SECRET, {"host_id": "home-mac", "sequence": 0})
    backend.snapshots["home-mac"] = snapshot
    template: dict[str, Any] = json.loads((FIXTURES / "metadata-event.json").read_text())
    for i in (1, 2, 3):
        event = dict(template, event_id=f"ev-{i}", sequence=i, cursor=f"c{i}")
        backend.event_logs.setdefault("home-mac", []).append(event)
    state = GatewayState(backend=backend, cursor_secret=SECRET, now_ms=lambda: 1_000_000)
    return state, backend


def _event(resource_id: str, **kw: Any) -> dict[str, Any]:
    template: dict[str, Any] = json.loads((FIXTURES / "metadata-event.json").read_text())
    template.update(resource_id=resource_id)
    template.update(kw)
    if "sequence" not in kw:
        template.pop("sequence", None)
    return template


async def _wait_until(pred: Any, timeout: float = 30.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not pred():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("timed out waiting for sync condition")
        await asyncio.sleep(0.01)


async def _next_gateway_frame(stream: AsyncIterator[SseFrame]) -> SseFrame:
    """Ignore client readiness; preserve every gateway frame and error."""
    while True:
        frame = await anext(stream)
        if frame.event != "stream_open":
            return frame


def test_stale_cursor_maps_and_recovers_live() -> None:
    async def scenario() -> None:
        state, backend = _seed()
        access, _ = state.sessions.create("acct-1", CAPS, frozenset({"home-mac"}), 1_000_000)

        async def token() -> str:
            return access

        with LiveGateway(create_app(state)) as live:
            async with httpx.AsyncClient(base_url=live.base_url, timeout=10.0) as http:
                transport = HttpxGatewayTransport(base_url=live.base_url, token=token, client=http)
                first = await transport.snapshot("home-mac")
                old_cursor = str(first["cursor"])
                # Host restarts with a rotated cursor secret; sessions stay.
                # The restarted host mints fresh cursors for new snapshots.
                rotated = b"rotated-cursor-secret-32bytes!!!!!"
                state.cursor_secret = rotated
                backend.snapshots["home-mac"]["cursor"] = encode_cursor(
                    rotated, {"host_id": "home-mac", "sequence": 0}
                )
                with pytest.raises(TransportError) as exc:
                    async for _ in transport.stream("home-mac", old_cursor):
                        pass
                assert exc.value.code == "stale_epoch"
                assert exc.value.retryable is True
                # Recovery is a fresh snapshot under the new secret.
                second = await transport.snapshot("home-mac")
                assert second["cursor"] != old_cursor
                stream = transport.stream("home-mac", str(second["cursor"]))
                try:
                    frame = await _next_gateway_frame(stream)
                    assert frame.event == "metadata"
                finally:
                    await stream.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("event", ["metadata", "error", "future-event"])
def test_gateway_frame_reader_skips_only_client_open_marker(event: str) -> None:
    async def scenario() -> None:
        async def token() -> str:
            return "synthetic-access"

        response = f"event: {event}\ndata: synthetic\n\n".encode()
        mock = httpx.MockTransport(lambda request: httpx.Response(200, content=response))
        async with httpx.AsyncClient(base_url="https://gateway.test", transport=mock) as http:
            transport = HttpxGatewayTransport("https://gateway.test", token, http)
            stream = transport.stream("home-mac", "synthetic-cursor")
            try:
                assert await _next_gateway_frame(stream) == SseFrame(event, "synthetic")
                with pytest.raises(StopAsyncIteration):
                    await _next_gateway_frame(stream)
            finally:
                await stream.aclose()

    asyncio.run(scenario())


def test_duplicate_reorder_converges_live() -> None:
    async def scenario() -> None:
        state, backend = _seed()
        access, _ = state.sessions.create("acct-1", CAPS, frozenset({"home-mac"}), 1_000_000)
        snapshots = 0
        real_snapshot = backend.snapshot

        def counting(host_id: str) -> dict[str, Any]:
            nonlocal snapshots
            snapshots += 1
            return real_snapshot(host_id)

        backend.snapshot = counting  # type: ignore[method-assign]
        stopped = False

        async def token() -> str:
            return access

        with LiveGateway(create_app(state)) as live:
            async with httpx.AsyncClient(base_url=live.base_url, timeout=10.0) as http:
                transport = HttpxGatewayTransport(base_url=live.base_url, token=token, client=http)
                controller = SyncController(transport=transport, backoff=Backoff.seeded(11))
                task = asyncio.create_task(controller.run("home-mac", lambda: stopped))
                try:
                    await _wait_until(lambda: controller.state.connection.last_sequence == 3)
                    assert snapshots == 1
                    # In-order pair, then a duplicate and an old delivery
                    # (replay overlap): all converge without resync.
                    backend.publish_raw(
                        "home-mac",
                        _event(
                            "term-4",
                            sequence=4,
                            cursor="c4",
                            payload={"status": "running", "name": "four"},
                        ),
                    )
                    backend.publish_raw(
                        "home-mac",
                        _event(
                            "term-5",
                            sequence=5,
                            cursor="c5",
                            payload={"status": "running", "name": "five"},
                        ),
                    )
                    backend.publish_raw("home-mac", _event("term-4", sequence=4, cursor="c4"))
                    backend.publish_raw("home-mac", _event("term-1", sequence=3, cursor="c3"))
                    await _wait_until(lambda: controller.state.connection.last_sequence == 5)
                    # A later in-order frame is a barrier: both older deliveries
                    # ahead of it have been consumed, regardless of runner speed.
                    backend.publish_raw("home-mac", _event("term-6", sequence=6, cursor="c6"))
                    await _wait_until(lambda: controller.state.connection.last_sequence == 6)
                    assert snapshots == 1
                    # Inventory keys are host-qualified so several hosts never collide.
                    assert set(controller.state.inventory.agents) >= {
                        "home-mac:term-4",
                        "home-mac:term-5",
                    }
                finally:
                    stopped = True
                    backend.publish("home-mac", _event("term-5"))
                    await asyncio.wait_for(task, timeout=15)

    asyncio.run(scenario())


def test_gap_heals_and_converges_live() -> None:
    async def scenario() -> None:
        state, backend = _seed()
        access, _ = state.sessions.create("acct-1", CAPS, frozenset({"home-mac"}), 1_000_000)
        snapshots = 0
        real_snapshot = backend.snapshot

        def counting(host_id: str) -> dict[str, Any]:
            nonlocal snapshots
            snapshots += 1
            return real_snapshot(host_id)

        backend.snapshot = counting  # type: ignore[method-assign]
        stopped = False

        async def token() -> str:
            return access

        with LiveGateway(create_app(state)) as live:
            async with httpx.AsyncClient(base_url=live.base_url, timeout=10.0) as http:
                transport = HttpxGatewayTransport(base_url=live.base_url, token=token, client=http)
                controller = SyncController(transport=transport, backoff=Backoff.seeded(7))
                task = asyncio.create_task(controller.run("home-mac", lambda: stopped))
                try:
                    await _wait_until(lambda: controller.state.connection.last_sequence == 3)
                    backend.publish_raw(
                        "home-mac",
                        _event(
                            "term-6",
                            sequence=6,
                            cursor="c6",
                            payload={"status": "running", "name": "six"},
                        ),
                    )
                    await _wait_until(lambda: snapshots >= 2, timeout=20.0)
                    # The journal heals: missing sequences arrive and the
                    # log is sequence-ordered again.
                    log = backend.event_logs["home-mac"]
                    log.append(
                        _event(
                            "term-4",
                            event_id="ev-4",
                            sequence=4,
                            cursor="c4",
                            payload={"status": "running", "name": "four"},
                        )
                    )
                    log.append(
                        _event(
                            "term-5",
                            event_id="ev-5",
                            sequence=5,
                            cursor="c5",
                            payload={"status": "running", "name": "five"},
                        )
                    )
                    log.sort(key=lambda e: int(e.get("sequence", 0)))
                    await _wait_until(
                        lambda: controller.state.connection.last_sequence == 6,
                        timeout=30.0,
                    )
                    assert controller.state.connection.state == ConnectionState.LIVE
                finally:
                    stopped = True
                    backend.publish("home-mac", _event("term-6"))
                    await asyncio.wait_for(task, timeout=15)

    asyncio.run(scenario())
