"""Same-browser pairing completion fallback.

When a mobile tab resumes with a NEW Flet page id, `pairing_complete` falls
back to another page of the SAME browser cookie holding an unexpired result
(hold 300_000 ms). Drives the real Starlette route in-process (httpx
ASGITransport, `Harness` gateway + injectable clock); holds come from the
real `web_pairing_flow`/`PairingFlow` approval path, never `sleep`.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx

from relaydeck.client.serve import WebSessionStore, build_web_app, web_pairing_flow
from relaydeck.gateway.app import create_app
from tests.support.companion.pairing_harness import Harness
from tests.support.pairing import ORIGIN


def _setup(tmp_path: Path) -> tuple[Harness, Any, WebSessionStore, Any]:
    h = Harness(tmp_path / "state")
    gateway = create_app(h.state)
    store = WebSessionStore(ORIGIN, b"s" * 32, now_ms=lambda: h.clock[0])
    app, _ = build_web_app(
        gateway_state=h.state,
        gateway_app=gateway,
        flet_main=lambda page: None,
        public_origin=ORIGIN,
        cookie_secret=b"s" * 32,
        store=store,
    )
    return h, gateway, store, app


async def _bind_page(browser: httpx.AsyncClient, store: WebSessionStore, page_id: str) -> str:
    """Real browser-cookie bootstrap, then bind a synthetic page id to it."""
    response = await browser.post("/v1/auth/pairing/browser", headers={"Origin": ORIGIN})
    assert response.status_code in (201, 204)
    identity = store.browser_identity(browser.cookies["rd_browser"])
    assert identity is not None
    store.browser_pages[page_id] = identity
    return identity


async def _ready_hold(h: Harness, store: WebSessionStore, gateway: Any, page_id: str) -> None:
    """Run the real approval flow so `page_id` holds a ready result."""
    flow = web_pairing_flow(store, page_id, gateway)
    assert flow is not None
    flow.sleep = h.sleep
    h.approve = True
    await flow.begin(ORIGIN, "home-mac", "Synthetic")
    assert flow.snapshot.phase == "ready"
    assert page_id in store.pairing_ready


def test_fallback_completes_for_new_page_id_same_browser(tmp_path: Path) -> None:
    async def scenario() -> None:
        h, gateway, store, app = _setup(tmp_path)
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url=ORIGIN
            ) as browser:
                await _bind_page(browser, store, "page-1")
                await _ready_hold(h, store, gateway, "page-1")
                response = await browser.post(
                    "/v1/auth/pairing/complete",
                    headers={"Origin": ORIGIN},
                    json={"flet_session_id": "page-2-resumed"},
                )
                assert response.status_code == 200
                assert response.json() == {"ok": True}
                assert "rd_session" in browser.cookies
                assert "page-1" not in store.pairing_ready  # held result consumed once
                assert len(store._sessions) == 1
        finally:
            await h.close()

    asyncio.run(scenario())


def test_other_browser_cookie_is_refused_and_hold_survives(tmp_path: Path) -> None:
    async def scenario() -> None:
        h, gateway, store, app = _setup(tmp_path)
        try:
            async with (
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url=ORIGIN
                ) as browser,
                httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN) as other,
            ):
                await _bind_page(browser, store, "page-1")
                await _ready_hold(h, store, gateway, "page-1")
                assert (
                    await other.post("/v1/auth/pairing/browser", headers={"Origin": ORIGIN})
                ).status_code == 201
                for page_id in ("page-1", "page-2-resumed"):
                    response = await other.post(
                        "/v1/auth/pairing/complete",
                        headers={"Origin": ORIGIN},
                        json={"flet_session_id": page_id},
                    )
                    assert response.status_code == 404
                assert "rd_session" not in other.cookies
                assert "page-1" in store.pairing_ready  # refused attempt consumes nothing
                assert not store._sessions
        finally:
            await h.close()

    asyncio.run(scenario())


def test_expired_hold_is_refused(tmp_path: Path) -> None:
    async def scenario() -> None:
        h, gateway, store, app = _setup(tmp_path)
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url=ORIGIN
            ) as browser:
                await _bind_page(browser, store, "page-1")
                await _ready_hold(h, store, gateway, "page-1")
                h.clock[0] += 300_001  # hold lasts 300_000 ms; browser cookie 600_000
                for page_id in ("page-2-resumed", "page-1"):
                    response = await browser.post(
                        "/v1/auth/pairing/complete",
                        headers={"Origin": ORIGIN},
                        json={"flet_session_id": page_id},
                    )
                    assert response.status_code == 404
                assert "rd_session" not in browser.cookies
                assert not store._sessions
        finally:
            await h.close()

    asyncio.run(scenario())


def test_a_consumed_attempt_cannot_complete_again(tmp_path: Path) -> None:
    async def scenario() -> None:
        h, gateway, store, app = _setup(tmp_path)
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url=ORIGIN
            ) as browser:
                await _bind_page(browser, store, "page-1")
                await _ready_hold(h, store, gateway, "page-1")
                first = await browser.post(
                    "/v1/auth/pairing/complete",
                    headers={"Origin": ORIGIN},
                    json={"flet_session_id": "page-2-resumed"},
                )
                assert first.status_code == 200
                for page_id in ("page-2-resumed", "page-1"):
                    response = await browser.post(
                        "/v1/auth/pairing/complete",
                        headers={"Origin": ORIGIN},
                        json={"flet_session_id": page_id},
                    )
                    assert response.status_code == 404
                    assert "set-cookie" not in response.headers
                assert len(store._sessions) == 1
                # The finish page treats a live signed-in browser as success.
                live = await browser.post("/v1/auth/session/check", headers={"Origin": ORIGIN})
                assert live.status_code == 204
        finally:
            await h.close()

    asyncio.run(scenario())


def test_distinct_attempts_of_one_browser_never_share_a_completion(tmp_path: Path) -> None:
    async def scenario() -> None:
        h, gateway, store, app = _setup(tmp_path)
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url=ORIGIN
            ) as browser:
                await _bind_page(browser, store, "page-1")
                await _bind_page(browser, store, "page-2")
                await _ready_hold(h, store, gateway, "page-1")
                await _ready_hold(h, store, gateway, "page-2")
                results = await asyncio.gather(
                    *(
                        browser.post(
                            "/v1/auth/pairing/complete",
                            headers={"Origin": ORIGIN},
                            json={"flet_session_id": page_id},
                        )
                        for page_id in ("page-1", "page-2")
                    )
                )
                assert [r.status_code for r in results] == [200, 200]
                assert len(store._sessions) == 2  # one session per attempt
                assert not store.pairing_ready
        finally:
            await h.close()

    asyncio.run(scenario())


def test_a_revoked_gateway_session_is_not_signed_in(tmp_path: Path) -> None:
    async def scenario() -> None:
        h, gateway, store, app = _setup(tmp_path)
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url=ORIGIN
            ) as browser:
                await _bind_page(browser, store, "page-1")
                await _ready_hold(h, store, gateway, "page-1")
                device = store.pairing_ready["page-1"][2]["device_id"]
                assert (
                    await browser.post(
                        "/v1/auth/pairing/complete",
                        headers={"Origin": ORIGIN},
                        json={"flet_session_id": "page-1"},
                    )
                ).status_code == 200
                h.state.sessions.revoke_device("owner", device)
                response = await browser.post("/v1/auth/session/check", headers={"Origin": ORIGIN})
                assert response.status_code == 401
                assert "set-cookie" not in response.headers  # an observer never clears
        finally:
            await h.close()

    asyncio.run(scenario())


def test_concurrent_completers_share_one_consumption(tmp_path: Path) -> None:
    async def scenario() -> None:
        h, gateway, store, app = _setup(tmp_path)
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url=ORIGIN
            ) as browser:
                await _bind_page(browser, store, "page-1")
                await _ready_hold(h, store, gateway, "page-1")
                results = await asyncio.gather(
                    *(
                        browser.post(
                            "/v1/auth/pairing/complete",
                            headers={"Origin": ORIGIN},
                            json={"flet_session_id": page_id},
                        )
                        for page_id in ("page-1", "page-1", "page-1")
                    )
                )
                assert [r.status_code for r in results] == [200, 200, 200]
                assert len(store._sessions) == 1  # one consumption, shared by the same attempt
                assert "page-1" not in store.pairing_ready
        finally:
            await h.close()

    asyncio.run(scenario())


def test_an_approval_after_the_page_session_was_replaced_still_completes(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        h, gateway, store, app = _setup(tmp_path)
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url=ORIGIN
            ) as browser:
                await _bind_page(browser, store, "page-1")
                await _bind_page(browser, store, "page-2-reconnected")  # new transport, same tab
                await _ready_hold(h, store, gateway, "page-1")  # approval arrives afterwards
                response = await browser.post(
                    "/v1/auth/pairing/complete",
                    headers={"Origin": ORIGIN},
                    json={"flet_session_id": "page-2-reconnected"},
                )
                assert response.status_code == 200
                assert "rd_session" in browser.cookies
        finally:
            await h.close()

    asyncio.run(scenario())


def test_original_page_id_wins_over_fallback(tmp_path: Path) -> None:
    async def scenario() -> None:
        h, gateway, store, app = _setup(tmp_path)
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url=ORIGIN
            ) as browser:
                first = await _bind_page(browser, store, "page-1")
                second = await _bind_page(browser, store, "page-2")
                assert first == second  # both pages, one browser cookie
                await _ready_hold(h, store, gateway, "page-1")
                h.clock[0] += 60_000
                await _ready_hold(h, store, gateway, "page-2")  # newer hold, later expiry
                assert store.pairing_ready["page-2"][3] > store.pairing_ready["page-1"][3]
                response = await browser.post(
                    "/v1/auth/pairing/complete",
                    headers={"Origin": ORIGIN},
                    json={"flet_session_id": "page-1"},
                )
                assert response.status_code == 200
                assert "page-1" not in store.pairing_ready
                # The fallback (latest expiry) was NOT used: page-2's hold survives.
                assert "page-2" in store.pairing_ready
        finally:
            await h.close()

    asyncio.run(scenario())


def test_an_authenticated_page_cannot_consume_another_tabs_attempt(tmp_path: Path) -> None:
    async def scenario() -> None:
        h, gateway, store, app = _setup(tmp_path)
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url=ORIGIN
            ) as browser:
                await _bind_page(browser, store, "page-1")
                await _bind_page(browser, store, "page-2")
                await _ready_hold(h, store, gateway, "page-1")
                await _ready_hold(h, store, gateway, "page-2")
                first = await browser.post(
                    "/v1/auth/pairing/complete",
                    headers={"Origin": ORIGIN},
                    json={"flet_session_id": "page-1"},
                )
                assert first.status_code == 200
                sid = next(iter(store._sessions))
                store._owners["page-1-registered"] = sid  # the signed-in page after a reload
                store.browser_pages["page-1-registered"] = store.browser_pages["page-1"]
                for page_id in ("page-1-registered", "page-1"):
                    response = await browser.post(
                        "/v1/auth/pairing/complete",
                        headers={"Origin": ORIGIN},
                        json={"flet_session_id": page_id},
                    )
                    assert response.status_code == 404
                    assert "set-cookie" not in response.headers
                assert "page-2" in store.pairing_ready  # the other attempt is untouched
                assert len(store._sessions) == 1
        finally:
            await h.close()

    asyncio.run(scenario())


def test_a_caller_joins_the_completion_paused_at_session_creation(tmp_path: Path) -> None:
    async def scenario() -> None:
        h, gateway, store, app = _setup(tmp_path)
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url=ORIGIN
            ) as browser:
                await _bind_page(browser, store, "page-1")
                await _ready_hold(h, store, gateway, "page-1")
                entered = asyncio.Event()
                release = asyncio.Event()
                original = store.run_private

                async def paused(operation: Any) -> Any:
                    if "page-1" not in store.pairing_ready and not entered.is_set():
                        entered.set()
                        await release.wait()
                    return await original(operation)

                store.run_private = paused  # type: ignore[method-assign]

                def post() -> Any:
                    return browser.post(
                        "/v1/auth/pairing/complete",
                        headers={"Origin": ORIGIN},
                        json={"flet_session_id": "page-1"},
                    )

                first = asyncio.ensure_future(post())
                await asyncio.wait_for(entered.wait(), 2)
                assert "page-1" not in store.pairing_ready  # already consumed
                second = asyncio.ensure_future(post())
                await asyncio.sleep(0.05)
                release.set()
                results = await asyncio.gather(first, second)
                assert [r.status_code for r in results] == [200, 200]
                assert len(store._sessions) == 1
                assert results[0].headers["set-cookie"] == results[1].headers["set-cookie"]
        finally:
            await h.close()

    asyncio.run(scenario())


def _principal(store: WebSessionStore, response: httpx.Response) -> str:
    cookie = response.headers["set-cookie"].split(";")[0].split("=", 1)[1]
    sid = store.signed_sid(cookie)
    assert sid is not None
    return store._sessions[sid].identity.device_id


def _pause_creation(store: WebSessionStore, hold_page: str) -> tuple[asyncio.Event, asyncio.Event]:
    entered, release = asyncio.Event(), asyncio.Event()
    original = store.run_private

    async def paused(operation: Any) -> Any:
        if hold_page not in store.pairing_ready and not entered.is_set():
            entered.set()
            await release.wait()
        return await original(operation)

    store.run_private = paused  # type: ignore[method-assign]
    return entered, release


def _post(browser: httpx.AsyncClient, page_id: str) -> Any:
    return browser.post(
        "/v1/auth/pairing/complete", headers={"Origin": ORIGIN}, json={"flet_session_id": page_id}
    )


def test_a_newer_attempt_on_the_same_page_never_joins_the_older_running_one(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        h, gateway, store, app = _setup(tmp_path)
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url=ORIGIN
            ) as browser:
                await _bind_page(browser, store, "page-1")
                await _ready_hold(h, store, gateway, "page-1")
                principal_a = store.pairing_ready["page-1"][2]["device_id"]
                entered, release = _pause_creation(store, "page-1")
                first = asyncio.ensure_future(_post(browser, "page-1"))
                await asyncio.wait_for(entered.wait(), 2)
                await _ready_hold(h, store, gateway, "page-1")  # a newer attempt, same page
                principal_b = store.pairing_ready["page-1"][2]["device_id"]
                assert principal_a != principal_b
                second = await _post(browser, "page-1")
                assert second.status_code == 200
                assert _principal(store, second) == principal_b
                release.set()
                first_result = await first
                assert first_result.status_code == 200
                assert _principal(store, first_result) == principal_a
        finally:
            await h.close()

    asyncio.run(scenario())


def test_an_ambiguous_replacement_page_gets_nothing(tmp_path: Path) -> None:
    async def scenario() -> None:
        h, gateway, store, app = _setup(tmp_path)
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url=ORIGIN
            ) as browser:
                await _bind_page(browser, store, "page-1")
                await _bind_page(browser, store, "page-2")
                await _ready_hold(h, store, gateway, "page-1")
                entered, release = _pause_creation(store, "page-1")
                first = asyncio.ensure_future(_post(browser, "page-1"))
                await asyncio.wait_for(entered.wait(), 2)
                await _ready_hold(h, store, gateway, "page-2")  # a second, held candidate
                await _bind_page(browser, store, "page-new")
                response = await _post(browser, "page-new")
                assert response.status_code == 404  # two candidates: refuse, never guess
                assert "set-cookie" not in response.headers
                assert "page-2" in store.pairing_ready
                release.set()
                assert (await first).status_code == 200
        finally:
            await h.close()

    asyncio.run(scenario())


def test_a_single_candidate_completes_with_its_own_principal(tmp_path: Path) -> None:
    async def scenario() -> None:
        h, gateway, store, app = _setup(tmp_path)
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url=ORIGIN
            ) as browser:
                await _bind_page(browser, store, "page-1")
                await _ready_hold(h, store, gateway, "page-1")
                expected = store.pairing_ready["page-1"][2]["device_id"]
                await _bind_page(browser, store, "page-new")
                response = await _post(browser, "page-new")
                assert response.status_code == 200
                assert _principal(store, response) == expected
        finally:
            await h.close()

    asyncio.run(scenario())


def test_unavailable_session_storage_is_a_temporary_failure_not_signed_out(
    tmp_path: Path,
) -> None:
    from relaydeck.persistence_contracts import StorageUnavailable

    async def scenario() -> None:
        h, gateway, store, app = _setup(tmp_path)
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url=ORIGIN
            ) as browser:
                await _bind_page(browser, store, "page-1")
                await _ready_hold(h, store, gateway, "page-1")
                assert (await _post(browser, "page-1")).status_code == 200

                class Storage:
                    async def run_draft(self, operation: Any) -> Any:
                        return operation()

                def unavailable(_sid: str) -> Any:
                    raise StorageUnavailable("synthetic")

                store.persistence = Storage()  # type: ignore[assignment]
                store._read_web = unavailable  # type: ignore[method-assign]
                response = await browser.post("/v1/auth/session/check", headers={"Origin": ORIGIN})
                assert response.status_code == 503
                assert "set-cookie" not in response.headers
        finally:
            await h.close()

    asyncio.run(scenario())


def test_a_replacement_page_joins_a_completion_paused_at_the_authority_check(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        h = Harness(tmp_path / "state")
        gateway = create_app(h.state)
        entered, release = asyncio.Event(), asyncio.Event()

        @gateway.middleware("http")
        async def pause(request: Any, call_next: Any) -> Any:
            if request.url.path == "/v1/me" and not entered.is_set() and armed:
                entered.set()
                await release.wait()
            return await call_next(request)

        armed = False
        store = WebSessionStore(ORIGIN, b"s" * 32, now_ms=lambda: h.clock[0])
        app, _ = build_web_app(
            gateway_state=h.state,
            gateway_app=gateway,
            flet_main=lambda page: None,
            public_origin=ORIGIN,
            cookie_secret=b"s" * 32,
            store=store,
        )
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url=ORIGIN
            ) as browser:
                await _bind_page(browser, store, "page-a")
                await _ready_hold(h, store, gateway, "page-a")
                expected = store.pairing_ready["page-a"][2]["device_id"]
                await _bind_page(browser, store, "page-b")
                armed = True
                first = asyncio.ensure_future(_post(browser, "page-a"))
                await asyncio.wait_for(entered.wait(), 2)
                assert "page-a" in store.pairing_ready  # not yet consumed
                second = asyncio.ensure_future(_post(browser, "page-b"))
                await asyncio.sleep(0.05)
                release.set()
                results = await asyncio.gather(first, second)
                assert [r.status_code for r in results] == [200, 200]
                assert [_principal(store, r) for r in results] == [expected, expected]
                assert results[0].headers["set-cookie"] == results[1].headers["set-cookie"]
                assert len(store._sessions) == 1
        finally:
            await h.close()

    asyncio.run(scenario())
