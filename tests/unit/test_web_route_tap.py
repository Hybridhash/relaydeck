"""The rd_route token, the server check and the route it opens.

Synthetic data only. The transport talks to a mock transport; no network.
"""

from __future__ import annotations

import asyncio
import functools
import json
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from relaydeck.client.features.inventory.routing import format_route
from relaydeck.client.features.sync.transport import HttpxGatewayTransport, TransportError
from relaydeck.client.notifications_runtime import (
    open_web_route,
    start_web_tap,
    web_route_token,
    without_web_route,
)
from relaydeck.domain.models import scope_key

TOKEN = "wp_abcdefghijklmnopqrstuvwx"
ACCOUNT = "acct-one"
DEVICE = "dev-phone"
HOST = "home-mac"


def run(test: Callable[..., Any]) -> Callable[..., Any]:
    """Runs an async test body to completion (the repo has no async test plugin)."""

    @functools.wraps(test)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        return asyncio.run(test(*args, **kwargs))

    return wrapper


# -- the URL --------------------------------------------------------------------------------------


def test_the_token_is_read_from_the_initial_url() -> None:
    assert web_route_token(f"/?rd_route={TOKEN}") == TOKEN
    assert web_route_token(f"/app?x=1&rd_route={TOKEN}") == TOKEN


@pytest.mark.parametrize(
    "route",
    [
        "/",
        "/?rd_route=",
        "/?rd_route=wp_short",
        "/?rd_route=../../etc/passwd",
        "/?rd_route=wp_abcdefghijklmnopqrstuvwx&rd_route=wp_abcdefghijklmnopqrstuvwy",
        "/?rd_route=rdl_abcdefghijklmnopqrstuvwx",
    ],
)
def test_absent_repeated_or_malformed_tokens_are_ignored(route: str) -> None:
    assert web_route_token(route) is None


def test_the_parameter_is_stripped_and_everything_else_is_kept() -> None:
    assert without_web_route(f"/?rd_route={TOKEN}") == "/"
    assert without_web_route(f"/settings?theme=dark&rd_route={TOKEN}") == "/settings?theme=dark"
    assert without_web_route("/") == "/"
    assert without_web_route("/?rd_route=bad") == "/"


# -- the transport call ---------------------------------------------------------------------


def _transport(handler: Any) -> HttpxGatewayTransport:
    async def token() -> str:
        return "synthetic-access"

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return HttpxGatewayTransport(base_url="http://gw", token=token, client=client)


@run
async def test_the_resolve_call_posts_the_token_with_the_bearer_credential() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={"category": "attention", "host_id": HOST, "session_id": "s1"},
        )

    doc = await _transport(handler).resolve_web_route(TOKEN)
    assert doc == {"category": "attention", "host_id": HOST, "session_id": "s1"}
    assert seen[0].url.path == "/v1/push/web/resolve"
    assert json.loads(seen[0].content) == {"route": TOKEN}
    assert seen[0].headers["authorization"] == "Bearer synthetic-access"


@run
@pytest.mark.parametrize("status", [401, 404, 429, 500])
async def test_a_refused_token_raises_and_routes_nowhere(status: int) -> None:
    transport = _transport(lambda request: httpx.Response(status, json={"error": {"code": "x"}}))
    with pytest.raises(TransportError):
        await transport.resolve_web_route(TOKEN)


@run
@pytest.mark.parametrize(
    "body",
    [
        {"category": "attention", "host_id": HOST},
        {"category": "attention", "host_id": HOST, "session_id": "s1", "extra": "x"},
        {"category": "", "host_id": HOST, "session_id": "s1"},
        {"category": "attention", "host_id": 7, "session_id": "s1"},
        ["not", "an", "object"],
    ],
)
async def test_an_unexpected_shape_is_refused(body: Any) -> None:
    transport = _transport(lambda request: httpx.Response(200, json=body))
    with pytest.raises(TransportError):
        await transport.resolve_web_route(TOKEN)


# -- opening the route ----------------------------------------------------------------------


class _Controller:
    def __init__(self, agents: dict[str, Any]) -> None:
        self.state = SimpleNamespace(
            inventory=SimpleNamespace(agents=agents, tabs={}), granted_hosts=None
        )
        self.snapshots: list[str] = []

    def apply_grants(self, hosts: Any) -> None:
        self.state.granted_hosts = frozenset(item["host_id"] for item in hosts)

    def accept_snapshot(self, host: str, snapshot: Any) -> None:
        self.snapshots.append(host)


class _Bridge:
    def __init__(self) -> None:
        self.routes: list[str] = []

    def set_route(self, route: str) -> None:
        self.routes.append(route)


