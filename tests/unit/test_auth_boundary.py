"""auth boundary (callback parsing, service flow, components)."""

from __future__ import annotations

import asyncio
from typing import Any
from urllib.parse import parse_qsl, urlparse

import pytest

from relaydeck.client.adapters.fake import FakeCredentialStore
from relaydeck.client.features.auth.callback import (
    CallbackError,
    parse_callback,
    state_matches,
)
from relaydeck.client.features.auth.components import AccountRow, LoginScreen
from relaydeck.client.features.auth.service import (
    AuthService,
    AuthSnapshot,
    AuthState,
    GatewayConfig,
)
from tests.support.host_harness import render_component, run


def test_callback_custom_scheme_ok() -> None:
    parsed = parse_callback("dev.relaydeck.app://oauth?code=c123&state=s1", https_hosts=frozenset())
    assert parsed.code == "c123"
    assert parsed.state == "s1"
    assert parsed.error is None


def test_callback_https_app_link_ok() -> None:
    parsed = parse_callback(
        "https://gw.example/app/oauth?code=c&state=s",
        https_hosts=frozenset({"gw.example"}),
    )
    assert parsed.code == "c"


def test_callback_rejections() -> None:
    with pytest.raises(CallbackError):
        parse_callback(
            "https://evil.example/app/oauth?code=c&state=s", https_hosts=frozenset({"gw.example"})
        )
    with pytest.raises(CallbackError):
        parse_callback("dev.relaydeck.app://other?code=c&state=s", https_hosts=frozenset())
    with pytest.raises(CallbackError):
        parse_callback("dev.relaydeck.app://oauth?code=c", https_hosts=frozenset())
    with pytest.raises(CallbackError):
        parse_callback("dev.relaydeck.app://oauth?state=s", https_hosts=frozenset())
    with pytest.raises(CallbackError):
        parse_callback("ftp://x/y?code=c&state=s", https_hosts=frozenset())


def test_callback_provider_error() -> None:
    parsed = parse_callback(
        "dev.relaydeck.app://oauth?state=s&error=access_denied&error_description=no",
        https_hosts=frozenset(),
    )
    assert parsed.error == "access_denied"
    assert parsed.code is None


def test_state_matches() -> None:
    assert state_matches("abc", "abc") is True
    assert state_matches("abc", "abd") is False
    assert state_matches("", "abc") is False


class _Http:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.status = 200
        self.payload: Any = {
            "access_token": "a" * 16,
            "refresh_token": "r" * 16,
            "expires_in": 900,
        }

    async def post_json(
        self, url: str, body: dict[str, Any], headers: dict[str, str] | None = None
    ) -> tuple[int, Any]:
        self.calls.append((url, body))
        self.last_headers = headers or {}
        return self.status, self.payload


def _service(
    http: _Http | None = None,
) -> tuple[AuthService, FakeCredentialStore, _Http, list[str]]:
    http = http or _Http()
    creds = FakeCredentialStore()
    opened: list[str] = []

    async def open_url(url: str) -> None:
        opened.append(url)

    service = AuthService(
        GatewayConfig("https://gw.example"),
        creds,
        http.post_json,
        open_url,
    )
    return service, creds, http, opened


def test_restore_signed_out_and_in() -> None:
    async def scenario() -> None:
        service, creds, _, _ = _service()
        snap = await service.restore()
        assert snap.state == AuthState.SIGNED_OUT
        await creds.set("refresh_token", "r")
        snap = await service.restore()
        assert snap.state == AuthState.SIGNED_IN

    asyncio.run(scenario())


def test_login_happy_path() -> None:
    async def scenario() -> None:
        service, creds, http, opened = _service()
        url = await service.begin_login()
        assert url.startswith("https://gw.example/v1/authorize?")
        query = dict(parse_qsl(urlparse(url).query))
        assert query["code_challenge_method"] == "S256"
        assert len(opened) == 1
        assert service.snapshot.state == AuthState.WAITING_CALLBACK
        state = await creds.get("oauth_state")
        assert state
        snap = await service.complete_callback(f"dev.relaydeck.app://oauth?code=C1&state={state}")
        assert snap.state == AuthState.SIGNED_IN
        assert await creds.get("access_token") == "a" * 16
        assert await creds.get("oauth_verifier") is None
        assert http.calls[0][0] == "https://gw.example/v1/token"
        assert http.calls[0][1]["grant"] == "authorization_code"

    asyncio.run(scenario())


