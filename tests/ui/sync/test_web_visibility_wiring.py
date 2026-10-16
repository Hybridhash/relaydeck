"""The hosted web entrypoint delivers tab visibility to the app resources."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import flet as ft
import pytest

from relaydeck.client import serve
from relaydeck.client.features.sync.controller import SyncController
from relaydeck.gateway.app import create_app
from tests.support.gateway.flet_gateway import _gateway_state
from tests.support.sync.scripted_sync import _ScriptedTransport


def test_page_lifecycle_state_reaches_the_rendered_resources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        store = serve.WebSessionStore(
            public_origin="https://private.test", cookie_secret=b"synthetic-test-cookie-key-32bytes"
        )
        sid = store.create_session(
            "dev", "own-device", "synthetic-access", "synthetic-refresh", 2_000_000
        )
        visible: list[bool] = []

        async def close() -> None:
            pass

        def run_app(*args: Any, **kwargs: Any) -> Any:
            return SimpleNamespace(
                controller=SyncController(transport=_ScriptedTransport()),
                close=close,
                resume_terminals=lambda **kwargs: None,
                fence_replaced_transport=lambda transport: None,
                mark_page_visible=lambda: None,
                note_hidden=lambda: None,
                activate=lambda reason, **kw: visible.append(True),
            )

        monkeypatch.setattr("relaydeck.client.app.run_app", run_app)

        class Page:
            def __init__(self) -> None:
                self.session = SimpleNamespace(id="synthetic-visibility", connection=object())
                self.fonts: dict[str, str] = {}
                self.on_app_lifecycle_state_change: Any = None

            def render(self, component: Any, **kwargs: Any) -> None:
                pass

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
            assert callable(page.on_app_lifecycle_state_change)
            await page.on_connect(None)
            states = ft.AppLifecycleState
            before = len(visible)
            # Focus loss pairs and a stray hide after a return never mark the page hidden.
            for pair in (
                (states.INACTIVE, states.HIDE),
                (states.SHOW, states.RESUME),
                (states.INACTIVE, states.HIDE),
                (states.PAUSE,),
            ):
                for state_value in pair:
                    page.on_app_lifecycle_state_change(SimpleNamespace(state=state_value))
            assert visible[before:] == [True, True]
            assert False not in visible
        finally:
            serve._current_handshake.reset(token)

    asyncio.run(scenario())
