"""Packaged entrypoint wiring on Flet + ASGI; native service methods are doubles."""

from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace
from typing import Any, cast

import flet as ft
import httpx
import pytest

from relaydeck.client.features.auth.service import REFRESH_KEY
from relaydeck.client.packaged import PackagedApp
from relaydeck.client.packaged_runtime import PackagedRuntime, ScopedDrafts, private_endpoint
from relaydeck.gateway.app import create_app
from tests.security.helpers import ALL_CAPS, NOW, make_state
from tests.support.fakes import FakeClipboard, FakeCredentialStore, FakeNotifications
from tests.support.host_harness import render_component, run
from tests.support.sync.transport_fixtures import _wait
from tests.support.ui.packaged import Connectivity, Preferences


def test_packaged_app_constructs_authenticated_root_with_actual_identity_and_secure_drafts() -> (
    None
):
    async def scenario() -> None:
        state, backend = make_state()
        _, refresh = state.sessions.create(
            "acct-1", ALL_CAPS, frozenset({"home-mac"}), NOW, "native-login"
        )
        credentials = FakeCredentialStore()
        await credentials.set(REFRESH_KEY, refresh)
        await credentials.set("identity_origin", "https://gw")
        prefs = Preferences(
            {"relaydeck-endpoint": "https://gw", "relaydeck-route": "/hosts/home-mac"}
        )
        http = httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(state)))
        owner: list[PackagedRuntime] = []
        connectivity = Connectivity()

        async def browser(url: str) -> None:
            pytest.fail("Cold restore must not open the browser")

        def factory(page: Any, notify: Callable[[], None]) -> PackagedRuntime:
            runtime = PackagedRuntime(
                page, credentials, prefs, browser, notify, client=http, clipboard=FakeClipboard()
            )
            owner.append(runtime)
            return runtime

        app = await render_component(lambda: ft.Text("Starting"))

        @ft.component
        def Parent() -> Any:
            mounted, set_mounted = ft.use_state(True)
            return ft.Column(
                controls=[
                    PackagedApp(
                        page=app.page,
                        runtime_factory=factory,
                        notifications_factory=FakeNotifications,
                        connectivity_factory=lambda: connectivity,
                    )
                    if mounted
                    else ft.Text("Closed"),
                    ft.Button("Unmount", key="test-unmount", on_click=lambda e: set_mounted(False)),
                ]
            )

        app.page.render(Parent)
        cast(Any, app.session).get_page_patch()
        try:
            await _wait(lambda: bool(owner) and owner[0].phase in {"ready", "error"})
            assert owner[0].phase == "ready", owner[0].notice
            await app.settle()
            runtime = owner[0]
            assert runtime.resources is not None
            assert runtime.resources.props["account"] == "acct-1"
            assert runtime.resources is not None
            assert runtime.resources.props["session_factory"].device_id == "native-login"
            assert runtime.resources.props["session_factory"].ops.picture_api.native is True
            assert runtime.resources is not None
            assert runtime.resources.bridge.state.destination == "settings"
            assert app.find("destinations") is not None and runtime.input_allowed()
            navigation_id = app.find("destinations")._i
            refresh_before = await credentials.get(REFRESH_KEY)
            app.page.on_app_lifecycle_state_change(
                SimpleNamespace(state=ft.AppLifecycleState.PAUSE)
            )
            assert not runtime.input_allowed() and runtime.phase == "paused"
            assert runtime._pause_task is not None
            await runtime._pause_task
            await app.settle()
            assert app.find("destinations")._i == navigation_id
            assert app.find("packaged-root-holder").disabled
            connectivity.on_change(SimpleNamespace(connectivity=[ft.ConnectivityType.NONE]))
            app.page.on_app_lifecycle_state_change(
                SimpleNamespace(state=ft.AppLifecycleState.RESUME)
            )
            assert not runtime.input_allowed()
            connectivity.on_change(SimpleNamespace(connectivity=[ft.ConnectivityType.WIFI]))
            await _wait(lambda: runtime.phase in {"ready", "error", "login"})
            assert runtime.phase == "ready", runtime.notice
            await app.settle()
            assert app.find("destinations")._i == navigation_id
            assert runtime.resume_report is not None
            assert not runtime.resume_report.control_granted  # No terminal was attached.
            assert await credentials.get(REFRESH_KEY) != refresh_before
            assert runtime.resources is not None
            assert runtime.resources.controller.state.inventory.hosts["home-mac"].epoch == "epoch-7"
            assert runtime.input_allowed()
            assert runtime.resources is not None
            drafts = runtime.resources.props["session_factory"].draft_store
            await drafts.set("draft-home-mac:term-1", "synthetic sensitive draft")
            foreign = ScopedDrafts(credentials, "acct-2")
            assert await foreign.get("draft-home-mac:term-1") is None
            assert "synthetic sensitive draft" not in str(prefs.values)
            assert not any(":" in key or "acct-1" in key for key in credentials._data)
            await runtime.logout()
            await app.settle()
            assert not runtime.input_allowed() and runtime.resources is None
            assert await credentials.get(REFRESH_KEY) is None
            assert prefs.values["relaydeck-endpoint"] == "https://gw"
            assert app.find("connect-begin") is not None
        finally:
            await app.set_props_and_fire("test-unmount", {}, "click")
            await http.aclose()
            state.push.close()

    run(scenario())


def test_configured_origin_change_never_sends_previous_refresh_credentials() -> None:
    async def scenario() -> None:
        credentials = FakeCredentialStore()
        await credentials.set(REFRESH_KEY, "old-origin-refresh-secret")
        await credentials.set("identity_origin", "https://old-gw")
        prefs = Preferences({"relaydeck-endpoint": "https://new-gw"})
        requests: list[httpx.Request] = []

        def request(req: httpx.Request) -> httpx.Response:
            requests.append(req)
            return httpx.Response(500)

        app = await render_component(lambda: ft.Text("Starting"))

        async def browser(url: str) -> None:
            pass

        async with httpx.AsyncClient(transport=httpx.MockTransport(request)) as http:
            runtime = PackagedRuntime(
                app.page,
                credentials,
                prefs,
                browser,
                lambda: None,
                client=http,
                clipboard=FakeClipboard(),
            )
            await runtime.start()
            assert runtime.phase == "login" and requests == []
            assert await credentials.get(REFRESH_KEY) is None
            assert await credentials.get("identity_origin") == "https://new-gw"
            await runtime.close()

    run(scenario())


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8125",
        "https://u:p@gw",
        "https://gw/auth",
        "https://gw?token=secret",
        "https://gw#secret",
    ],
)
def test_packaged_endpoint_rejects_plaintext_paths_and_credentials(url: str) -> None:
    with pytest.raises(ValueError):
        private_endpoint(url)
