"""Server metrics: templates only, correct quantiles, a rolling 15-minute window, a scoped route."""

from __future__ import annotations

import json
import time
from pathlib import Path

from relaydeck.contracts import paths
from relaydeck.contracts.pairing import DEVICE_CAPABILITIES
from relaydeck.contracts.schemas import validate
from relaydeck.gateway.metrics import (
    MAX_UNMATCHED_SEGMENTS,
    OTHER,
    SEGMENT_CHARS,
    UNMATCHED,
    WINDOW_MINUTES,
    Metrics,
    unmatched_segment,
)
from tests.security.helpers import ALL_CAPS, auth, make_client, mint_token

HEALTH = ALL_CAPS | {"server.health"}


class Clock:
    def __init__(self, now: float = 1_800_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _routes(metrics: Metrics) -> dict[tuple[str, str], dict[str, float]]:
    snapshot = metrics.snapshot(_live())
    return {(r["method"], r["path"]): r for r in snapshot["routes"]}  # type: ignore[misc]


def _live() -> dict[str, object]:
    return {
        "open": 0,
        "controlling": 0,
        "viewing": 0,
        "connections": {
            "terminal_sockets": 0,
            "host_tunnels": 0,
            "leases_control": 0,
            "leases_view": 0,
            "event_streams": 0,
            "web_tabs": 0,
        },
        "usage_refresh": [],
    }


def test_only_route_templates_are_stored() -> None:
    client, state, _ = make_client()
    token = mint_token(state, caps=HEALTH)
    client.get("/v1/hosts/secret-host/agents?x=token", headers=auth(token))
    client.get("/nowhere/at/all?secret=1")
    reply = client.get(paths.METRICS, headers=auth(token))
    assert reply.status_code == 200
    body = reply.json()
    assert body["requests"]["total"] >= 2
    stored = {key for key, _ in state.metrics._slot(time.time()).routes.items()}
    assert ("GET", "/v1/hosts/{host_id}/agents") in stored
    text = json.dumps(state.metrics.snapshot(_live()))
    for leaked in ("secret-host", "token", "at/all", "secret=1"):
        assert leaked not in text
    assert any(path.startswith(UNMATCHED) for _, path in stored)


def test_p50_and_p95_come_from_a_known_sample() -> None:
    metrics = Metrics(clock=Clock())
    for _ in range(94):
        metrics.record_request("GET", "/v1/me", 200, 0.005)
    for _ in range(6):
        metrics.record_request("GET", "/v1/me", 200, 0.2)  # lands in the 250 ms bucket
    route = _routes(metrics)[("GET", "/v1/me")]
    assert (route["calls"], route["p50_ms"], route["p95_ms"], route["errors"]) == (100, 5, 250, 0)


def test_errors_split_into_client_and_server_and_rank_by_p95() -> None:
    metrics = Metrics(clock=Clock())
    metrics.record_request("GET", "/fast", 200, 0.001)
    metrics.record_request("GET", "/slow", 500, 2.0)
    metrics.record_request("GET", "/slow", 404, 2.0)
    snapshot = metrics.snapshot(_live())
    assert [r["path"] for r in snapshot["routes"]] == ["/slow", "/fast"]
    assert snapshot["routes"][0]["errors"] == 2
    assert (snapshot["errors"]["server_total"], snapshot["errors"]["client_total"]) == (1, 1)


def test_the_window_rolls_over_after_fifteen_minutes() -> None:
    clock = Clock()
    metrics = Metrics(clock=clock)
    metrics.record_request("GET", "/a", 200, 0.001)
    first = metrics.snapshot(_live())
    assert (
        first["requests"]["series"][-1] == 1 and len(first["requests"]["series"]) == WINDOW_MINUTES
    )
    clock.now += 60 * (WINDOW_MINUTES - 1)  # the oldest slot is still the last in the window
    assert metrics.snapshot(_live())["requests"]["series"][0] == 1
    clock.now += 60  # now it has rolled off
    later = metrics.snapshot(_live())
    assert later["requests"]["total"] == 0 and later["routes"] == []
    metrics.record_request("GET", "/b", 200, 0.001)  # the reused ring slot starts clean
    assert metrics.snapshot(_live())["requests"]["total"] == 1


def test_loop_delay_and_socket_samples_land_in_the_window() -> None:
    clock = Clock()
    metrics = Metrics(clock=clock)
    for _ in range(19):
        metrics.record_loop_delay(0.001)
    metrics.record_loop_delay(0.3)
    metrics.sample_sockets(3)
    snapshot = metrics.snapshot({**_live(), "open": 3})
    assert snapshot["loop_delay"]["p95_ms"] == 1.0 and snapshot["loop_delay"]["max_ms"] >= 300
    assert snapshot["loop_delay"]["max_at_ms"] is not None
    assert snapshot["terminal_sockets"]["series"][-1] == 3


def test_signed_out_and_without_the_scope_are_refused_and_the_reply_is_not_cached() -> None:
    client, state, _ = make_client()
    assert client.get(paths.METRICS).status_code == 401
    plain = client.get(paths.METRICS, headers=auth(mint_token(state)))
    other = client.post(
        "/v1/terminal-tickets",
        headers=auth(mint_token(state, caps=frozenset({"sessions.read"}))),
        json={"lease_id": "lease-1"},
    )
    assert plain.status_code == other.status_code
    assert plain.json()["code"] == other.json()["code"] == "unsupported_capability"
    ok = client.get(paths.METRICS, headers=auth(mint_token(state, caps=HEALTH)))
    assert ok.status_code == 200 and ok.headers["cache-control"] == "no-store"
    validate("relaydeck/gateway/v1/server-metrics", ok.json())


def test_new_pairings_get_the_capability() -> None:
    assert "server.health" in DEVICE_CAPABILITIES


def test_recording_a_request_costs_microseconds() -> None:
    metrics = Metrics()
    runs = 20_000
    started = time.perf_counter()
    for _ in range(runs):
        metrics.record_request("GET", "/v1/hosts/{host_id}/agents", 200, 0.004)
    per_call_us = (time.perf_counter() - started) / runs * 1e6
    assert per_call_us < 50, f"{per_call_us:.1f} µs per request"


def test_the_middleware_adds_under_fifty_microseconds_per_request() -> None:
    import asyncio

    from relaydeck.gateway.metrics import MetricsMiddleware

    async def app(scope, receive, send):  # type: ignore[no-untyped-def]
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def sink(message):  # type: ignore[no-untyped-def]
        return None

    async def receive():  # type: ignore[no-untyped-def]
        return {"type": "http.request"}

    async def run(target, runs: int) -> float:  # type: ignore[no-untyped-def]
        started = time.perf_counter()
        for _ in range(runs):
            await target({"type": "http", "method": "GET", "headers": []}, receive, sink)
        return (time.perf_counter() - started) / runs * 1e6

    async def measure() -> float:
        wrapped = MetricsMiddleware(app, Metrics())
        runs = 20_000
        await run(app, 1000)
        await run(wrapped, 1000)
        return await run(wrapped, runs) - await run(app, runs)

    assert asyncio.run(measure()) < 50


def test_an_unknown_method_never_mints_a_new_route_key() -> None:
    from relaydeck.gateway.metrics import MAX_ROUTES_PER_SLOT, OTHER, Metrics

    metrics = Metrics(clock=lambda: 1_000_000.0)
    for index in range(MAX_ROUTES_PER_SLOT * 3):
        metrics.record_request(f"X{index}", f"/v1/route-{index}", 200, 0.001)
    slot = metrics._slot(1_000_000.0)
    assert len(slot.routes) <= MAX_ROUTES_PER_SLOT + 1
    assert all(method == OTHER for method, _path in slot.routes)


def _unmatched(metrics: Metrics) -> dict[str, dict[str, float]]:
    return {
        f"{r['method']} {r['path']}": r  # type: ignore[misc]
        for r in metrics.snapshot(_live())["routes"]
    }


def test_unmatched_requests_are_keyed_by_first_segment_and_status_class() -> None:
    metrics = Metrics(Clock())
    metrics.record_unmatched("GET", "/assets/js/app.js", 200, 0.001)
    metrics.record_unmatched("GET", "/v1/unknown/deeper/secret-host", 404, 0.001)
    metrics.record_unmatched("GET", "/", 404, 0.001)
    keys = _unmatched(metrics)
    assert set(keys) == {
        "GET <unmatched> /assets 2xx",
        "GET <unmatched> /v1 4xx",
        "GET <unmatched> / 4xx",
    }
    assert "secret-host" not in json.dumps(metrics.snapshot(_live()))


def test_the_segment_is_clipped_and_made_safe() -> None:
    assert unmatched_segment("/ab/c") == "/ab"
    assert unmatched_segment("/a b%c\u00e9") == "/a?b?c?"
    assert unmatched_segment("/" + "x" * 80) == "/" + "x" * SEGMENT_CHARS
    assert unmatched_segment("/probe?token=1/x") == "/probe"
    assert unmatched_segment("") == "/"


def test_at_most_sixteen_unmatched_segments_a_minute() -> None:
    metrics = Metrics(Clock())
    for index in range(MAX_UNMATCHED_SEGMENTS + 5):
        metrics.record_unmatched("GET", f"/scan{index}/x", 404, 0.001)
    paths = {r["path"] for r in metrics.snapshot(_live())["routes"]}  # type: ignore[misc]
    stored = {key for key in metrics._slot(metrics._clock()).routes}
    assert len(stored) == MAX_UNMATCHED_SEGMENTS + 1
    assert ("GET", f"{UNMATCHED} {OTHER} 4xx") in stored
    assert paths  # reported


def test_a_new_minute_starts_a_new_segment_budget() -> None:
    clock = Clock()
    metrics = Metrics(clock)
    for index in range(MAX_UNMATCHED_SEGMENTS):
        metrics.record_unmatched("GET", f"/s{index}", 404, 0.001)
    clock.now += 60
    metrics.record_unmatched("GET", "/fresh", 404, 0.001)
    assert ("GET", f"{UNMATCHED} /fresh 4xx") in metrics._slot(clock.now).routes


def test_a_request_an_outer_guard_rejects_is_counted(tmp_path: Path) -> None:
    from starlette.testclient import TestClient

    from relaydeck.client.serve import WebSessionStore, build_web_app
    from relaydeck.gateway.app import MAX_JSON_BODY, create_app
    from tests.support.companion.pairing_harness import Harness
    from tests.support.pairing import ORIGIN

    h = Harness(tmp_path / "state")
    app, _ = build_web_app(
        gateway_state=h.state,
        gateway_app=create_app(h.state),
        flet_main=lambda page: None,
        public_origin=ORIGIN,
        cookie_secret=b"s" * 32,
        store=WebSessionStore(ORIGIN, b"s" * 32),
    )
    # The body-size guard sits outside the routes and answers before any of them runs.
    reply = TestClient(app).post("/blocked/deep?q=1", content=b"x" * (MAX_JSON_BODY + 1))
    assert reply.status_code == 413
    assert "POST <unmatched> /blocked 4xx" in _unmatched(h.state.metrics)
