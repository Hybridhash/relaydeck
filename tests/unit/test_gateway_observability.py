"""One error path, request IDs, /readyz, the docs switch and the shared logging config."""

from __future__ import annotations

import logging
import subprocess
import sys
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

from relaydeck.gateway.app import GatewayState, create_app
from relaydeck.gateway.backend import FakeBackend
from relaydeck.gateway.request_id import current_request_id
from relaydeck.persistence_contracts import StorageUnavailable

NOW = 1_000_000
SECRET_BODY = "PRIVATE_BODY_MARKER"


def _state(**over: Any) -> GatewayState:
    return GatewayState(
        backend=FakeBackend(now_ms=lambda: NOW), cursor_secret=b"x" * 32, now_ms=lambda: NOW, **over
    )


class _Body(BaseModel):
    count: int


def _app_with_probe_routes(state: GatewayState | None = None) -> Any:
    app = create_app(state or _state())

    @app.post("/__typed")
    async def typed(body: _Body) -> dict[str, int]:
        return {"count": body.count}

    @app.get("/__boom")
    async def boom() -> None:
        raise RuntimeError(f"secret detail {SECRET_BODY}")

    @app.get("/__storage")
    async def storage() -> None:
        raise StorageUnavailable("unavailable")

    @app.get("/__rid")
    async def rid() -> dict[str, str]:
        return {"id": current_request_id()}

    return app


def _lines(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == "relaydeck.gateway.errors"]


def test_a_valid_request_id_is_echoed_and_an_invalid_one_replaced() -> None:
    client = TestClient(_app_with_probe_routes())
    valid = client.get("/__rid", headers={"X-Request-ID": "abc-12345678"})
    assert valid.headers["x-request-id"] == "abc-12345678"
    assert valid.json() == {"id": "abc-12345678"}
    for bad in ("short", "has space in it!", "x" * 65, "id_with_underscore_1"):
        reply = client.get("/__rid", headers={"X-Request-ID": bad})
        generated = reply.headers["x-request-id"]
        assert generated != bad and len(generated) == 32 and reply.json() == {"id": generated}
    assert client.get("/__rid").headers["x-request-id"]


def test_a_gateway_error_returns_the_envelope_and_logs_one_line(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    client = TestClient(_app_with_probe_routes())
    reply = client.get("/v1/me", headers={"X-Request-ID": "req-aaaa1111"})
    assert reply.status_code == 401 and reply.json()["code"] == "unauthenticated"
    (record,) = _lines(caplog)
    assert record.levelno == logging.INFO
    assert record.getMessage() == (
        "gateway_error code=unauthenticated status=401 method=GET route=/v1/me "
        "request_id=req-aaaa1111"
    )


def test_a_validation_error_is_a_malformed_envelope_without_the_body(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    client = TestClient(_app_with_probe_routes())
    reply = client.post(
        "/__typed", json={"count": SECRET_BODY}, headers={"X-Request-ID": "req-bbbb2222"}
    )
    body = reply.json()
    assert reply.status_code == 400 and body["code"] == "forbidden"
    assert body["details"] == {"reason": "malformed"} and SECRET_BODY not in reply.text
    (record,) = _lines(caplog)
    assert record.getMessage() == (
        "gateway_error code=forbidden status=400 method=POST route=/__typed request_id=req-bbbb2222"
    )
    assert SECRET_BODY not in caplog.text


def test_an_unexpected_exception_returns_internal_and_logs_one_line(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    client = TestClient(_app_with_probe_routes(), raise_server_exceptions=False)
    reply = client.get("/__boom", headers={"X-Request-ID": "req-cccc3333"})
    assert reply.status_code == 500 and reply.json()["code"] == "internal"
    assert reply.headers["x-request-id"] == "req-cccc3333" and SECRET_BODY not in reply.text
    (record,) = _lines(caplog)
    assert record.levelno == logging.ERROR
    assert record.getMessage() == (
        "unhandled_error method=GET route=/__boom request_id=req-cccc3333"
    )


def test_storage_unavailable_keeps_its_envelope_and_logs_one_line(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    client = TestClient(_app_with_probe_routes())
    reply = client.get("/__storage", headers={"X-Request-ID": "req-dddd4444"})
    assert reply.status_code == 503
    body = reply.json()
    assert body["code"] == "unauthenticated"
    assert body["details"] == {"reason": "storage_unavailable", "storage_reason": "unavailable"}
    (record,) = _lines(caplog)
    assert record.levelno == logging.ERROR
    assert record.getMessage() == (
        "gateway_error code=unauthenticated status=503 method=GET route=/__storage "
        "request_id=req-dddd4444"
    )


def test_readyz_is_503_until_a_host_tunnel_is_live() -> None:
    state = _state()
    token = state.hosts.enroll("home-mac", account="acct-1")
    with TestClient(create_app(state)) as client:
        before = client.get("/readyz")
        assert before.status_code == 503
        assert before.json() == {"ready": False, "hosts_connected": 0}
        with client.websocket_connect(
            "/v1/connectors", headers={"authorization": f"Bearer {token}"}
        ):
            after = client.get("/readyz")
            assert after.status_code == 200
            assert after.json() == {"ready": True, "hosts_connected": 1}
    assert client.get("/healthz").status_code == 200


def test_docs_routes_exist_only_with_the_debug_flag() -> None:
    off = TestClient(create_app(_state()))
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert off.get(path).status_code == 404
    on = TestClient(create_app(_state(debug_docs=True)))
    assert on.get("/openapi.json").status_code == 200


def test_logging_config_is_a_valid_dict_config() -> None:
    # Applied in a child process: dictConfig changes global logger state.
    code = (
        "import logging, logging.config\n"
        "from relaydeck.logging_setup import logging_config\n"
        "logging.config.dictConfig(logging_config('INFO'))\n"
        "logging.getLogger('relaydeck.gateway.x').info('probe')\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False, timeout=60
    )
    assert done.returncode == 0, done.stderr
    assert "rid=- probe" in done.stderr and "relaydeck.gateway.x" in done.stderr
