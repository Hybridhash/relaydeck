"""Actual private web mount serves the bundled Console assets without a CDN."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast
from urllib.parse import urlsplit

import flet as ft
import httpx

from relaydeck.client.asset_paths import CLIENT_ASSETS
from relaydeck.client.components.appearance import configure_page_theme
from relaydeck.client.components.design_system.fonts import dot_tile_src, page_fonts
from relaydeck.client.serve import build_web_app
from relaydeck.gateway.app import GatewayState, create_app
from relaydeck.gateway.backend import FakeBackend


def test_actual_web_mount_serves_exact_local_fonts_and_tile() -> None:
    async def scenario() -> None:
        state = GatewayState(backend=FakeBackend(), cursor_secret=b"x" * 32)

        async def main(page: ft.Page) -> None:
            raise AssertionError("static assets must not start a Flet user session")

        app, _ = build_web_app(
            gateway_state=state,
            gateway_app=create_app(state),
            flet_main=main,
            public_origin="https://private.example",
            cookie_secret=b"console-test-cookie-secret-32bytes",
        )
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="https://private.example"
            ) as client:
                for path in (*page_fonts().values(), dot_tile_src()):
                    assert "://" not in path
                    response = await client.get(f"/{path}")
                    assert response.status_code == 200, path
                    assert response.content == (CLIENT_ASSETS / urlsplit(path).path).read_bytes()
                # Serving public fonts must not open the recovered API boundary.
                private = await client.get("/v1/hosts/home-mac/snapshot")
                assert private.status_code == 401
        finally:
            state.push.close()

    asyncio.run(scenario())


def test_shared_page_setup_registers_all_bundled_weights_and_keeps_existing_fonts() -> None:
    page = SimpleNamespace(
        fonts={"Existing": "existing.ttf"}, platform_brightness=ft.Brightness.DARK
    )
    configure_page_theme(cast(Any, page))
    assert page.fonts == {"Existing": "existing.ttf", **page_fonts()}
    assert page.theme.font_family == page.dark_theme.font_family == "Bricolage Grotesque"
