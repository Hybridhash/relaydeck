"""Real Flet setup component lifecycle, paired theme and diagnostics refresh."""

from __future__ import annotations

import asyncio
from typing import Any

import flet as ft

from relaydeck.client.components.appearance import AppearanceBridge
from relaydeck.client.components.setup import SetupPanel
from relaydeck.client.theme import palette
from tests.support.host_harness import render_component, run


def test_setup_refresh_theme_and_unmount_cleanup() -> None:
    async def scenario() -> None:
        current = {"pairing": "Pair this Mac explicitly."}
        appearance = AppearanceBridge(False)
        setters = {}

        @ft.component
        def SetupHost() -> Any:
            visible, set_visible = ft.use_state(True)
            setters["visible"] = set_visible
            return ft.Column(
                controls=[
                    SetupPanel(
                        report=lambda: (
                            ("Private address", "https://test-mac.tailnet.ts.net"),
                            ("Pairing", current["pairing"]),
                        ),
                        appearance=appearance,
                    )
                ]
                if visible
                else [ft.Text("Signed in")]
            )

        app = await render_component(SetupHost)
        assert app.find("companion-setup").bgcolor == palette(False)["surface"]
        assert len(appearance._listeners) == 1
        appearance.set_dark(True)
        await app.settle()
        assert app.find("companion-setup").bgcolor == palette(True)["surface"]
        current["pairing"] = "Authenticated connector connected."
        await asyncio.sleep(1.05)
        await app.settle()
        assert app.find("setup-Pairing").controls[1].value == current["pairing"]
        setters["visible"](False)
        await app.settle()
        assert appearance._listeners == []

    run(scenario())