def test_state_mismatch_keeps_tokens() -> None:
    async def scenario() -> None:
        service, creds, http, _ = _service()
        await service.begin_login()
        snap = await service.complete_callback("dev.relaydeck.app://oauth?code=C1&state=forged")
        assert snap.state == AuthState.ERROR
        assert snap.error == "state mismatch"
        assert http.calls == []
        assert await creds.get("access_token") is None

    asyncio.run(scenario())


def test_exchange_failure_surfaces() -> None:
    async def scenario() -> None:
        service, creds, http, _ = _service()
        http.status = 401
        http.payload = {"code": "unauthenticated", "message": "PKCE mismatch"}
        await service.begin_login()
        state = await creds.get("oauth_state")
        snap = await service.complete_callback(f"dev.relaydeck.app://oauth?code=C1&state={state}")
        assert snap.state == AuthState.ERROR
        assert "PKCE" in snap.error

    asyncio.run(scenario())


def test_refresh_and_logout() -> None:
    async def scenario() -> None:
        service, creds, http, _ = _service()
        await creds.set("refresh_token", "old")
        snap = await service.refresh()
        assert snap.state == AuthState.SIGNED_IN
        assert http.calls[0][1]["grant"] == "refresh_token"
        snap = await service.logout()
        assert snap.state == AuthState.SIGNED_OUT
        assert snap.notice == ""
        assert await creds.get("refresh_token") is None
        assert http.calls[-1][0] == "https://gw.example/v1/logout"
        assert http.last_headers["Authorization"] == "Bearer " + "a" * 16

    asyncio.run(scenario())


def test_untrusted_gateway_rejected() -> None:
    async def scenario() -> None:
        creds = FakeCredentialStore()
        service = AuthService(
            GatewayConfig("http://gw.example"),
            creds,
            _Http().post_json,
            lambda url: asyncio.sleep(0),
        )
        with pytest.raises(Exception, match="untrusted"):
            await service.begin_login()

    asyncio.run(scenario())


def test_login_screen_states() -> None:
    async def scenario() -> None:
        calls: list[str] = []
        app = await render_component(
            LoginScreen,
            snapshot=AuthSnapshot(AuthState.SIGNED_OUT),
            on_sign_in=lambda: calls.append("sign-in"),
            on_cancel=lambda: calls.append("cancel"),
            on_retry=lambda: calls.append("retry"),
        )
        assert app.find("auth-title").value == "RelayDeck"
        button = app.find("auth-sign-in")
        await app.session.dispatch_event(button._i, "click", "")
        assert calls == ["sign-in"]
        waiting = await render_component(
            LoginScreen,
            snapshot=AuthSnapshot(AuthState.WAITING_CALLBACK),
            on_sign_in=lambda: None,
            on_cancel=lambda: calls.append("cancel"),
            on_retry=lambda: None,
        )
        assert waiting.find("auth-waiting-text").value.startswith("Complete")
        error = await render_component(
            LoginScreen,
            snapshot=AuthSnapshot(AuthState.ERROR, error="boom", can_retry=True),
            on_sign_in=lambda: None,
            on_cancel=lambda: None,
            on_retry=lambda: calls.append("retry"),
        )
        assert error.find("auth-error-text").value == "boom"
        retry = error.find("auth-retry")
        await error.session.dispatch_event(retry._i, "click", "")
        assert calls[-1] == "retry"
        row = await render_component(
            AccountRow,
            account="ada-account",
            on_logout=lambda: calls.append("logout"),
        )
        assert row.find("account-label").value == "ada-account"

    run(scenario())


# -- logout device_ref (client sends the known push ref) ---------------------


