"""reconnect recovery against the real gateway.

Live cases use no scripted transports: HttpxGatewayTransport talks to a live uvicorn
serving the real app (auth, cursors, envelopes, SSE framing). Drops
before and after snapshot boundaries plus a mid-stream host restart
prove the controller resyncs instead of wiping or stalling.
The paused-HTTP regression separately pins readiness versus replay completion.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from relaydeck.client.features.sync.backoff import Backoff
from relaydeck.client.features.sync.controller import SyncController
from relaydeck.client.features.sync.transport import HttpxGatewayTransport
from relaydeck.domain.models import ConnectionState
from relaydeck.gateway.app import GatewayState, create_app
from relaydeck.gateway.backend import FakeBackend
from relaydeck.gateway.cursors import AFTER_SEQUENCE_HEADER, encode_cursor
from tests.support.live_server import LiveGateway

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "gateway"
SECRET = b"live-sync-test-secret-32bytes!!!!"
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
        # publish() assigns the next sequence; explicit sequences are
        # preserved for fault injection via publish_raw().
        template.pop("sequence", None)
    return template


async def _wait_until(pred: Any, timeout: float = 30.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not pred():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("timed out waiting for sync condition")
        await asyncio.sleep(0.01)


async def _wait_for_replay(controller: SyncController, sequence: int) -> None:
    # LIVE confirms accepted SSE headers, including for hosts without metadata.
    # It does not promise that this test's retained replay has finished applying.
    await _wait_until(
        lambda: controller.state.connection.state == ConnectionState.LIVE
        and controller.state.connection.last_sequence == sequence
    )


@pytest.mark.parametrize("paused_after", [0, 1, 2])
def test_replay_wait_does_not_confuse_live_with_caught_up(paused_after: int) -> None:
    """Real HTTP parser/controller, with the remaining replay held by an event."""

    async def scenario() -> None:
        state, backend = _seed()
        paused, release, finish = asyncio.Event(), asyncio.Event(), asyncio.Event()
        stopped = False

        class ReplayBody(httpx.AsyncByteStream):
            async def __aiter__(self) -> AsyncIterator[bytes]:
                for index, event in enumerate(backend.event_logs["home-mac"]):
                    if index == paused_after:
                        paused.set()
                        await release.wait()
                    yield f"event: metadata\ndata: {json.dumps(event)}\n\n".encode()
                await finish.wait()

        def respond(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/snapshot"):
                return httpx.Response(200, json=backend.snapshot("home-mac"))
            assert request.url.path == "/v1/events"
            return httpx.Response(
                200,
                headers={AFTER_SEQUENCE_HEADER: "0"},
                stream=ReplayBody(),
            )

        async def token() -> str:
            return "synthetic"

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            transport = HttpxGatewayTransport("https://gateway.test", token, http)
            controller = SyncController(transport)
            task = asyncio.create_task(controller.run("home-mac", lambda: stopped))
            waiting: asyncio.Task[None] | None = None
            try:
                await asyncio.wait_for(paused.wait(), 5)
                assert controller.state.connection.state == ConnectionState.LIVE
                assert controller.state.connection.last_sequence == paused_after
                waiting = asyncio.create_task(_wait_for_replay(controller, 3))
                await asyncio.sleep(0)  # Run the waiter; no elapsed-time readiness assumption.
                assert not waiting.done(), "LIVE did not yet include the full replay"
                release.set()
                await asyncio.wait_for(waiting, 5)
                assert controller.state.connection.last_sequence == 3
                assert controller.state.connection.cursor == "c3"
            finally:
                stopped = True
                release.set()
                finish.set()
                if waiting is not None and not waiting.done():
                    waiting.cancel()
                if waiting is not None:
                    await asyncio.gather(waiting, return_exceptions=True)
                await asyncio.wait_for(task, 5)
                state.push.close()

    asyncio.run(scenario())


def test_controller_live_apply_gap_resync() -> None:
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
                controller = SyncController(transport=transport, backoff=Backoff.seeded(3))
                states: list[str] = []
                controller.subscribe(lambda s: states.append(s.connection.state.value))
                task = asyncio.create_task(controller.run("home-mac", lambda: stopped))
                try:
                    await _wait_for_replay(controller, 3)
                    assert controller.state.connection.last_sequence == 3
                    assert controller.state.connection.cursor == "c3"
                    assert snapshots == 1

                    backend.publish(
                        "home-mac",
                        _event("term-9", payload={"status": "blocked", "name": "live"}),
                    )
                    await _wait_until(
                        lambda: "home-mac:term-9" in controller.state.inventory.agents
                    )
                    assert (
                        controller.state.inventory.agents["home-mac:term-9"].status.value
                        == "blocked"
                    )

                    forged = _event("term-9", sequence=9, cursor="c9")
                    backend.publish_raw("home-mac", forged)
                    await _wait_until(lambda: snapshots >= 2)
                    assert "synchronizing" in states
                finally:
                    stopped = True
                    # Wake the stream so the stop flag is noticed on the
                    # next frame (stop is checked per frame, and heartbeats
                    # yield no frames).
                    backend.publish("home-mac", _event("term-9"))
                    await asyncio.wait_for(task, timeout=15)
        assert snapshots >= 2

    asyncio.run(scenario())


class _TcpProxy:
    """Loopback TCP proxy that can sever client connections with RST.

    A graceful server shutdown closes SSE cleanly, which the controller
    rightly treats as a stop. Killing the proxy mid-stream models real
    whole-transport loss (dead middlebox, interface drop).
    """

    def __init__(self, upstream_port: int) -> None:
        self._upstream = upstream_port
        self._server: asyncio.AbstractServer | None = None
        self._pumps: set[asyncio.Task[None]] = set()
        self._writers: set[asyncio.StreamWriter] = set()
        self.port = 0

    async def start(self, port: int = 0) -> _TcpProxy:
        self._server = await asyncio.start_server(self._pipe, "127.0.0.1", port, reuse_address=True)
        addrs: list[Any] = list(self._server.sockets or [])
        self.port = addrs[0].getsockname()[1] if addrs else 0
        return self

    async def _pump(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                chunk = await reader.read(65536)
                if not chunk:
                    return
                writer.write(chunk)
                await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            return

    async def _pipe(
        self, down_reader: asyncio.StreamReader, down_writer: asyncio.StreamWriter
    ) -> None:
        self._writers.add(down_writer)
        try:
            up_reader, up_writer = await asyncio.open_connection("127.0.0.1", self._upstream)
        except OSError:
            down_writer.close()
            return
        self._writers.add(up_writer)
        first = asyncio.create_task(self._pump(down_reader, up_writer))
        second = asyncio.create_task(self._pump(up_reader, down_writer))
        self._pumps.add(first)
        self._pumps.add(second)
        try:
            await asyncio.wait((first, second), return_when=asyncio.FIRST_COMPLETED)
        finally:
            self._pumps.discard(first)
            self._pumps.discard(second)
            self._writers.discard(down_writer)
            self._writers.discard(up_writer)
            for task in (first, second):
                task.cancel()
            down_writer.close()
            up_writer.close()

    def kill(self) -> None:
        """Sever every proxied connection with RST (unclean drop)."""
        for writer in list(self._writers):
            transport = writer.transport
            try:
                transport.abort()
            except OSError:
                pass
        for pump in list(self._pumps):
            pump.cancel()

    async def stop(self) -> None:
        self.kill()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None


@contextlib.contextmanager
def _restart(app: Any, port: int) -> Any:
    last: Exception | None = None
    for _ in range(10):
        live = LiveGateway(app, port=port)
        try:
            live.__enter__()
            break
        except (OSError, RuntimeError) as exc:  # bind race
            last = exc
            time.sleep(0.2)
    else:
        raise RuntimeError(f"could not rebind {port}") from last
    try:
        yield live
    finally:
        live.__exit__(None, None, None)


def test_controller_recovers_after_transport_drop() -> None:
    async def scenario() -> None:
        state, backend = _seed()
        access, _ = state.sessions.create("acct-1", CAPS, frozenset({"home-mac"}), 1_000_000)
        stopped = False

        async def token() -> str:
            return access

        with LiveGateway(create_app(state)) as live:
            proxy = await _TcpProxy(live.port).start()
            try:
                base = f"http://127.0.0.1:{proxy.port}"
                async with httpx.AsyncClient(base_url=base, timeout=10.0) as http:
                    transport = HttpxGatewayTransport(base_url=base, token=token, client=http)
                    controller = SyncController(transport=transport, backoff=Backoff.seeded(3))
                    task = asyncio.create_task(controller.run("home-mac", lambda: stopped))
                    try:
                        await _wait_for_replay(controller, 3)
                        proxy.kill()  # whole transport loss, mid-stream
                        await _wait_until(
                            lambda: controller.state.connection.state
                            == ConnectionState.RECONNECTING,
                            timeout=20.0,
                        )
                        # Stale view retained, not wiped.
                        assert controller.state.connection.last_sequence == 3
                        assert controller.state.inventory.agents
                        await _wait_until(
                            lambda: controller.state.connection.state == ConnectionState.LIVE,
                            timeout=30.0,
                        )
                        backend.publish(
                            "home-mac",
                            _event("term-7", payload={"status": "running", "name": "back"}),
                        )
                        await _wait_until(
                            lambda: "home-mac:term-7" in controller.state.inventory.agents
                        )
                    finally:
                        stopped = True
                        backend.publish("home-mac", _event("term-7"))
                        await asyncio.wait_for(task, timeout=15)
            finally:
                await proxy.stop()

    asyncio.run(scenario())


def test_controller_starts_when_server_comes_up() -> None:
    async def scenario() -> None:
        state, backend = _seed()
        access, _ = state.sessions.create("acct-1", CAPS, frozenset({"home-mac"}), 1_000_000)
        stopped = False

        async def token() -> str:
            return access

        with LiveGateway(create_app(state)) as probe:
            port = probe.port
        # Gateway is down: the first snapshot hits connection refused.
        base = f"http://127.0.0.1:{port}"
        async with httpx.AsyncClient(base_url=base, timeout=10.0) as http:
            transport = HttpxGatewayTransport(base_url=base, token=token, client=http)
            controller = SyncController(transport=transport, backoff=Backoff.seeded(5))
            task = asyncio.create_task(controller.run("home-mac", lambda: stopped))
            try:
                await _wait_until(
                    lambda: controller.state.connection.state == ConnectionState.RECONNECTING,
                    timeout=20.0,
                )
                with _restart(create_app(state), port):
                    await _wait_until(
                        lambda: controller.state.connection.state == ConnectionState.LIVE,
                        timeout=30.0,
                    )
                    await _wait_until(
                        lambda: controller.state.connection.last_sequence == 3,
                        timeout=30.0,
                    )
            finally:
                stopped = True
                backend.publish("home-mac", _event("term-9"))
                await asyncio.wait_for(task, timeout=15)

    asyncio.run(scenario())
