"""Fresh web/native login through gateway + explicitly synthetic signed OIDC."""

from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import flet as ft
import httpx
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from relaydeck.client.features.auth.service import ACCESS_KEY, AuthState
from relaydeck.client.notifications_runtime import NotificationRuntime
from relaydeck.client.packaged_runtime import PackagedRuntime
from relaydeck.client.serve import COOKIE_NAME, WebSessionStore, build_web_app
from relaydeck.companion.config import load_config
from relaydeck.companion.runtime import CompanionRuntime
from relaydeck.gateway.app import create_app
from relaydeck.gateway.authorization import (
    FLOW_LIMIT,
    FLOW_TTL_MS,
    AuthorizationError,
    OidcProvider,
)
from relaydeck.gateway.identity import pkce_challenge
from tests.security.helpers import ALL_CAPS, NOW, make_state
from tests.support.companion.asgi_companion import AsgiServer, config_file
from tests.support.fakes import FakeClipboard, FakeCredentialStore, FakeNotifications
from tests.support.host_harness import render_component, run
from tests.support.ui.packaged import Preferences


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


class SyntheticOidc:
    """Test-only provider. Private key, account and codes never enter production config."""

    def __init__(self, fault: str = "") -> None:
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.pending: dict[str, dict[str, str]] = {}
        self.fault = fault
        self.exchanges = 0
        self.config = {
            "issuer": "https://identity.synthetic.invalid",
            "authorization_endpoint": "https://identity.synthetic.invalid/authorize",
            "token_endpoint": "https://identity.synthetic.invalid/token",
            "jwks_uri": "https://identity.synthetic.invalid/keys",
            "client_id": "synthetic-relaydeck",
            "grants": {
                "synthetic-subject": {
                    "account": "acct-1",
                    "capabilities": list(ALL_CAPS),
                    "hosts": ["home-mac"],
                }
            },
        }

    def request(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/authorize":
            query = dict(request.url.params)
            code = f"provider-code-{len(self.pending)}"
            self.pending[code] = query
            return httpx.Response(
                302,
                headers={
                    "Location": query["redirect_uri"]
                    + "?"
                    + urlencode(
                        {
                            "code": code,
                            "state": query["state"],
                        }
                    )
                },
            )
        if request.url.path == "/token":
            self.exchanges += 1
            query = {key: values[0] for key, values in parse_qs(request.content.decode()).items()}
            pending = self.pending.pop(query["code"])
            assert query["redirect_uri"] == pending["redirect_uri"]
            assert pkce_challenge(query["code_verifier"]) == pending["code_challenge"]
            claims: dict[str, Any] = {
                "iss": self.config["issuer"],
                "aud": self.config["client_id"],
                "sub": "synthetic-subject",
                "nonce": pending["nonce"],
                "iat": NOW // 1000,
                "exp": NOW // 1000 + 300,
            }
            if self.fault == "nonce":
                claims["nonce"] = "wrong-nonce"
            if self.fault == "issuer":
                claims["iss"] = "https://foreign.invalid"
            if self.fault == "audience":
                claims["aud"] = "other-client"
            if self.fault == "extra-audience":
                claims["aud"] = [claims["aud"], "foreign"]
            if self.fault == "azp":
                claims["azp"] = "foreign"
            if self.fault == "expired":
                claims["exp"] = NOW // 1000 - 1
            if self.fault == "future":
                claims["iat"] = NOW // 1000 + 60
            if self.fault == "foreign":
                claims["sub"] = "unapproved-subject"
            head = _b64(json.dumps({"alg": "RS256", "kid": "synthetic-key"}).encode())
            if self.fault == "algorithm":
                head = _b64(json.dumps({"alg": "none", "kid": "synthetic-key"}).encode())
            payload = head + "." + _b64(json.dumps(claims).encode())
            signature = self.key.sign(payload.encode(), padding.PKCS1v15(), hashes.SHA256())
            if self.fault == "signature":
                signature = b"wrong-signature"
            return httpx.Response(200, json={"id_token": payload + "." + _b64(signature)})
        assert request.url.path == "/keys"
        key = self.key.public_key().public_numbers()
        return httpx.Response(
            200,
            json={
                "keys": [
                    {
                        "kty": "RSA",
                        "alg": "RS256",
                        "kid": "synthetic-key",
                        "use": "sig",
                        "n": _b64(key.n.to_bytes(256)),
                        "e": _b64(key.e.to_bytes(3)),
                    }
                ]
            },
        )


async def _redirect(
    http: httpx.AsyncClient, provider: httpx.AsyncClient, url: str
) -> httpx.Response:
    authorize = await http.get(url)
    assert authorize.status_code == 302
    external = await provider.get(authorize.headers["location"])
    callback = await http.get(external.headers["location"])
    return callback


@pytest.mark.parametrize("platform", ["web", "native"])
def test_fresh_no_token_login_uses_actual_authorize_provider_callback_and_identity(
    platform: str,
) -> None:
    async def scenario() -> None:
        state, _ = make_state()
        state.public_origin = "https://gw"
        double = SyntheticOidc()
        async with httpx.AsyncClient(transport=httpx.MockTransport(double.request)) as provider:
            state.identity_provider = OidcProvider(
                double.config, client=provider, clock=lambda: NOW
            )
            gateway = create_app(state)
            store = WebSessionStore("https://gw", b"s" * 32, now_ms=lambda: NOW)

            async def main(page: ft.Page) -> None:
                page.add(ft.Text("Synthetic login page"))

            app = (
                build_web_app(
                    store=store,
                    gateway_app=gateway,
                    gateway_state=state,
                    flet_main=main,
                    public_origin="https://gw",
                    cookie_secret=b"s" * 32,
                )[0]
                if platform == "web"
                else gateway
            )
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="https://gw"
            ) as http:
                assert state.sessions._by_access == {} and store._sessions == {}
                if platform == "web":
                    _, url = store.begin_login("https://gw")
                    callback = await _redirect(http, provider, url)
                    assert callback.status_code == 302
                    completed = await http.get(callback.headers["location"])
                    assert completed.status_code == 302 and completed.headers["location"] == "/"
                    assert "HttpOnly" in completed.headers["set-cookie"]
                    assert "Secure" in completed.headers["set-cookie"]
                    assert http.cookies.get(COOKIE_NAME) is not None
                    assert len(store._sessions) == 1
                    assert next(iter(store._sessions.values())).identity.account == "acct-1"
                    assert "access_token" not in completed.text
                else:
                    secure = FakeCredentialStore()
                    opened: list[str] = []

                    async def browser(url: str) -> None:
                        opened.append(url)

                    mounted = await render_component(lambda: ft.Text("Fresh native"))
                    runtime = PackagedRuntime(
                        mounted.page,
                        secure,
                        Preferences({}),
                        browser,
                        lambda: None,
                        client=http,
                        clipboard=FakeClipboard(),
                    )
                    notifications = NotificationRuntime(FakeNotifications(), runtime)
                    runtime.notifications = notifications
                    try:
                        await runtime.configure("https://gw")
                        assert runtime.auth is not None
                        assert runtime.auth.snapshot.state == AuthState.SIGNED_OUT
                        assert await secure.get(ACCESS_KEY) is None
                        await runtime.sign_in()
                        callback = await _redirect(http, provider, opened[0])
                        assert callback.status_code == 302
                        await notifications.app_link(callback.headers["location"])
                        assert runtime.auth is not None
                        assert runtime.auth.snapshot.state.value == AuthState.SIGNED_IN.value
                        assert runtime.resources is not None
                        assert runtime.resources.props["account"] == "acct-1"
                        assert await secure.get(ACCESS_KEY) is not None
                        assert runtime.input_allowed()
                        await notifications.app_link(callback.headers["location"])
                    finally:
                        await runtime.close()
                assert double.exchanges == 1
                assert not state.authorization.pending and not state.pending_codes
        state.push.close()

    run(scenario())


