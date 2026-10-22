"""The gateway's route table and its anonymous answers are frozen.

Moving routes between modules must keep every path, method, endpoint name and
the reply a request without a sign-in gets, so no route moves or loses its
auth check unnoticed. A deliberate change updates the fixture in the same commit."""

from __future__ import annotations

import json
import re
from typing import Any

from fastapi.routing import APIRoute, APIWebSocketRoute
from fastapi.testclient import TestClient

from relaydeck.gateway.app import GatewayState, create_app
from relaydeck.gateway.backend import FakeBackend
from tests.support.paths import TESTS_DIR

FIXTURE = TESTS_DIR / "fixtures" / "gateway" / "route_table.json"
NOW = 1_000_000


def route_table() -> list[dict[str, Any]]:
    state = GatewayState(
        backend=FakeBackend(now_ms=lambda: NOW), cursor_secret=b"x" * 32, now_ms=lambda: NOW
    )
    app = create_app(state)
    client = TestClient(app)
    rows: list[dict[str, Any]] = []
    for route in app.routes:
        if isinstance(route, APIWebSocketRoute):
            rows.append({"kind": "ws", "path": route.path, "name": route.name})
        elif isinstance(route, APIRoute):
            (method,) = sorted(route.methods)
            url = re.sub(r"\{[^}]+\}", "x1", route.path)
            reply = client.request(method, url, json={} if method in ("POST", "PUT") else None)
            body = reply.json() if reply.headers.get("content-type") == "application/json" else {}
            rows.append(
                {
                    "kind": "http",
                    "method": method,
                    "path": route.path,
                    "name": route.name,
                    "anonymous": [reply.status_code, body.get("code")],
                }
            )
    return sorted(rows, key=lambda row: (row["path"], row.get("method", "")))


def test_routes_and_anonymous_replies_match_the_frozen_table() -> None:
    assert route_table() == json.loads(FIXTURE.read_text())
