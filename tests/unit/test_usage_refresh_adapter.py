"""The gateway adapter's on-demand limits read: the POST and the fixed outcomes it maps to."""

from __future__ import annotations

from typing import Any

import pytest

from relaydeck.client.features.sync.transport import TransportError
from relaydeck.client.features.usage.source import GatewayUsageFetch
from tests.support.host_harness import run


class _Response:
    def __init__(self, status: int, body: Any) -> None:
        self.status_code = status
        self._body = body

    def json(self) -> Any:
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class _Transport:
    base_url = "https://gw.example"

    def __init__(self, response: Any) -> None:
        self.response = response
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    async def _request(self, method: str, url: str, **kwargs: Any) -> Any:
        self.calls.append((method, url, kwargs))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def _call(response: Any) -> tuple[Any, _Transport]:
    transport = _Transport(response)
    fetch = GatewayUsageFetch(transport)  # type: ignore[arg-type]
    return run(fetch.refresh_limits("host/1", "kind-a")), transport


def test_the_post_goes_to_the_refresh_route_with_the_kind() -> None:
    outcome, transport = _call(_Response(202, {"state": "started"}))
    assert outcome.state == "started"
    method, url, kwargs = transport.calls[0]
    assert method == "POST"
    assert url == "https://gw.example/v1/hosts/host%2F1/usage/limits/refresh"
    assert kwargs["json"] == {"kind": "kind-a"}


@pytest.mark.parametrize(
    ("response", "state", "wait"),
    [
        (_Response(202, {"state": "in_progress"}), "in_progress", None),
        (_Response(429, {"state": "cooldown", "retry_after_s": 90}), "cooldown", 90),
        (_Response(429, {}), "cooldown", None),
        (_Response(404, {"error": "not_configured"}), "not_configured", None),
    ],
)
def test_the_answers_map_to_fixed_outcomes(response: Any, state: str, wait: int | None) -> None:
    outcome, _ = _call(response)
    assert outcome.state == state and outcome.retry_after_s == wait


def test_a_transport_failure_is_an_error_outcome_never_raised() -> None:
    outcome, _ = _call(TransportError("host_offline", "down", True))
    assert outcome.state == "error" and outcome.code == "host_offline"
    outcome, _ = _call(_Response(503, ValueError("not json")))
    assert outcome.state == "error" and outcome.code == "usage_unavailable"


def test_a_transport_without_the_route_is_an_error_outcome() -> None:
    fetch = GatewayUsageFetch(object())  # type: ignore[arg-type]
    outcome = run(fetch.refresh_limits("h", "k"))
    assert outcome.state == "error"
