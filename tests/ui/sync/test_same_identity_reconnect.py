"""Production web reconnect rebinds identity before resuming metadata UI."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from relaydeck.client import serve
from relaydeck.client.features.sync.controller import SyncController
from relaydeck.gateway.app import create_app
from tests.support.gateway.flet_gateway import _gateway_state
from tests.support.sync.scripted_sync import _ScriptedTransport


def test_actual_web_on_connect_resumes_only_same_verified_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        store = serve.WebSessionStore(
            public_origin="https://private.test", cookie_secret=b"synthetic-test-cookie-key-32bytes"
        )
        sid = store.create_session(
            "dev", "own-device", "synthetic-access", "synthetic-refresh", 2_000_000
        )
        controller = SyncController(transport=_ScriptedTransport())
        resumed: list[bool] = []
        controller.subscribe_resume(lambda: resumed.append(True))
        starts: list[bool] = []
        terminal_resumes: list[dict[str, Any]] = []

        async def close() -> None:
            pass

        def run_app(*args: Any, **kwargs: Any) -> Any:
            starts.append(True)
            return SimpleNamespace(
                controller=controller,
                close=close,
                resume_terminals=lambda **kwargs: terminal_resumes.append(kwargs),
                mark_page_visible=lambda: None,
                note_hidden=lambda: None,
                activate=lambda reason, **kw: None,
                fence_replaced_transport=lambda transport: None,
            )

        monkeypatch.setattr("relaydeck.client.app.run_app", run_app)

        class Page:
            def __init__(self) -> None:
                self.session = SimpleNamespace(id="synthetic-same-identity", connection=object())
                self.renders: list[Any] = []
                self.fonts: dict[str, str] = {}

            def render(self, component: Any, **kwargs: Any) -> None:
                self.renders.append(component)

        page = Page()
        state = _gateway_state()
        main = serve.make_relaydeck_main(
            store=store,
            gateway_state=state,
            gateway_app=create_app(state),
            base_url="https://private.test",
            host_id="home-mac",
        )
        handshake = serve._Handshake(ip="", user_agent="synthetic", page_name="")
        token = serve._current_handshake.set((sid, handshake))
        try:
            main(page)
            # The bare Page double has no ASGI registration guard; the
            # first real on_connect handler adopts the verified handshake.
            await page.on_connect(None)
            assert starts == [True]
            await page.on_connect(None)
            await page.on_connect(None)
            assert starts == [True] and resumed == [True, True]
            assert store.owner_of(page.session.id) == sid
            old = serve._FletTransport(close, asyncio.Event())
            old.closed.set()
            stale = serve._current_transport.set(old)
            try:
                await page.on_connect(None)
                assert starts == [True] and resumed == [True, True]
                assert store.owner_of(page.session.id) == sid
            finally:
                serve._current_transport.reset(stale)
            active = serve._FletTransport(close, asyncio.Event(), flet_id=page.session.id)
            proof = serve._current_transport.set(active)
            try:
                await page.on_connect(None)
                assert len(terminal_resumes) == 1
                trigger = terminal_resumes[0]
                assert trigger["transport"] is active and trigger["closed"] is active.closed
                assert trigger["current"]()
                original_connection = page.session.connection
                page.session.connection = object()
                assert not trigger["current"]()
                page.session.connection = original_connection
                active.closed.set()
                assert not trigger["current"]()
                await page.on_connect(None)
                assert len(terminal_resumes) == 1
            finally:
                serve._current_transport.reset(proof)
        finally:
            serve._current_handshake.reset(token)
        store.retire_sid(sid)
        token = serve._current_handshake.set((None, handshake))
        try:
            await page.on_connect(None)
            assert starts == [True] and resumed == [True, True, True]
        finally:
            serve._current_handshake.reset(token)

    asyncio.run(scenario())
