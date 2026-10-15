"""Settings > Notifications reads this device's web push state and turns it off.

The transport calls run against a mock transport; the status wording is checked directly.
No network.
"""

from __future__ import annotations

import asyncio
import functools
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from relaydeck.client.features.settings.web_push import OFF_TEXT, ON_TEXT, status_line
from relaydeck.client.features.sync.transport import HttpxGatewayTransport, TransportError


def run(test: Callable[..., Any]) -> Callable[..., Any]:
    """Runs an async test body to completion (the repo has no async test plugin)."""

    @functools.wraps(test)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        return asyncio.run(test(*args, **kwargs))

    return wrapper


def _transport(handler: Any) -> HttpxGatewayTransport:
    async def token() -> str:
        return "synthetic-access"

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return HttpxGatewayTransport(base_url="http://gw", token=token, client=client)


def test_the_status_line_says_on_for_this_device_or_off() -> None:
    assert status_line(True) == "Notifications are on for this device"
    assert status_line(False).startswith("Notifications are off for this device")
    assert ON_TEXT == "Notifications are on for this device"
    assert "bar at the top" in OFF_TEXT and "Add to Home Screen" in OFF_TEXT


@run
async def test_the_state_call_reads_enabled_and_subscribed_with_the_bearer() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "publicKey": "synthetic-key",
                "enabled": True,
                "subscribed": True,
                "csrf": "synthetic-csrf",
                "choice": ["blocked", "done"],
            },
        )

    state = await _transport(handler).web_push_state()
    assert state == {"enabled": True, "subscribed": True}
    assert seen[0].method == "GET" and seen[0].url.path == "/v1/push/web/key"
    assert seen[0].headers["authorization"] == "Bearer synthetic-access"


@run
async def test_a_state_answer_that_is_not_the_expected_shape_is_refused() -> None:
    transport = _transport(
        lambda request: httpx.Response(200, json={"enabled": True, "subscribed": "yes"})
    )
    with pytest.raises(TransportError):
        await transport.web_push_state()


@run
async def test_web_push_off_on_the_companion_is_a_refusal_the_panel_hides_on() -> None:
    transport = _transport(lambda request: httpx.Response(404, json={"error": {"code": "x"}}))
    with pytest.raises(TransportError):
        await transport.web_push_state()


@run
async def test_turn_off_sends_the_same_delete_with_the_bearer_and_no_body() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(204)

    await _transport(handler).web_push_disable()
    assert seen[0].method == "DELETE"
    assert seen[0].url.path == "/v1/push/web/subscription"
    assert seen[0].headers["authorization"] == "Bearer synthetic-access"
    assert seen[0].content == b""


@run
async def test_a_turn_off_that_is_not_confirmed_raises() -> None:
    transport = _transport(lambda request: httpx.Response(500))
    with pytest.raises(TransportError):
        await transport.web_push_disable()