@pytest.mark.parametrize(
    "fault",
    [
        "nonce",
        "issuer",
        "audience",
        "extra-audience",
        "azp",
        "expired",
        "future",
        "foreign",
        "algorithm",
        "signature",
    ],
)
def test_untrusted_identity_cannot_mint_gateway_code_or_session(fault: str) -> None:
    async def scenario() -> None:
        state, _ = make_state()
        state.public_origin = "https://gw"
        double = SyntheticOidc(fault)
        async with httpx.AsyncClient(transport=httpx.MockTransport(double.request)) as provider:
            state.identity_provider = OidcProvider(
                double.config, client=provider, clock=lambda: NOW
            )
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=create_app(state)), base_url="https://gw"
            ) as http:
                store = WebSessionStore("https://gw", b"s" * 32)
                _, url = store.begin_login("https://gw")
                callback = await _redirect(http, provider, url)
                assert callback.status_code == 401
                assert not state.pending_codes and not state.sessions._by_access
                assert not state.authorization.pending and double.exchanges == 1
                assert fault not in callback.text
        state.push.close()

    run(scenario())


def test_unconfigured_invalid_redirect_duplicate_query_replay_and_pkce_fail_closed() -> None:
    async def scenario() -> None:
        state, _ = make_state()
        state.public_origin = "https://gw"
        double = SyntheticOidc()
        async with httpx.AsyncClient(transport=httpx.MockTransport(double.request)) as provider:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=create_app(state)), base_url="https://gw"
            ) as http:
                assert (await http.get("/v1/authorize")).status_code == 503
                state.identity_provider = OidcProvider(
                    double.config, client=provider, clock=lambda: NOW
                )
                store = WebSessionStore("https://gw", b"s" * 32)
                _, url = store.begin_login("https://gw")
                query = dict(httpx.URL(url).params)
                for patch in (
                    {"redirect_uri": "https://foreign.invalid/callback"},
                    {"account": "foreign"},
                    {"code_challenge_method": "plain"},
                ):
                    result = await http.get("/v1/authorize", params=dict(query, **patch))
                    assert result.status_code == 400
                assert (await http.get(url + "&state=duplicate")).status_code == 400
                callback = await _redirect(http, provider, url)
                code = parse_qs(urlsplit(callback.headers["location"]).query)["code"][0]
                rejected = await http.post(
                    "/v1/token",
                    json={"grant": "authorization_code", "code": code, "verifier": "w" * 64},
                )
                assert rejected.status_code == 401
                assert (
                    await http.post(
                        "/v1/token",
                        json={"grant": "authorization_code", "code": code, "verifier": "w" * 64},
                    )
                ).status_code == 401
                assert not state.sessions._by_access and not state.pending_codes
        state.push.close()

    run(scenario())