class _Transport:
    def __init__(
        self,
        *,
        me_hosts: list[str] | None = None,
        resolved: dict[str, str] | None = None,
        resolve_error: Exception | None = None,
        granted: list[str] | None = None,
        identity: tuple[str, str] = (ACCOUNT, DEVICE),
    ) -> None:
        self.me_hosts = [HOST] if me_hosts is None else me_hosts
        self.resolved = resolved
        self.resolve_error = resolve_error
        self.granted = [HOST] if granted is None else granted
        self.identity = identity
        self.snapshot_calls: list[str] = []

    async def me(self) -> dict[str, Any]:
        return {
            "account": self.identity[0],
            "device_id": self.identity[1],
            "hosts": self.me_hosts,
        }

    async def resolve_web_route(self, token: str) -> dict[str, str]:
        if self.resolve_error is not None:
            raise self.resolve_error
        assert self.resolved is not None
        return self.resolved

    async def hosts(self) -> dict[str, Any]:
        return {"hosts": [{"host_id": host} for host in self.granted]}

    async def snapshot(self, host: str) -> dict[str, Any]:
        self.snapshot_calls.append(host)
        return {}


def _resources(transport: _Transport, agents: dict[str, Any] | None = None) -> Any:
    return SimpleNamespace(
        transport=transport,
        controller=_Controller(agents or {}),
        bridge=_Bridge(),
        tasks=set(),
    )


@run
async def test_a_valid_token_opens_the_owned_session() -> None:
    key = scope_key(HOST, "s1")
    transport = _Transport(resolved={"category": "attention", "host_id": HOST, "session_id": "s1"})
    resources = _resources(transport, agents={key: object()})
    assert await open_web_route(resources, TOKEN, ACCOUNT, DEVICE) is True
    assert resources.bridge.routes == [format_route("agents", key)]


@run
async def test_an_unknown_session_opens_the_host_in_settings() -> None:
    transport = _Transport(
        resolved={"category": "completion", "host_id": HOST, "session_id": "gone"}
    )
    resources = _resources(transport)
    assert await open_web_route(resources, TOKEN, ACCOUNT, DEVICE) is True
    assert resources.bridge.routes == [format_route("settings", HOST)]


@run
async def test_an_expired_or_foreign_token_stays_on_console() -> None:
    transport = _Transport(resolve_error=TransportError("not_found", "x", False))
    resources = _resources(transport)
    assert await open_web_route(resources, TOKEN, ACCOUNT, DEVICE) is False
    assert resources.bridge.routes == []


@run
async def test_a_host_the_device_no_longer_holds_routes_nowhere_and_fetches_nothing() -> None:
    transport = _Transport(
        me_hosts=["other-host"],
        resolved={"category": "attention", "host_id": HOST, "session_id": "s1"},
    )
    resources = _resources(transport)
    assert await open_web_route(resources, TOKEN, ACCOUNT, DEVICE) is False
    assert resources.bridge.routes == []
    assert transport.snapshot_calls == []


@run
async def test_a_host_revoked_after_the_resolve_is_refused() -> None:
    transport = _Transport(
        resolved={"category": "attention", "host_id": HOST, "session_id": "s1"},
        granted=[],
    )
    resources = _resources(transport)
    assert await open_web_route(resources, TOKEN, ACCOUNT, DEVICE) is False
    assert resources.bridge.routes == []


@run
async def test_another_identity_on_this_page_is_refused() -> None:
    transport = _Transport(
        resolved={"category": "attention", "host_id": HOST, "session_id": "s1"},
        identity=(ACCOUNT, "dev-laptop"),
    )
    resources = _resources(transport)
    assert await open_web_route(resources, TOKEN, ACCOUNT, DEVICE) is False
    assert resources.bridge.routes == []


class _Task:
    def add_done_callback(self, callback: Any) -> None:
        return None


class _Page:
    def __init__(self, route: str) -> None:
        self.route = route
        self.went: list[str] = []
        self.tasks: list[Any] = []

    def go(self, route: str) -> None:
        self.went.append(route)

    def run_task(self, function: Any) -> Any:
        # Runs the coroutine to completion here, as the page's task would.
        asyncio.run(function())
        task = _Task()
        self.tasks.append(task)
        return task


def test_a_page_without_the_parameter_is_left_alone() -> None:
    page = _Page("/settings")
    resources = _resources(_Transport())
    start_web_tap(page, resources, ACCOUNT, DEVICE)
    assert page.went == [] and resources.bridge.routes == []


def test_a_valid_tap_strips_the_parameter_and_opens_the_owned_session() -> None:
    key = scope_key(HOST, "s1")
    page = _Page(f"/?rd_route={TOKEN}")
    transport = _Transport(resolved={"category": "attention", "host_id": HOST, "session_id": "s1"})
    resources = _resources(transport, agents={key: object()})
    start_web_tap(page, resources, ACCOUNT, DEVICE)
    assert page.went == ["/"]
    assert resources.bridge.routes == ["/", format_route("agents", key)]


def test_a_malformed_tap_strips_the_parameter_and_opens_nothing() -> None:
    page = _Page("/?rd_route=../../etc/passwd&theme=dark")
    transport = _Transport(resolved={"category": "attention", "host_id": HOST, "session_id": "s1"})
    resources = _resources(transport)
    start_web_tap(page, resources, ACCOUNT, DEVICE)
    assert page.went == ["/?theme=dark"]
    assert resources.bridge.routes == ["/?theme=dark"]
    assert transport.snapshot_calls == []
    assert page.tasks == []
