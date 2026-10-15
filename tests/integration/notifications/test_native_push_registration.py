"""Packaged registration/taps over actual auth/gateway, with native calls doubled."""

from typing import Any

import flet as ft
import httpx
import pytest

from relaydeck.client.adapters.fake import FakeClipboard, FakeCredentialStore, FakeNotifications
from relaydeck.client.adapters.ports import PushPermission, PushTap
from relaydeck.client.features.auth.service import REFRESH_KEY
from relaydeck.client.notifications_runtime import NotificationRuntime
from relaydeck.client.packaged_runtime import PackagedRuntime
from relaydeck.domain.models import scope_key
from relaydeck.gateway.app import create_app
from tests.security.helpers import ALL_CAPS, NOW, make_state
from tests.support.host_harness import render_component, run
from tests.support.sync.transport_fixtures import _wait
from tests.support.ui.packaged import Preferences


@pytest.mark.parametrize("permission", [PushPermission.GRANTED, PushPermission.DENIED])
def test_native_permission_registration_rotation_authenticated_tap_and_logout(
    permission: PushPermission,
) -> None:
    async def scenario() -> None:
        state, backend = make_state()
        _, refresh = state.sessions.create(
            "acct-1", ALL_CAPS, frozenset({"home-mac"}), NOW, "native-login"
        )
        secure = FakeCredentialStore()
        await secure.set(REFRESH_KEY, refresh)
        await secure.set("identity_origin", "https://gw")
        prefs = Preferences({"relaydeck-endpoint": "https://gw"})
        port = FakeNotifications(permission=permission, token="synthetic-token-1")
        requests: list[str] = []

        async def requested(request: httpx.Request) -> None:
            requests.append(f"{request.method} {request.url.path}")

        async def browser(url: str) -> None:
            pytest.fail("Push must not silently authorize or open a browser")

        app = await render_component(lambda: ft.Text("Host harness"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(state)),
            event_hooks={"request": [requested]},
        ) as client:
            runtime = PackagedRuntime(
                app.page,
                secure,
                prefs,
                browser,
                lambda: None,
                client=client,
                clipboard=FakeClipboard(),
            )
            push = NotificationRuntime(port, runtime)
            runtime.notifications = push
            try:
                await runtime.start()
                await push.sync()
                assert runtime.input_allowed() and port.permission_requests == 0
                if permission == PushPermission.DENIED:
                    assert state.push.token_for("acct-1", "native-login") is None
                    await push.enable()
                    assert port.permission_requests == 1
                    assert "Notifications are off" in push.notice and runtime.input_allowed()
                else:
                    assert state.push.token_for("acct-1", "native-login") == "synthetic-token-1"
                    await push.token_changed("synthetic-token-2")
                    assert state.push.token_for("acct-1", "native-login") == "synthetic-token-2"
                    assert state.push.subscription_count("acct-1") == 1
                    rows = state.push.notify(
                        account="acct-1",
                        account_hosts={"home-mac"},
                        host_id="home-mac",
                        session_id="term-1",
                        event_id="event-1",
                        category="attention",
                        now=NOW,
                    )
                    payload = state.push.hint_payload(rows[0]["notification_id"])
                    tap = PushTap(
                        payload["notification_id"], payload["event_ref"], payload["category"]
                    )
                    runtime.background()
                    assert runtime._pause_task is not None
                    await runtime._pause_task
                    await push.tap(tap)
                    assert push._pending is not None
                    runtime.foreground = True
                    await runtime.resume()
                    await _wait(
                        lambda: runtime.resources is not None
                        and runtime.resources.bridge.state.selection
                        == scope_key("home-mac", "term-1")
                    )
                    assert runtime.resources is not None
                    assert runtime.resources.bridge.state.selection == scope_key(
                        "home-mac", "term-1"
                    )
                    assert backend.commands == {} and backend.input_logs == {}
                    assert not any(
                        "/attachments" in request or "/commands" in request for request in requests
                    )
                    previous = runtime.resources.bridge.state
                    await push.tap(PushTap(tap.notification_id, "forged-ref", tap.category))
                    assert runtime.resources is not None
                    assert runtime.resources.bridge.state == previous
                    await push.app_link("https://foreign.example/app/n/rdl_syntheticfake")
                    assert runtime.resources is not None
                    assert runtime.resources.bridge.state == previous
                generation = push.generation
                await runtime.logout()
                await push.token_changed("late-token", generation=generation)
                assert port.revokes == 1 and push._token == ""
                assert state.push.token_for("acct-1", "native-login") is None
                assert not runtime.input_allowed()
                assert "synthetic-token" not in str(prefs.values)
            finally:
                await runtime.close()
                state.push.close()

    run(scenario())


def test_extension_permission_does_not_treat_a_cached_token_as_permission_and_unsubscribes(
    monkeypatch: Any,
) -> None:
    from relaydeck_notifications.notifications import RelayDeckNotifications

    from relaydeck.client.adapters.notifications import ExtensionNotifications

    async def scenario() -> None:
        service = RelayDeckNotifications()
        methods: list[str] = []

        async def invoke(method: str, *args: Any, **kwargs: Any) -> Any:
            methods.append(method)
            return {"permission": "denied", "get_token": "cached-token", "delete_token": None}[
                method
            ]

        monkeypatch.setattr(service, "_invoke_method", invoke)
        port = ExtensionNotifications(service)
        assert await port.permission() == PushPermission.DENIED
        assert methods == ["permission"]
        tokens: list[str] = []
        unsubscribe = port.on_token_refresh(tokens.append)
        service._test_event("push_token", {"token": "first"})
        unsubscribe()
        unsubscribe()
        service._test_event("push_token", {"token": "late"})
        assert tokens == ["first"]
        await port.revoke()
        assert methods[-1] == "delete_token"

    run(scenario())
