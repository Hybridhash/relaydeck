"""Pairing drives the actual packaged entrypoint and secure auth/resource boundary."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import flet as ft
import pytest

from relaydeck.client.adapters.fake import FakeClipboard, FakeCredentialStore, FakeNotifications
from relaydeck.client.adapters.ports import PushPermission
from relaydeck.client.features.auth.service import ACCESS_KEY, REFRESH_KEY
from relaydeck.client.features.pairing.service import PENDING_KEY
from relaydeck.client.packaged import PackagedApp
from relaydeck.client.packaged_runtime import PackagedRuntime
from tests.support.companion.pairing_harness import Harness
from tests.support.host_harness import render_component
from tests.support.sync.transport_fixtures import _wait
from tests.support.ui.packaged import Connectivity, Preferences


async def mount(factory: Any, notifications: Any) -> Any:
    app = await render_component(ft.Text, "Starting")

    @ft.component
    def Parent() -> Any:
        mounted, set_mounted = ft.use_state(True)
        return ft.Column(
            controls=[
                PackagedApp(
                    page=app.page,
                    runtime_factory=factory,
                    notifications_factory=notifications,
                    connectivity_factory=Connectivity,
                )
                if mounted
                else ft.Text("Closed"),
                ft.Button("Unmount", key="test-unmount", on_click=lambda e: set_mounted(False)),
            ]
        )

    app.page.render(Parent)
    app.session.get_page_patch()
    await app.settle()
    return app


def test_connect_panel_pairs_actual_packaged_root_and_push_with_server_device_id(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        h = Harness(tmp_path / "state")
        credentials = FakeCredentialStore()
        prefs = Preferences({})
        runtimes: list[PackagedRuntime] = []
        notifications = FakeNotifications(permission=PushPermission.GRANTED)

        async def browser(url: str) -> None:
            pytest.fail("Device pairing must not open an identity provider")

        def factory(page: Any, notify: Any) -> PackagedRuntime:
            runtime = PackagedRuntime(
                page, credentials, prefs, browser, notify, client=h.http, clipboard=FakeClipboard()
            )
            runtime.pairing.now_ms = lambda: h.clock[0]
            runtime.pairing.sleep = h.sleep
            runtimes.append(runtime)
            return runtime

        app = await mount(factory, lambda: notifications)
        runtime = runtimes[0]
        try:
            await app.settle()
            with pytest.raises(KeyError):
                app.find("packaged-root-holder")
            with pytest.raises(KeyError):
                app.find("auth-sign-in")
            await app.set_props_and_fire(
                "connect-endpoint",
                {"value": h.state.public_origin},
                "change",
                h.state.public_origin,
            )
            await app.set_props_and_fire("connect-begin", {}, "click")
            await _wait(
                lambda: runtime.phase in {"ready", "error"}
                or runtime.pairing.snapshot.phase == "failed"
            )
            assert runtime.phase == "ready", (runtime.notice, runtime.pairing.snapshot)
            await app.settle()
            assert not app.find("packaged-root-holder").disabled
            assert runtime.resources is not None and runtime.auth is not None
            device_id = runtime.resources.props["session_factory"].device_id
            assert device_id.startswith("device_")
            assert runtime.resources.props["account"] == "owner"
            # The native provider creates a fresh token after old-origin cleanup.
            notifications.rotate_token("synthetic-paired-push-token")
            await app.settle()
            await runtime.notifications.sync()
            assert runtime.notifications._registered is not None, (
                runtime.notifications.notice,
                runtime.input_allowed(),
                runtime.phase,
            )
            assert runtime.notifications._registered[2] == device_id
            assert [row["device_id"] for row in h.state.push.device_metadata("owner")] == [
                device_id
            ]
            assert await credentials.get(ACCESS_KEY) and await credentials.get(REFRESH_KEY)
            assert not any(k.startswith("pairing_") for k in credentials._data)
            assert set(prefs.values) <= {"relaydeck-endpoint", "relaydeck-route"}
            assert "token" not in str(prefs.values) and "verifier" not in str(prefs.values)
            before = await credentials.get(REFRESH_KEY)
            await runtime.auth.refresh()
            assert await credentials.get(REFRESH_KEY) != before
            me = await runtime.resources.transport.me()
            assert me["device_id"] == device_id and me["hosts"] == ["home-mac"]
            await runtime.logout()
            await app.settle()
            assert runtime.resources is None and not runtime.input_allowed()
            assert app.find("connect-begin") is not None
            assert await credentials.get(REFRESH_KEY) is None
            assert notifications.revokes >= 1
        finally:
            await app.set_props_and_fire("test-unmount", {}, "click")
            await h.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["unavailable", "cleanup"])
def test_packaged_startup_storage_failure_disables_pairing_and_never_renders_root(
    failure: str,
) -> None:
    async def scenario() -> None:
        credentials = FakeCredentialStore(available=failure != "unavailable")
        if failure == "cleanup":

            async def fail(key: str) -> None:
                raise RuntimeError("synthetic")

            credentials.delete = fail  # type: ignore[method-assign]
        owners: list[PackagedRuntime] = []

        async def browser(url: str) -> None:
            pytest.fail("No identity provider during pairing")

        def factory(page: Any, notify: Any) -> PackagedRuntime:
            runtime = PackagedRuntime(page, credentials, Preferences({}), browser, notify)
            owners.append(runtime)
            return runtime

        app = await mount(factory, FakeNotifications)
        try:
            await _wait(lambda: owners[0].phase == "unavailable")
            await app.settle()
            assert app.find("packaged-connect").disabled
            assert not owners[0].input_allowed()
            with pytest.raises(KeyError):
                app.find("packaged-root-holder")
        finally:
            await app.set_props_and_fire("test-unmount", {}, "click")

    asyncio.run(scenario())


def test_packaged_cold_start_discards_pending_attempt_instead_of_redeeming(tmp_path: Path) -> None:
    async def scenario() -> None:
        h = Harness(tmp_path / "state")
        credentials = FakeCredentialStore()
        await credentials.set(PENDING_KEY, "pairing_verifier_old")
        await credentials.set("pairing_verifier_old", "private old verifier")
        owners: list[PackagedRuntime] = []

        async def browser(url: str) -> None:
            pytest.fail("No provider")

        def factory(page: Any, notify: Any) -> PackagedRuntime:
            runtime = PackagedRuntime(
                page, credentials, Preferences({}), browser, notify, client=h.http
            )
            owners.append(runtime)
            return runtime

        app = await mount(factory, FakeNotifications)
        try:
            await app.settle()
            assert not credentials._data and not h.calls
            assert owners[0].pairing.snapshot.phase == "setup"
            assert app.find("connect-begin") is not None
        finally:
            await app.set_props_and_fire("test-unmount", {}, "click")
            await h.close()

    asyncio.run(scenario())


def test_background_during_local_approval_fences_pairing_without_creating_root(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        h = Harness(tmp_path / "state")
        owners: list[PackagedRuntime] = []
        release = asyncio.Event()

        async def waiting(interval: float) -> None:
            await release.wait()

        async def browser(url: str) -> None:
            pytest.fail("No provider")

        def factory(page: Any, notify: Any) -> PackagedRuntime:
            runtime = PackagedRuntime(
                page, h.credentials, Preferences({}), browser, notify, client=h.http
            )
            runtime.pairing.now_ms = lambda: h.clock[0]
            runtime.pairing.sleep = waiting
            owners.append(runtime)
            return runtime

        app = await mount(factory, FakeNotifications)
        runtime = owners[0]
        try:
            await app.set_props_and_fire(
                "connect-endpoint",
                {"value": h.state.public_origin},
                "change",
                h.state.public_origin,
            )
            await app.set_props_and_fire("connect-begin", {}, "click")
            await _wait(lambda: runtime.pairing.snapshot.phase == "waiting")
            assert not app.find("connect-cancel").disabled
            runtime.background()
            assert runtime.pairing.snapshot.phase == "setup"
            release.set()
            if runtime._pause_task is not None:
                await runtime._pause_task
            await app.settle()
            assert not await h.credentials.get(REFRESH_KEY)
            assert not any(key.startswith("pairing_") for key in h.credentials._data)
            assert runtime.resources is None and not runtime.input_allowed()
        finally:
            release.set()
            await app.set_props_and_fire("test-unmount", {}, "click")
            await h.close()

    asyncio.run(scenario())