def test_identity_config_is_explicit_and_owner_only(tmp_path: Path) -> None:
    async def scenario() -> None:
        path = tmp_path / "identity.json"
        path.write_text(json.dumps(SyntheticOidc().config))
        path.chmod(0o644)
        with pytest.raises(ValueError, match="owner-only"):
            OidcProvider.from_file(path, lambda: NOW)
        path.chmod(0o600)
        provider = OidcProvider.from_file(path, lambda: NOW)
        await provider.close()
        with pytest.raises(AuthorizationError):
            OidcProvider(
                dict(SyntheticOidc().config, token_endpoint="http://untrusted/token"),
                clock=lambda: NOW,
            )

    run(scenario())


def test_web_refresh_coalesces_and_cannot_resurrect_retired_cookie() -> None:
    async def scenario() -> None:
        store = WebSessionStore("https://gw", b"s" * 32, now_ms=lambda: NOW)
        sid = store.create_session("acct-1", "native", "old-access", "old-refresh", NOW)
        started, release = asyncio.Event(), asyncio.Event()
        calls = 0

        async def refresh(token: str) -> Any:
            nonlocal calls
            calls += 1
            assert token == "old-refresh"
            started.set()
            await release.wait()
            return "new-access", "new-refresh", 900

        first = asyncio.create_task(store.ensure_access(sid, refresh, force=True))
        await started.wait()
        second = asyncio.create_task(store.ensure_access(sid, refresh, force=True))
        await asyncio.sleep(0)
        release.set()
        assert await first == await second == "new-access" and calls == 1
        started.clear()
        release.clear()
        sid = store.create_session("acct-1", "native", "old-access", "old-refresh", NOW)
        pending = asyncio.create_task(store.ensure_access(sid, refresh, force=True))
        await started.wait()
        store.retire_sid(sid)
        release.set()
        assert await pending is None and store.identity(sid) is None

    run(scenario())


def test_authorization_states_are_bounded_expire_and_callback_is_single_use() -> None:
    async def scenario() -> None:
        state, _ = make_state()
        clock = [NOW]
        state.now_ms = lambda: clock[0]
        state.public_origin = "https://gw"
        double = SyntheticOidc()
        async with httpx.AsyncClient(transport=httpx.MockTransport(double.request)) as provider:
            state.identity_provider = OidcProvider(
                double.config, client=provider, clock=lambda: NOW
            )
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=create_app(state)), base_url="https://gw"
            ) as http:
                _, url = WebSessionStore("https://gw", b"s" * 32).begin_login("https://gw")
                authorize = await http.get(url)
                external = await provider.get(authorize.headers["location"])
                callback = await http.get(external.headers["location"])
                assert callback.status_code == 302
                assert (await http.get(external.headers["location"])).status_code == 401
                code = parse_qs(urlsplit(callback.headers["location"]).query)["code"][0]
                clock[0] += 60_001
                expired = await http.post(
                    "/v1/token",
                    json={"grant": "authorization_code", "code": code, "verifier": "v" * 64},
                )
                assert expired.status_code == 401 and not state.sessions._by_access
                for _ in range(FLOW_LIMIT):
                    assert (await http.get(url)).status_code == 302
                assert len(state.authorization.pending) == FLOW_LIMIT
                assert (await http.get(url)).status_code == 400
                clock[0] += FLOW_TTL_MS + 1
                assert (await http.get(url)).status_code == 302
                assert len(state.authorization.pending) == 1 and double.exchanges == 1
        state.push.close()

    run(scenario())


def test_companion_configures_identity_and_reports_readiness_without_a_service(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    import flet.fastapi as flet_fastapi

    async def noop() -> None:
        pass

    monkeypatch.setattr(flet_fastapi.app_manager, "start", noop)
    monkeypatch.setattr(flet_fastapi.app_manager, "shutdown", noop)

    async def scenario() -> None:
        identity = tmp_path / "identity.json"
        identity.write_text(json.dumps(SyntheticOidc().config))
        identity.chmod(0o600)
        config = load_config(config_file(tmp_path, identity_config="identity.json"))
        runtime = CompanionRuntime(config, server_factory=AsgiServer)
        try:
            await runtime.start(asyncio.Event())
            assert runtime.report()["identity_ready"]
            provider = runtime.gateway.identity_provider
            assert isinstance(provider, OidcProvider)
            assert not provider.client.is_closed
        finally:
            await runtime.close()
        assert provider.client.is_closed

    run(scenario())