def test_logout_sends_known_device_ref() -> None:
    async def scenario() -> None:
        service, creds, http, _ = _service()
        await creds.set("access_token", "tok-1")
        service.device_ref_provider = lambda: "dev-ref-1"
        snap = await service.logout()
        assert snap.state == AuthState.SIGNED_OUT
        assert snap.notice == ""
        assert http.calls[-1] == ("https://gw.example/v1/logout", {"device_ref": "dev-ref-1"})
        assert http.last_headers["Authorization"] == "Bearer tok-1"
        assert await creds.get("access_token") is None

        # Async providers are supported too (stored ref behind async reads).
        service2, creds2, http2, _ = _service()

        async def stored_ref() -> str | None:
            return "dev-ref-2"

        await creds2.set("access_token", "tok-2")
        service2.device_ref_provider = stored_ref
        await service2.logout()
        assert service2.snapshot.notice == ""
        assert http2.calls[-1] == ("https://gw.example/v1/logout", {"device_ref": "dev-ref-2"})

    asyncio.run(scenario())


def test_logout_without_known_ref_keeps_existing_body() -> None:
    async def scenario() -> None:
        # No provider: exact `{}` body (existing behavior).
        service, creds, http, _ = _service()
        await creds.set("access_token", "tok-1")
        await service.logout()
        assert service.snapshot.notice == ""
        assert http.calls[-1] == ("https://gw.example/v1/logout", {})

        # Unknown ref in any shape keeps the `{}` body.
        async def variants() -> None:
            for provider in (
                lambda: None,
                lambda: "",
                lambda: 0,
            ):
                http.calls.clear()
                await creds.set("access_token", "tok-1")
                service.device_ref_provider = provider
                await service.logout()
                assert service.snapshot.notice == ""
                assert http.calls[-1] == ("https://gw.example/v1/logout", {})

            async def failing() -> str | None:
                raise RuntimeError("store down")

            http.calls.clear()
            await creds.set("access_token", "tok-1")
            service.device_ref_provider = failing
            snap = await service.logout()
            assert snap.state == AuthState.SIGNED_OUT
            assert snap.notice == ""
            assert http.calls[-1] == ("https://gw.example/v1/logout", {})
            assert await creds.get("access_token") is None

        await variants()

    asyncio.run(scenario())


def test_logout_accepts_any_2xx_as_revoke_success() -> None:
    """Gateway logout contract is 200 + {"ok": true}: any 2xx means the
    server revoke succeeded, so logout clears credentials silently."""

    async def scenario() -> None:
        for status in (200, 201, 204):
            service, creds, http, _ = _service()
            http.status = status
            http.payload = {"ok": True} if status != 204 else {}
            await creds.set("access_token", "tok-1")
            await creds.set("refresh_token", "ref-1")
            snap = await service.logout()
            assert snap.state == AuthState.SIGNED_OUT
            assert snap.notice == ""
            assert await creds.get("access_token") is None
            assert await creds.get("refresh_token") is None

    asyncio.run(scenario())


def test_logout_revoke_failure_notices_but_clears() -> None:
    async def scenario() -> None:
        service, creds, http, _ = _service()
        http.status = 500
        http.payload = {"code": "internal", "message": "boom"}
        await creds.set("access_token", "tok-1")
        snap = await service.logout()
        assert snap.state == AuthState.SIGNED_OUT
        assert snap.notice == "signed out locally; server revoke failed"
        assert await creds.get("access_token") is None

    asyncio.run(scenario())


def test_logout_sync_provider_raise_keeps_empty_body() -> None:
    """A synchronously-raising device_ref provider keeps the `{}` logout
    body (mirrors the async-raise case); logout never fails on lookup."""

    async def scenario() -> None:
        service, creds, http, _ = _service()
        await creds.set("access_token", "tok-1")

        def failing() -> str | None:
            raise RuntimeError("store down")

        service.device_ref_provider = failing
        snap = await service.logout()
        assert snap.state == AuthState.SIGNED_OUT
        assert snap.notice == ""
        assert http.calls[-1] == ("https://gw.example/v1/logout", {})
        assert await creds.get("access_token") is None

    asyncio.run(scenario())
