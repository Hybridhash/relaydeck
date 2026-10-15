"""The choice mapping, loads, saves, and the offline and error states.

The state logic is plain Python, so it is tested without Flet. Transport calls run against a mock
transport. No network.
"""

from __future__ import annotations

import asyncio
import functools
import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from relaydeck.client.features.settings.web_push import (
    CHOICES,
    DEFAULT_CHOICE,
    PrefsState,
    categories_for,
    choice_for,
    load_choice,
    save_choice,
)
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


# -- the mapping -------------------------------------------------------------------------


def test_the_default_choice_is_all() -> None:
    assert DEFAULT_CHOICE == "all"
    assert categories_for("all") == ["blocked", "done"]


def test_every_choice_maps_to_the_categories_it_names() -> None:
    assert [(key, categories_for(key)) for key, _cats, _label in CHOICES] == [
        ("all", ["blocked", "done"]),
        ("blocked", ["blocked"]),
        ("done", ["done"]),
        ("none", []),
    ]


@pytest.mark.parametrize(
    ("categories", "choice"),
    [
        (["blocked", "done"], "all"),
        (["done", "blocked"], "all"),
        (["blocked"], "blocked"),
        (["done"], "done"),
        ([], "none"),
        (["blocked", "secret"], None),
    ],
)
def test_a_stored_set_is_shown_as_its_choice(categories: list[str], choice: str | None) -> None:
    assert choice_for(categories) == choice


def test_an_unknown_choice_is_refused() -> None:
    with pytest.raises(ValueError):
        categories_for("everything")


def test_the_state_is_enabled_only_when_the_choice_is_known_and_nothing_is_pending() -> None:
    assert PrefsState("all", "ready").enabled is True
    assert PrefsState("all", "saved").enabled is True
    assert PrefsState("all", "error").enabled is True
    assert PrefsState(None, "loading").enabled is False
    assert PrefsState("all", "saving").enabled is False
    assert PrefsState(None, "offline").enabled is False


# -- loads -------------------------------------------------------------------------------


class FakeTransport:
    def __init__(
        self,
        *,
        stored: list[str] | None = None,
        load_error: BaseException | None = None,
        save_error: BaseException | None = None,
    ) -> None:
        self.stored = ["blocked", "done"] if stored is None else stored
        self.load_error = load_error
        self.save_error = save_error
        self.saves: list[list[str]] = []

    async def web_push_preferences(self) -> list[str]:
        if self.load_error is not None:
            raise self.load_error
        return list(self.stored)

    async def set_web_push_preferences(self, categories: list[str]) -> list[str]:
        self.saves.append(list(categories))
        if self.save_error is not None:
            raise self.save_error
        self.stored = list(categories)
        return list(categories)


@run
async def test_a_load_shows_the_saved_choice() -> None:
    state = await load_choice(FakeTransport(stored=["done"]))
    assert state == PrefsState("done", "ready")


@run
async def test_an_offline_load_says_so_and_disables_the_choice() -> None:
    error = TransportError("host_offline", "no route", True)
    state = await load_choice(FakeTransport(load_error=error))
    assert state.status == "offline" and state.enabled is False
    assert "Offline" in state.text


@run
async def test_a_refused_load_is_an_error_not_offline() -> None:
    error = TransportError("upstream_incompatible", "invalid notification choice", False)
    state = await load_choice(FakeTransport(load_error=error))
    assert state.status == "error" and state.choice is None


@run
async def test_an_unexpected_failure_on_load_shows_the_error_state() -> None:
    state = await load_choice(FakeTransport(load_error=RuntimeError("boom")))
    assert state.status == "error"


# -- saves -------------------------------------------------------------------------------


@run
async def test_a_save_stores_the_choice_and_reports_saved() -> None:
    transport = FakeTransport(stored=["blocked", "done"])
    state = await save_choice(transport, PrefsState("all", "ready"), "blocked")
    assert transport.saves == [["blocked"]]
    assert state == PrefsState("blocked", "saved")
    assert state.text == "Saved"


@run
async def test_a_failed_save_keeps_the_last_known_choice_and_says_so() -> None:
    transport = FakeTransport(save_error=TransportError("upstream_incompatible", "x", True))
    state = await save_choice(transport, PrefsState("done", "ready"), "none")
    assert state == PrefsState("done", "error")
    assert state.text == "Could not save. Try again."


@run
async def test_a_save_while_offline_keeps_the_choice_and_reports_offline() -> None:
    transport = FakeTransport(save_error=TransportError("host_offline", "no route", True))
    state = await save_choice(transport, PrefsState("all", "ready"), "none")
    assert state.status == "offline" and state.choice == "all"


@run
async def test_an_unknown_choice_is_never_sent() -> None:
    transport = FakeTransport()
    with pytest.raises(ValueError):
        await save_choice(transport, PrefsState("all", "ready"), "everything")
    assert transport.saves == []


# -- the transport calls -----------------------------------------------------------------


@run
async def test_the_preference_read_uses_the_bearer_and_checks_the_shape() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"categories": ["done"]})

    assert await _transport(handler).web_push_preferences() == ["done"]
    assert seen[0].method == "GET" and seen[0].url.path == "/v1/push/prefs"
    assert seen[0].headers["authorization"] == "Bearer synthetic-access"


@run
@pytest.mark.parametrize(
    "body",
    [
        {"categories": ["attention"]},  # the internal name is never shown to the client
        {"categories": ["blocked", 7]},
        {"categories": "blocked"},
        {"categories": [], "extra": True},
    ],
)
async def test_an_unexpected_read_shape_is_refused(body: Any) -> None:
    transport = _transport(lambda request: httpx.Response(200, json=body))
    with pytest.raises(TransportError):
        await transport.web_push_preferences()


@run
async def test_the_save_sends_the_ui_names_as_json() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"categories": ["blocked"]})

    assert await _transport(handler).set_web_push_preferences(["blocked"]) == ["blocked"]
    assert seen[0].method == "PUT"
    assert json.loads(seen[0].content) == {"categories": ["blocked"]}
    assert seen[0].headers["authorization"] == "Bearer synthetic-access"


@run
async def test_a_save_that_the_companion_refuses_raises() -> None:
    transport = _transport(lambda request: httpx.Response(400, json={"error": {"code": "invalid"}}))
    with pytest.raises(TransportError):
        await transport.set_web_push_preferences(["blocked"])
