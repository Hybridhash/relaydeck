"""Real metadata/auth boundaries without listening sockets."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import flet as ft
import httpx
import pytest

from relaydeck.client.adapters.fake import FakeClipboard, FakeCredentialStore
from relaydeck.client.features.auth.service import ACCESS_KEY, REFRESH_KEY
from relaydeck.client.features.sync.controller import SyncController
from relaydeck.client.features.sync.sse import SseFrame, SseParser
from relaydeck.client.features.sync.transport import TransportError
from relaydeck.client.packaged_runtime import PackagedRuntime
from relaydeck.gateway.app import create_app
from relaydeck.gateway.backend import BackendError
from relaydeck.gateway.metadata_follower import MAX_PENDING_METADATA
from relaydeck.gateway.production import JournalBackend
from relaydeck.journal import CommandJournal
from tests.security.helpers import ALL_CAPS, NOW, make_state
from tests.support.host_harness import render_component, run
from tests.support.sync.scripted_sync import _ScriptedTransport
from tests.support.ui.packaged import Preferences


def test_eof_is_stale_and_repeated_closures_back_off_without_overflow() -> None:
    async def scenario() -> None:
        transport = _ScriptedTransport()
        delays: list[float] = []
        controller = SyncController(transport)

        async def sleep(delay: float) -> None:
            delays.append(delay)

        controller.sleep = sleep
        await controller.run("h1", stop=lambda: len(delays) == 80)
        assert controller.state.connection.state.value == "reconnecting"
        assert controller.state.connection.detail == "stream_closed"
        assert len(transport.cursors) == 80
        assert all(0.375 <= delay <= 37.5 for delay in delays)
        assert min(delays[6:]) >= 22.5

    run(scenario())


def test_repeated_auth_denial_refreshes_once_and_cannot_loop() -> None:
    async def scenario() -> None:
        transport = _ScriptedTransport()
        transport.scripts = [[TransportError("unauthenticated", "expired", False)]] * 2
        refreshes = 0

        async def recover() -> bool:
            nonlocal refreshes
            refreshes += 1
            return True

        async def sleep(delay: float) -> None:
            assert delay >= 0.375

        controller = SyncController(transport, recover_auth=recover, sleep=sleep)
        state = await controller.run("h1")
        assert state.revoked and refreshes == 1 and len(transport.cursors) == 2

    run(scenario())


def test_quiet_access_expiry_interrupts_stream_and_cleans_up() -> None:
    async def scenario() -> None:
        state, _ = make_state()
        loop = asyncio.get_running_loop()
        started = loop.time()
        state.now_ms = lambda: NOW + int((loop.time() - started) * 1000)
        token, _ = state.sessions.create("acct-1", ALL_CAPS, {"home-mac"}, NOW, "native")
        state.sessions._by_access[token] = replace(
            state.sessions._by_access[token], access_expires_ms=NOW + 40
        )
        stream = _sse(create_app(state), token, lambda: None)
        try:
            frame = await asyncio.wait_for(anext(stream), 0.5)
            assert frame.event == "error"
            assert json.loads(frame.data)["details"]["reason"] == "access_expired"
        finally:
            await stream.aclose()
            assert not state.metadata._held
            state.push.close()

    run(scenario())


async def _sse(app: Any, token: str, on_start: Any) -> AsyncGenerator[SseFrame]:
    messages: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=1)
    inbound: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    inbound.put_nowait({"type": "http.request", "body": b"", "more_body": False})
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "https",
        "path": "/v1/events",
        "raw_path": b"/v1/events",
        "query_string": b"host_id=home-mac",
        "root_path": "",
        "headers": [(b"authorization", f"Bearer {token}".encode()), (b"host", b"gw")],
        "client": ("127.0.0.1", 1),
        "server": ("gw", 443),
    }
    task = asyncio.create_task(app(scope, inbound.get, messages.put))
    parser = SseParser()
    try:
        start = await asyncio.wait_for(messages.get(), 1)
        on_start()
        if start["status"] != 200:
            body = await asyncio.wait_for(messages.get(), 1)
            raise TransportError(json.loads(body["body"])["code"], "Access changed", False)
        while True:
            message = await asyncio.wait_for(messages.get(), 1)
            for frame in parser.feed(message.get("body", b"")):
                yield frame
            if not message.get("more_body", False):
                return
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("outcome", ["refresh", "rejected", "logout"])
def test_native_stream_expiry_refresh_is_scoped_and_logout_cannot_restore_authority(
    outcome: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        state, _ = make_state()
        clock = [NOW]
        state.now_ms = lambda: clock[0]
        _, refresh = state.sessions.create("acct-1", ALL_CAPS, {"home-mac"}, NOW, "native")
        secure = FakeCredentialStore()
        await secure.set(REFRESH_KEY, refresh)
        await secure.set("identity_origin", "https://gw")
        app = create_app(state)
        requests: list[str] = []
        refreshing, release = asyncio.Event(), asyncio.Event()

        async def request_seen(request: httpx.Request) -> None:
            requests.append(f"{request.method} {request.url.path}")
            if outcome == "logout" and request.url.path == "/v1/token" and refreshing.is_set():
                await release.wait()

        async def browser(url: str) -> None:
            pytest.fail("Metadata recovery must not open identity UI")

        mounted = await render_component(lambda: ft.Text("Synthetic"))
        page = mounted.page
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), event_hooks={"request": [request_seen]}
        ) as http:
            runtime = PackagedRuntime(
                page,
                secure,
                Preferences({"relaydeck-endpoint": "https://gw"}),
                browser,
                lambda: None,
                client=http,
                clipboard=FakeClipboard(),
            )
            try:
                await runtime.start()
                resources = runtime.resources
                assert resources is not None
                token = await secure.get(ACCESS_KEY)
                assert token is not None
                state.sessions._by_access[token] = replace(
                    state.sessions._by_access[token], access_expires_ms=NOW + 10
                )
                finished, attempts = False, 0

                async def stream(host: str, cursor: str) -> AsyncGenerator[SseFrame]:
                    nonlocal finished, attempts
                    attempts += 1
                    if attempts == 1:

                        def started() -> None:
                            clock[0] += 11
                            if outcome == "rejected":
                                state.sessions.revoke_device("acct-1", "native")
                            refreshing.set()

                        async for frame in _sse(app, token, started):
                            assert "access_expired" in frame.data or outcome == "rejected"
                            yield frame
                    else:
                        assert await secure.get(ACCESS_KEY) != token
                        finished = True
                        yield SseFrame("ping", "")

                monkeypatch.setattr(resources.transport, "stream", stream)
                delays: list[float] = []

                async def sleep(delay: float) -> None:
                    delays.append(delay)

                resources.controller.sleep = sleep
                task = asyncio.create_task(resources.controller.run("home-mac", lambda: finished))
                if outcome == "logout":
                    await asyncio.wait_for(refreshing.wait(), 1)
                    logout = asyncio.create_task(runtime.logout())
                    async with asyncio.timeout(1):
                        while runtime.resources is not None:
                            await asyncio.sleep(0)
                    release.set()
                    await logout
                    with pytest.raises(asyncio.CancelledError):
                        await task
                    assert runtime.resources is None and not runtime.input_allowed()
                    assert await secure.get(ACCESS_KEY) is None
                    assert attempts == 1
                else:
                    result = await asyncio.wait_for(task, 2)
                    assert result.revoked == (outcome == "rejected")
                    assert attempts == (2 if outcome == "refresh" else 1)
                    if outcome == "refresh":
                        assert result.granted_hosts == frozenset({"home-mac"})
                        assert len(delays) == 1
                        assert requests.count("GET /v1/me") == 2
                        assert "GET /v1/hosts" in requests
                assert not any(
                    "commands" in req or "attachments" in req or "leases" in req for req in requests
                )
            finally:
                release.set()
                await runtime.close()
                state.push.close()

    run(scenario())


def test_sustained_fanout_bounds_work_and_does_not_starve_healthy_peer(
    monkeypatch: Any, tmp_path: Path
) -> None:
    async def scenario() -> None:
        backend = JournalBackend(CommandJournal(tmp_path / "fanout.db"))
        slow, healthy = backend.follow("h", 0), backend.follow("h", 0)
        slow_read, healthy_read = (
            asyncio.ensure_future(anext(slow)),
            asyncio.ensure_future(anext(healthy)),
        )
        await asyncio.sleep(0)
        loop = asyncio.get_running_loop()
        original = loop.call_soon_threadsafe
        scheduled = 0

        def counted(callback: Any, *args: Any, **kwargs: Any) -> Any:
            nonlocal scheduled
            if getattr(callback, "__name__", "") == "_wake":
                scheduled += 1
            return original(callback, *args, **kwargs)

        monkeypatch.setattr(loop, "call_soon_threadsafe", counted)
        try:
            backend.publish("h", {"event_id": "first"})
            slow_first, healthy_first = await slow_read, await healthy_read
            assert slow_first is not None and healthy_first is not None
            assert slow_first["sequence"] == healthy_first["sequence"] == 1
            mailbox = backend._followers["h"][0]
            for seq in range(2, 2002):
                backend.publish("h", {"event_id": f"e-{seq}"})
                frame = await anext(healthy)
                assert frame is not None and frame["sequence"] == seq
                assert len(mailbox.pending) <= MAX_PENDING_METADATA
            assert mailbox.overflow
            # A follower that is never polled gets only one wake for the entire burst.
            assert scheduled <= 3
            with pytest.raises(BackendError) as overflow:
                await anext(slow)
            assert overflow.value.code == "stale_epoch" and overflow.value.retryable
            assert len(backend._followers["h"]) == 1
            burst = backend.follow("burst", 0)
            reading = asyncio.ensure_future(anext(burst))
            await asyncio.sleep(0)
            before = scheduled
            await asyncio.to_thread(
                lambda: [backend.publish("burst", {"event_id": f"b-{i}"}) for i in range(10_000)]
            )
            try:
                await reading
            except BackendError as exc:
                assert exc.code == "stale_epoch"
            else:
                with pytest.raises(BackendError) as overflow:
                    await anext(burst)
                assert overflow.value.code == "stale_epoch"
            assert scheduled == before + 1
            await cast(AsyncGenerator[dict[str, Any] | None], burst).aclose()
        finally:
            slow_read.cancel()
            healthy_read.cancel()
            await asyncio.gather(slow_read, healthy_read, return_exceptions=True)
            await cast(AsyncGenerator[dict[str, Any] | None], slow).aclose()
            await cast(AsyncGenerator[dict[str, Any] | None], healthy).aclose()
            assert backend._followers == {}
            backend.close()

    run(scenario())
