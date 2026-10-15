"""policy rejection stays isolated and offers browser recovery.

Use real ASGI guard/static middleware boundaries, without a loopback server.
Rendered terminal and physical-device checks remain separate evidence.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import msgpack  # type: ignore[import-untyped]
import pytest

from relaydeck.client.features.auth.web_recovery import (
    MAX_INDEX_BYTES,
    RECOVERY_PATH,
    RECOVERY_REASON,
    RECOVERY_SCRIPT,
    BrowserRecoveryMiddleware,
)
from relaydeck.client.serve import FletGuardMiddleware, WebSessionStore, _Handshake, parse_register


@pytest.mark.parametrize("identity", ["expired", "other-account", "anonymous"])
def test_rejected_reconnect_exposes_recovery_without_forwarding(identity: str) -> None:
    async def scenario() -> None:
        clock = [1_000_000]
        store = WebSessionStore("https://app.example", b"s" * 32, now_ms=lambda: clock[0])
        owner = store.create_session("a", "da", "at", "rt", clock[0] + 900_000)
        store.bind_flet(owner, "victim-page", _Handshake("", "ua", ""))
        cookie = ""
        if identity == "other-account":
            other = store.create_session("b", "db", "bt", "br", clock[0] + 900_000)
            cookie = "rd_session=" + store.sign_sid(other)
        elif identity == "expired":
            cookie = "rd_session=" + store.sign_sid(owner)
            clock[0] += store.session_ttl_s * 1000 + 1
        frame = b"\x00" + msgpack.packb([1, {"session_id": "victim-page", "page": {}}])
        incoming = iter(
            [{"type": "websocket.connect"}, {"type": "websocket.receive", "bytes": frame}]
        )
        forwarded: list[dict[str, Any]] = []
        outgoing: list[dict[str, Any]] = []

        async def receive() -> dict[str, Any]:
            return next(incoming)

        async def send(message: dict[str, Any]) -> None:
            outgoing.append(message)

        async def downstream(scope: Any, receive: Any, send: Any) -> None:
            assert (await receive())["type"] == "websocket.connect"
            await send({"type": "websocket.accept"})
            message = await receive()
            if message["type"] == "websocket.receive":
                forwarded.append(message)
            else:
                assert message == {"type": "websocket.disconnect", "code": 1008}

        scope = {
            "type": "websocket",
            "path": "/ws",
            "headers": [(b"origin", b"https://app.example"), (b"cookie", cookie.encode())],
        }
        await FletGuardMiddleware(downstream, store)(scope, receive, send)
        assert forwarded == []
        assert outgoing == [
            {"type": "websocket.accept"},
            {"type": "websocket.close", "code": 1008, "reason": RECOVERY_REASON},
        ]
        assert store.owner_of("victim-page") == owner
        assert "victim-page" not in str(outgoing)
        assert "rd_session" not in str(outgoing)

    asyncio.run(scenario())


def test_same_owner_reconnect_keeps_original_frame_and_session(monkeypatch: Any) -> None:
    from flet.fastapi import app_manager

    async def retained_session(key: str) -> Any:
        return SimpleNamespace(connection=None)

    monkeypatch.setattr(app_manager, "get_session", retained_session)

    async def scenario() -> None:
        store = WebSessionStore("https://app.example", b"s" * 32, now_ms=lambda: 1_000_000)
        owner = store.create_session("a", "da", "at", "rt", 1_900_000)
        store.bind_flet(owner, "existing-page", _Handshake("", "ua", ""))
        frame = b"\x00" + msgpack.packb([1, {"session_id": "existing-page", "page": {}}])
        incoming = iter(
            [{"type": "websocket.connect"}, {"type": "websocket.receive", "bytes": frame}]
        )
        outgoing: list[dict[str, Any]] = []

        async def receive() -> dict[str, Any]:
            return next(incoming)

        async def send(message: dict[str, Any]) -> None:
            outgoing.append(message)

        async def downstream(scope: Any, receive: Any, send: Any) -> None:
            await receive()
            assert await receive() == {"type": "websocket.receive", "bytes": frame}

        await FletGuardMiddleware(downstream, store)(
            {
                "type": "websocket",
                "path": "/ws",
                "headers": [
                    (b"origin", b"https://app.example"),
                    (b"user-agent", b"ua"),
                    (b"cookie", ("rd_session=" + store.sign_sid(owner)).encode()),
                ],
            },
            receive,
            send,
        )
        assert outgoing == [{"type": "websocket.accept"}]
        assert store.owner_of("existing-page") == owner
        assert store._closers == {}

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "attack",
    ["data-first", "text-first", "register-twice", "register-twice-bool", "expiry-race"],
)
def test_registration_gate_cannot_be_bypassed(attack: str) -> None:
    async def scenario() -> None:
        clock = [1_000_000]
        store = WebSessionStore("https://app.example", b"s" * 32, now_ms=lambda: clock[0])
        owner = store.create_session("a", "da", "at", "rt", 1_900_000)
        other = store.create_session("b", "db", "bt", "br", 1_900_000)
        store.bind_flet(owner, "victim-page", _Handshake("", "ua", ""))
        victim = b"\x00" + msgpack.packb([1, {"session_id": "victim-page", "page": {}}])
        if attack == "register-twice-bool":
            victim = b"\x00" + msgpack.packb([True, {"session_id": "victim-page", "page": {}}])
        own = b"\x00" + msgpack.packb([1, {"session_id": "", "page": {}}])
        frames: list[dict[str, Any]] = []
        cookie_sid = other
        if attack == "data-first":
            frames.append({"type": "websocket.receive", "bytes": b"\x01\x01\x00\x00\x00"})
        elif attack == "text-first":
            frames.append({"type": "websocket.receive", "text": "unregistered"})
        elif attack.startswith("register-twice"):
            frames.append({"type": "websocket.receive", "bytes": own})
        else:
            cookie_sid = owner
        frames.extend(
            [
                {"type": "websocket.receive", "bytes": victim},
                {"type": "websocket.disconnect", "code": 1000},
            ]
        )
        incoming = iter([{"type": "websocket.connect"}, *frames])
        forwarded: list[dict[str, Any]] = []
        outgoing: list[dict[str, Any]] = []

        async def receive() -> dict[str, Any]:
            if attack == "expiry-race":
                clock[0] += store.session_ttl_s * 1000 + 1
            return next(incoming)

        async def send(message: dict[str, Any]) -> None:
            outgoing.append(message)

        async def downstream(scope: Any, receive: Any, send: Any) -> None:
            await receive()
            await send({"type": "websocket.accept"})
            while True:
                message = await receive()
                if message["type"] == "websocket.disconnect":
                    break
                forwarded.append(message)

        await FletGuardMiddleware(downstream, store)(
            {
                "type": "websocket",
                "path": "/ws",
                "headers": [
                    (b"origin", b"https://app.example"),
                    (b"cookie", ("rd_session=" + store.sign_sid(cookie_sid)).encode()),
                ],
            },
            receive,
            send,
        )
        assert {"type": "websocket.receive", "bytes": victim} not in forwarded
        assert outgoing[-1]["type"] == "websocket.close"
        assert outgoing[-1]["code"] == 1008
        assert len(forwarded) == (1 if attack.startswith("register-twice") else 0)

    asyncio.run(scenario())


@pytest.mark.parametrize("session_id", [123, False, [], {}])
def test_register_does_not_coerce_invalid_identity_types(session_id: Any) -> None:
    assert parse_register(b"\x00" + msgpack.packb([1, {"session_id": session_id}])) == (False, "")


def test_register_does_not_coerce_boolean_action() -> None:
    assert parse_register(b"\x00" + msgpack.packb([True, {"session_id": "page"}])) == (False, "")


@pytest.mark.parametrize("path", ["/", "/agents/h1:a1", "/terminals/h1:t1", "/hosts/h1"])
def test_index_hook_precedes_bootstrap_and_discards_stale_cache_validators(path: str) -> None:
    async def scenario() -> None:
        outgoing: list[dict[str, Any]] = []

        async def send(message: dict[str, Any]) -> None:
            outgoing.append(message)

        async def downstream(scope: Any, receive: Any, send: Any) -> None:
            assert scope["headers"] == [(b"accept", b"text/html")]
            assert "http.response.pathsend" not in scope["extensions"]
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [
                        (b"content-type", b"text/html"),
                        (b"content-length", b"123"),
                        (b"etag", b"old"),
                        (b"last-modified", b"yesterday"),
                    ],
                }
            )
            # A split inside <head> must still inject before Flutter boots.
            await send({"type": "http.response.body", "body": b"<html><he", "more_body": True})
            await send(
                {"type": "http.response.body", "body": b'ad><script src="flutter.js"></script>'}
            )

        await BrowserRecoveryMiddleware(downstream)(
            {
                "type": "http",
                "method": "GET",
                "path": path,
                "headers": [
                    (b"if-none-match", b"old"),
                    (b"if-modified-since", b"yesterday"),
                    (b"range", b"bytes=0-5"),
                    (b"accept", b"text/html"),
                ],
                "extensions": {"http.response.pathsend": {}},
            },
            None,
            send,
        )
        body = outgoing[1]["body"]
        assert body.count(RECOVERY_PATH.encode()) == 1
        assert body.index(RECOVERY_PATH.encode()) < body.index(b"flutter.js")
        headers = dict(outgoing[0]["headers"])
        assert headers[b"content-length"] == str(len(body)).encode()
        assert headers[b"cache-control"] == b"no-store"
        assert b"etag" not in headers
        assert b"last-modified" not in headers

    asyncio.run(scenario())


def test_index_size_bound_has_no_partial_html_or_repeated_response() -> None:
    async def scenario() -> None:
        outgoing: list[dict[str, Any]] = []

        async def send(message: dict[str, Any]) -> None:
            outgoing.append(message)

        async def downstream(scope: Any, receive: Any, send: Any) -> None:
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [(b"content-type", b"text/html")],
                }
            )
            await send(
                {"type": "http.response.body", "body": b"x" * MAX_INDEX_BYTES, "more_body": True}
            )
            await send({"type": "http.response.body", "body": b"x", "more_body": True})
            await send({"type": "http.response.body", "body": b"done"})

        await BrowserRecoveryMiddleware(downstream)(
            {"type": "http", "method": "GET", "path": "/"}, None, send
        )
        assert len(outgoing) == 2
        assert outgoing[0]["status"] == 503
        assert b"connection page is unavailable" in outgoing[1]["body"]
        assert b"x" * 1024 not in outgoing[1]["body"]

    asyncio.run(scenario())


@pytest.mark.parametrize("path", ["/assets/fixture.json", "/main.dart.js", "/v1/me"])
def test_api_and_asset_responses_pass_through(path: str) -> None:
    async def scenario() -> None:
        original = [
            {"type": "http.response.start", "status": 200, "headers": []},
            {"type": "http.response.body", "body": b"unchanged"},
        ]
        outgoing: list[dict[str, Any]] = []

        async def send(message: dict[str, Any]) -> None:
            outgoing.append(message)

        async def downstream(scope: Any, receive: Any, send: Any) -> None:
            assert scope["headers"] == [(b"if-none-match", b"keep")]
            for message in original:
                await send(message)

        await BrowserRecoveryMiddleware(downstream)(
            {
                "type": "http",
                "method": "GET",
                "path": path,
                "headers": [(b"if-none-match", b"keep")],
            },
            None,
            send,
        )
        assert outgoing == original

    asyncio.run(scenario())


def test_script_behavior_with_node_dom_and_socket_doubles(tmp_path: Path) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node runtime required for browser-script behavioral checks")
    script_path = tmp_path / "recovery.js"
    script_path.write_text(RECOVERY_SCRIPT)
    harness = Path(__file__).resolve().parents[2] / "ui" / "web_recovery_browser.cjs"
    result = subprocess.run(
        [node, str(harness), str(script_path)], capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_combined_flet_index_and_recovery_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exercise the actual pinned FletStaticFiles mount via ASGI HTTP."""
    import httpx

    from relaydeck.client.serve import build_web_app
    from relaydeck.gateway.app import GatewayState, create_app
    from relaydeck.gateway.backend import FakeBackend

    (tmp_path / "index.html").write_text(
        '<html><head><script src="flutter.js"></script></head><body></body></html>'
    )
    (tmp_path / "manifest.json").write_text("{}")
    monkeypatch.setenv("FLET_WEB_PATH", str(tmp_path))

    async def scenario() -> None:
        state = GatewayState(backend=FakeBackend(), cursor_secret=b"c" * 32)

        def main(page: Any) -> None:
            raise AssertionError("An index or recovery-script request must not create a Flet page")

        app, _ = build_web_app(
            gateway_state=state,
            gateway_app=create_app(state),
            flet_main=main,
            public_origin="https://app.example",
            cookie_secret=b"s" * 32,
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://app.example"
        ) as http:
            for path in ("/", "/agents/h1:a1"):
                response = await http.get(path, headers={"If-None-Match": "old"})
                assert response.status_code == 200
                assert response.text.index(RECOVERY_PATH) < response.text.index("flutter.js")
                assert response.headers["cache-control"] == "no-store"
            script = await http.get(RECOVERY_PATH)
            assert script.status_code == 200
            assert script.text == RECOVERY_SCRIPT
            assert script.headers["content-type"].startswith("application/javascript")
            assert script.headers["x-content-type-options"] == "nosniff"
            assert script.headers["cache-control"] == "no-store"
            anonymous = await http.get("/v1/me")
            assert anonymous.status_code == 401

    asyncio.run(scenario())
