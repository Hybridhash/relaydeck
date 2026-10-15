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


def test_completed_hold_cannot_complete_twice(tmp_path: Path) -> None:
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
                assert len(store._sessions) == 1  # exactly one session minted
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
