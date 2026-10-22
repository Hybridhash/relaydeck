"""Production hardening that leaves replies alone: no-store headers, server limits, shutdown,
logging of swallowed failures and lifespan cleanup."""

from __future__ import annotations

import ast
import asyncio
import logging
from pathlib import Path
from typing import Any

import httpx
import pytest

from relaydeck.companion.pictures import PictureStore
from relaydeck.connector.pubkey import generate
from relaydeck.contracts.pairing import DEVICE_CAPABILITIES
from relaydeck.gateway import cli as gateway_cli
from relaydeck.gateway.app import create_app
from relaydeck.gateway.bounds import GRACEFUL_SHUTDOWN_S, WS_MAX_SIZE
from tests.security.helpers import attach, auth, lease, make_client, make_state, mint_token
from tests.support.companion.enrollment import _redeem_body
from tests.support.pictures.uploads import picture

SRC = Path(__file__).resolve().parents[2] / "src" / "relaydeck"


def test_secret_bearing_replies_are_not_cached() -> None:
    client, state, _ = make_client()
    token = mint_token(state)
    _, refresh = state.sessions.create(
        "acct-1", frozenset({"sessions.read"}), frozenset({"home-mac"}), 1_000_000
    )
    refreshed = client.post("/v1/token", json={"grant": "refresh_token", "refresh_token": refresh})
    assert refreshed.status_code == 200
    assert refreshed.headers["cache-control"] == "no-store"

    leased = lease(client, token, attach(client, token)["attachment_id"])
    issued = client.post(
        "/v1/terminal-tickets", headers=auth(token), json={"lease_id": leased["lease_id"]}
    )
    assert issued.status_code == 200
    assert issued.headers["cache-control"] == "no-store"

    seed, public = generate()
    created = client.post(
        "/v1/enrollments", headers=auth(token), json={"host_label": "Mac", "pubkey": public}
    )
    assert created.status_code == 201
    assert created.headers["cache-control"] == "no-store"
    invitation = created.json()
    status = client.get(f"/v1/enrollments/{invitation['id']}", headers=auth(token))
    assert status.status_code == 200
    assert status.headers["cache-control"] == "no-store"
    redeemed = client.post(
        f"/v1/enrollments/{invitation['id']}/redeem",
        json=_redeem_body(seed, public, invitation["id"], invitation["code"], "new-mac"),
    )
    assert redeemed.status_code == 200
    assert redeemed.headers["cache-control"] == "no-store"


def test_logout_without_a_token_is_the_same_unauthenticated_reply() -> None:
    client, _, _ = make_client()
    reply = client.post("/v1/logout")
    assert reply.status_code == 401
    assert reply.json() == {
        "code": "unauthenticated",
        "message": "missing or expired token",
        "retryable": False,
    }


def test_standalone_gateway_serve_passes_server_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    import uvicorn

    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: calls.append(kwargs))
    assert gateway_cli.main(["serve", "--cursor-secret", "c" * 32]) == 0
    (kwargs,) = calls
    assert kwargs["ws_max_size"] == WS_MAX_SIZE == 2 * 1024 * 1024
    assert kwargs["timeout_graceful_shutdown"] == GRACEFUL_SHUTDOWN_S == 5
    assert kwargs["proxy_headers"] is False


@pytest.mark.parametrize(
    ("module", "keywords"),
    [
        ("client/serve.py", {"ws_max_size", "timeout_graceful_shutdown", "proxy_headers"}),
        ("companion/runtime.py", {"ws_max_size", "timeout_graceful_shutdown", "proxy_headers"}),
    ],
)
def test_other_uvicorn_call_sites_pass_the_same_limits(module: str, keywords: set[str]) -> None:
    tree = ast.parse((SRC / module).read_text())
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in ("run", "Config")
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "uvicorn"
    ]
    assert calls
    for call in calls:
        assert keywords <= {keyword.arg for keyword in call.keywords}, module
        by_name = {keyword.arg: keyword.value for keyword in call.keywords}
        assert ast.unparse(by_name["ws_max_size"]) == "WS_MAX_SIZE"
        assert ast.unparse(by_name["timeout_graceful_shutdown"]) == "GRACEFUL_SHUTDOWN_S"


def test_event_stream_ends_when_the_gateway_stops_serving() -> None:
    async def scenario() -> bool:
        state, _ = make_state()
        token = mint_token(state)
        state.started = True  # the lifespan is skipped when the app is called directly
        app = create_app(state)
        started = asyncio.Event()

        requests = [{"type": "http.request", "body": b"", "more_body": False}]

        async def receive() -> dict[str, Any]:
            if requests:
                return requests.pop()
            await asyncio.sleep(3600)
            return {"type": "http.disconnect"}

        async def send(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                started.set()

        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/v1/events",
            "raw_path": b"/v1/events",
            "query_string": b"host_id=home-mac",
            "headers": [(b"authorization", f"Bearer {token}".encode())],
            "client": ("127.0.0.1", 1),
            "server": ("test", 80),
        }
        serving = asyncio.create_task(app(scope, receive, send))
        await asyncio.wait_for(started.wait(), 5)
        state.started = False
        done, _ = await asyncio.wait({serving}, timeout=5)
        serving.cancel()
        return bool(done)

    assert asyncio.run(scenario()), "the event stream outlived the gateway"


def test_a_swallowed_picture_upload_failure_is_logged_and_the_reply_is_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def scenario() -> None:
        state, _ = make_state()
        store = state.pictures = PictureStore(tmp_path)
        token, _ = state.sessions.create(
            "a", DEVICE_CAPABILITIES, {"home-mac"}, state.now_ms(), device_id="one"
        )

        def broken(data: bytes, now: int) -> tuple[str, str]:
            raise RuntimeError("decoder defect")

        monkeypatch.setattr(store, "save_with_preview", broken)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(state)), base_url="http://test"
        ) as client:
            grant = (
                await client.post("/v1/pictures", headers=auth(token), json={"host_id": "home-mac"})
            ).json()
            response = await client.put(grant["upload_url"], headers=auth(token), content=picture())
        assert response.status_code == 400
        assert response.json()["message"] == "Picture upload failed; try JPEG or PNG."

    with caplog.at_level(logging.ERROR):
        asyncio.run(scenario())
    # Only the exception type is logged; its text could carry paths or content.
    assert "picture_upload_failed error=RuntimeError" in caplog.text
    assert "decoder defect" not in caplog.text


def test_a_failure_while_starting_cancels_the_tasks_already_started(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from relaydeck.gateway import app as gateway_app

    def broken_start(self: Any) -> None:
        raise RuntimeError("monitor could not start")

    monkeypatch.setattr(gateway_app.LoopWatch, "start", broken_start)

    async def scenario() -> int:
        state, _ = make_state()
        app = create_app(state)
        with pytest.raises(RuntimeError, match="monitor could not start"):
            async with app.router.lifespan_context(app):
                pass
        await asyncio.sleep(0)
        return len(asyncio.all_tasks()) - 1

    assert asyncio.run(scenario()) == 0
