"""Retry banner delay is presentation only and spans the outage."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any

import flet as ft

from relaydeck.client.features.inventory.components import ConnectionBanner
from relaydeck.client.features.inventory.selectors import BannerModel, connection_banner
from relaydeck.client.features.sync.controller import SyncConnection
from relaydeck.domain.models import ConnectionState
from tests.support.host_harness import render_component, run
from tests.support.ui.navigation import _state
from tests.support.ui.window_design import _missing


def test_retry_banner_waits_then_renders_without_another_state_event() -> None:
    async def scenario() -> None:
        since = asyncio.get_running_loop().time()
        state = _state(
            connection=SyncConnection(
                state=ConnectionState.RECONNECTING,
                detail="stream_closed",
                retry_attempt=3,
                not_live_since=since,
            )
        )
        model = connection_banner(state)
        assert model is not None and model.delay_seconds == 2
        app = await render_component(ConnectionBanner, model=model, on_action=lambda: None)
        assert _missing(app, "banner-text")
        await asyncio.sleep(2.05)
        assert app.find("banner-text").value == "Reconnecting (stream_closed, attempt 3)…"

    run(scenario())


def test_same_outage_reason_changes_do_not_restart_timer_and_unmount_cancels() -> None:
    async def scenario() -> None:
        since = asyncio.get_running_loop().time() - 1.7

        @ft.component
        def Parent() -> Any:
            phase, set_phase = ft.use_state(0)
            model = BannerModel(
                "info",
                "retry A" if phase == 0 else "retry B",
                "",
                delay_seconds=2,
                not_live_since=since,
            )
            return ft.Column(
                controls=[
                    ConnectionBanner(key="banner", model=model, on_action=lambda: None)
                    if phase < 2
                    else ft.Text("Live", key="live"),
                    ft.Button("Change", key="change", on_click=lambda e: set_phase(phase + 1)),
                ]
            )

        app = await render_component(Parent)
        assert _missing(app, "banner-text")
        await app.set_props_and_fire("change", {}, "click")
        await asyncio.sleep(0.35)
        assert app.find("banner-text").value == "retry B"
        await app.set_props_and_fire("change", {}, "click")
        assert _missing(app, "banner-text") and app.find("live") is not None
        # A different host/outage creates a fresh pending timer, then
        # recovery unmounts it before expiry. No late banner resurrects.
        app = await render_component(Parent)
        await app.set_props_and_fire("change", {}, "click")
        await app.set_props_and_fire("change", {}, "click")
        await app.settle()
        assert _missing(app, "banner-text")

    run(scenario())


def test_fatal_revocation_bypasses_retry_grace() -> None:
    async def scenario() -> None:
        state = replace(_state(), revoked=True)
        model = connection_banner(state)
        assert model is not None and model.delay_seconds == 0
        app = await render_component(ConnectionBanner, model=model, on_action=lambda: None)
        assert "revoked" in app.find("banner-text").value.lower()

    run(scenario())


def test_retry_timer_is_cancelled_after_reason_rerender_and_unmount() -> None:
    async def scenario() -> None:
        since = asyncio.get_running_loop().time()

        @ft.component
        def Parent() -> Any:
            phase, set_phase = ft.use_state(0)
            return ft.Column(
                controls=[
                    ConnectionBanner(
                        key="banner",
                        model=BannerModel(
                            "info", f"retry {phase}", "", delay_seconds=20, not_live_since=since
                        ),
                        on_action=lambda: None,
                    )
                    if phase < 2
                    else ft.Text("Live"),
                    ft.Button("Change", key="change", on_click=lambda e: set_phase(phase + 1)),
                ]
            )

        def timers() -> list[Any]:
            return [
                task
                for task in asyncio.all_tasks()
                if task.get_name() == "relaydeck-reconnect-banner" and not task.done()
            ]

        app = await render_component(Parent)
        initial = timers()
        assert len(initial) == 1
        await app.set_props_and_fire("change", {}, "click")
        assert timers() == initial  # same outage and timer after a new reason
        await app.set_props_and_fire("change", {}, "click")
        assert timers() == [] and initial[0].cancelled()
        assert _missing(app, "banner-text")

    run(scenario())
