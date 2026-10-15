"""Companion route shutdown race: route must be released before the HTTP
server is told to exit, exactly once even across a double close().

A restarting successor can reuse the live `:8445` mapping while the old
instance is still shutting down; removing the route after the server exits
would cut the successor's route. Uses the real `CompanionRuntime`
constructor and `_open_route` with fake route/server doubles — `start()` is
never called, so no sockets are bound and tailscale is never invoked.
"""

from __future__ import annotations

import asyncio
import signal
from pathlib import Path
from typing import Any

import uvicorn

from relaydeck.companion.config import load_config
from relaydeck.companion.runtime import CompanionRuntime, _route_first_server
from tests.support.companion.asgi_companion import config_file


class _Route:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def ensure(self) -> None:
        self.events.append("route_ensured")

    def remove(self) -> None:
        self.events.append("route_removed")


class _FailingRoute(_Route):
    def remove(self) -> None:
        self.events.append("route_removed")
        raise RuntimeError("synthetic tailscale failure")


class _Server:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self._should_exit = False

    @property
    def should_exit(self) -> bool:
        return self._should_exit

    @should_exit.setter
    def should_exit(self, value: bool) -> None:
        self.events.append("server_exit")
        self._should_exit = value


def _runtime(tmp_path: Path, events: list[str], route_cls: Any = _Route) -> CompanionRuntime:
    config = load_config(config_file(tmp_path, tailscale_auto=True))
    runtime = CompanionRuntime(config, route_factory=lambda: route_cls(events))
    runtime._open_route()
    runtime.server = _Server(events)
    runtime.stop = asyncio.Event()
    return runtime


def test_route_removed_before_server_exit_exactly_once(tmp_path: Path) -> None:
    async def scenario() -> None:
        events: list[str] = []
        runtime = _runtime(tmp_path, events)
        await runtime.close()
        assert events.index("route_removed") < events.index("server_exit")
        assert events.count("route_removed") == 1
        assert runtime.route is None
        assert runtime.stop is not None and runtime.stop.is_set()
        await runtime.close()
        assert events.count("route_removed") == 1
        assert events.count("server_exit") == 2  # idempotent close, single removal

    asyncio.run(scenario())


def test_failing_route_removal_still_shuts_down_server(tmp_path: Path) -> None:
    async def scenario() -> None:
        events: list[str] = []
        runtime = _runtime(tmp_path, events, _FailingRoute)
        await runtime.close()  # must not raise: removal failure is suppressed
        assert events.index("route_removed") < events.index("server_exit")
        assert runtime.route is None
        await runtime.close()
        assert events.count("route_removed") == 1  # never retried after failure

    asyncio.run(scenario())


async def _tiny_asgi_app(scope: Any, receive: Any, send: Any) -> None:
    raise AssertionError("serve() must never run in this test")


def test_signal_exit_removes_route_before_should_exit() -> None:
    events: list[str] = []
    exited_during_callback: list[bool] = []
    created: list[Any] = []

    def before_exit() -> None:
        exited_during_callback.append(created[0].should_exit)
        events.append("route_removed")

    server = _route_first_server(uvicorn.Config(app=_tiny_asgi_app), before_exit)
    created.append(server)
    assert server.should_exit is False
    server.handle_exit(signal.SIGTERM, None)  # direct call: no OS signal, no serve()
    assert events == ["route_removed"]
    assert exited_during_callback == [False]  # removal ran before the exit flag
    assert server.should_exit is True


def test_remove_route_is_idempotent_across_direct_calls_and_close(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        events: list[str] = []
        runtime = _runtime(tmp_path, events)
        runtime._remove_route()
        runtime._remove_route()
        await runtime.close()
        assert events.count("route_removed") == 1
        assert events.count("server_exit") == 1
        assert runtime.route is None

    asyncio.run(scenario())


def test_failing_before_exit_still_sets_should_exit() -> None:
    def before_exit() -> None:
        raise RuntimeError("synthetic removal failure")

    server = _route_first_server(uvicorn.Config(app=_tiny_asgi_app), before_exit)
    server.handle_exit(signal.SIGTERM, None)  # must not raise
    assert server.should_exit is True
