"""Stored identity is validated and rotation/callbacks serialize over real ASGI."""

from __future__ import annotations

import asyncio
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx

from relaydeck.client.features.auth.service import (
    ACCESS_KEY,
    EXPIRES_KEY,
    REFRESH_KEY,
    STATE_KEY,
    AuthService,
    AuthState,
    GatewayConfig,
    HttpxTransport,
)
from relaydeck.gateway.app import create_app
from tests.security.helpers import ALL_CAPS, NOW, make_state
from tests.support.fakes import FakeCredentialStore


def test_json_grant_collections_are_frozen_and_cannot_expand_an_issued_session() -> None:
    async def scenario() -> None:
        state, _ = make_state()
        caps, hosts = ["sessions.read"], ["home-mac"]
        access, refresh = state.sessions.create("acct-1", caps, hosts, NOW, "native-1")
        caps.append("terminal.control")
        hosts.append("foreign-mac")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(state)), base_url="https://gw"
        ) as client:
            me = await client.get("/v1/me", headers={"Authorization": f"Bearer {access}"})
            assert me.status_code == 200
            assert me.json()["hosts"] == ["home-mac"]
            assert me.json()["capabilities"] == ["sessions.read"]
            assert state.sessions.refresh(refresh, NOW) is not None
        state.push.close()

    asyncio.run(scenario())


def test_cold_restore_parallel_expiry_reads_rotate_once_and_revoke_fails_closed() -> None:
    async def scenario() -> None:
        state, backend = make_state()
        access, refresh = state.sessions.create(
            "acct-1", ALL_CAPS, frozenset({"home-mac"}), NOW, "native"
        )
        credentials = FakeCredentialStore()
        await credentials.set(REFRESH_KEY, refresh)
        clock = [NOW]
        posts: list[str] = []

        async def open_url(url: str) -> None:
            pass

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(state)), base_url="https://gw"
        ) as http:
            transport = HttpxTransport(http)

            async def post(*args: Any, **kwargs: Any) -> tuple[int, Any]:
                posts.append(args[1]["grant"])
                return await transport.post_json(*args, **kwargs)

            service = AuthService(
                GatewayConfig("https://gw"), credentials, post, open_url, now_ms=lambda: clock[0]
            )
            assert (await service.restore()).state == AuthState.SIGNED_IN
            assert state.sessions.lookup(access, NOW) is None
            values = await asyncio.gather(*(service.access_token() for _ in range(20)))
            assert len(set(values)) == 1 and values[0] and posts == ["refresh_token"]
            clock[0] += 900_000
            state.now_ms = lambda: clock[0]
            values = await asyncio.gather(*(service.access_token() for _ in range(20)))
            assert (
                len(set(values)) == 1 and values[0] and posts == ["refresh_token", "refresh_token"]
            )
            expiry = await credentials.get(EXPIRES_KEY)
            assert expiry is not None and int(expiry) > clock[0]
            state.sessions.revoke_device("acct-1", "native")
            assert (await service.restore()).state == AuthState.ERROR
            assert await credentials.get(REFRESH_KEY) is None
            assert await credentials.get(ACCESS_KEY) is None
            assert await service.access_token() is None
        state.push.close()

    asyncio.run(scenario())


def test_duplicate_callback_exchanges_once_and_logout_cannot_resurrect_identity() -> None:
    async def scenario() -> None:
        state, backend = make_state()
        credentials = FakeCredentialStore()
        opened: list[str] = []

        async def open_url(url: str) -> None:
            opened.append(url)

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(state)), base_url="https://gw"
        ) as http:
            transport = HttpxTransport(http)
            calls: list[str] = []
            entered, release = asyncio.Event(), asyncio.Event()

            async def post(
                url: str, body: dict[str, Any], headers: dict[str, str] | None = None
            ) -> tuple[int, Any]:
                if body.get("grant") == "refresh_token":
                    entered.set()
                    await release.wait()
                calls.append(body.get("grant", "logout"))
                return await transport.post_json(url, body, headers)

            service = AuthService(
                GatewayConfig("https://gw"), credentials, post, open_url, now_ms=lambda: NOW
            )
            url = await service.begin_login()
            challenge = parse_qs(urlsplit(url).query)["code_challenge"][0]
            nonce = await credentials.get(STATE_KEY)
            state.pending_codes["synthetic-code"] = {
                "challenge": challenge,
                "account": "acct-1",
                "capabilities": ALL_CAPS,
                "hosts": frozenset({"home-mac"}),
            }
            uri = f"dev.relaydeck.app://oauth?code=synthetic-code&state={nonce}"
            results = await asyncio.gather(
                service.complete_callback(uri), service.complete_callback(uri)
            )
            assert all(result.state == AuthState.SIGNED_IN for result in results)
            assert calls == ["authorization_code"] and service.snapshot.state == AuthState.SIGNED_IN
            refresh_task = asyncio.create_task(service.refresh())
            await entered.wait()
            logout = asyncio.create_task(service.logout())
            release.set()
            await asyncio.gather(refresh_task, logout)
            assert service.snapshot.state.value == AuthState.SIGNED_OUT.value
            assert await credentials.get(ACCESS_KEY) is None
            assert not state.sessions._by_access
            assert (await service.complete_callback(uri)).state == AuthState.ERROR
            assert calls == ["authorization_code", "refresh_token", "logout"]
        state.push.close()

    asyncio.run(scenario())


def test_uncertain_exchange_consumes_callback_and_does_not_replay() -> None:
    async def scenario() -> None:
        credentials = FakeCredentialStore()
        calls: list[str] = []

        def fail(request: httpx.Request) -> httpx.Response:
            calls.append(request.url.path)
            raise httpx.ReadTimeout("synthetic response lost", request=request)

        async def open_url(url: str) -> None:
            pass

        async with httpx.AsyncClient(transport=httpx.MockTransport(fail)) as http:
            service = AuthService(
                GatewayConfig("https://gw"), credentials, HttpxTransport(http).post_json, open_url
            )
            await service.begin_login()
            nonce = await credentials.get(STATE_KEY)
            uri = f"dev.relaydeck.app://oauth?code=synthetic-code&state={nonce}"
            assert (await service.complete_callback(uri)).state == AuthState.ERROR
            assert await credentials.get(STATE_KEY) is None
            assert (await service.complete_callback(uri)).state == AuthState.ERROR
            assert calls == ["/v1/token"] and await credentials.get(ACCESS_KEY) is None

    asyncio.run(scenario())
