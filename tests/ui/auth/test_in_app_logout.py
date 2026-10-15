"""in-app logout seam pin (Root -> Settings -> prop).

This pins the shipped Root's logout callback seam with a real
dispatched click. The wiring that makes logout work is in serve.py
(run_app supplies a real teardown, flet_main flips to login), covered by
tests/integration/auth/test_logout_wiring.py plus the browser proof.
"""

from __future__ import annotations

from typing import Any

from relaydeck.client.app import Root
from relaydeck.client.features.inventory.routing import RouteBridge
from relaydeck.client.features.sync.backoff import Backoff
from relaydeck.domain import reducers
from relaydeck.domain.models import AppState
from tests.support.host_harness import render_component, run
from tests.support.scripted_sessions import make_scripted_sessions
from tests.support.ui.navigation import _state


class _StubController:
    def __init__(self) -> None:
        self.listeners: list[Any] = []
        self.backoff = Backoff.seeded(1)
        self.state = AppState()

    def subscribe(self, listener: Any) -> None:
        self.listeners.append(listener)

    def unsubscribe(self, listener: Any) -> None:
        if listener in self.listeners:
            self.listeners.remove(listener)

    async def run(self, host_id: str, stop: Any = lambda: False) -> AppState:
        return AppState()

    def push(self, state: AppState) -> None:
        self.state = state
        for listener in self.listeners:
            listener(state)

    def apply_grants(self, entries: list[dict[str, Any]]) -> AppState:
        """Mirror `SyncController.apply_grants` (grant ownership)."""
        self.push(reducers.merge_hosts(self.state, entries))
        return self.state

    def select(self, host_id: str, session_key: str, terminal_kind: str) -> AppState:
        """Mirror `SyncController.select` (selection ownership)."""
        self.push(reducers.select_session(self.state, host_id, session_key, terminal_kind))
        return self.state


class _StubTransport:
    async def submit_command(self, command: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError


def test_settings_logout_click_fires_prop() -> None:
    async def scenario() -> None:
        calls: list[str] = []
        controller = _StubController()
        app = await render_component(
            Root,
            controller=controller,
            transport=_StubTransport(),
            host_id="home-mac",
            bridge=RouteBridge(route="/", width=1280),
            go=lambda route: None,
            session_factory=make_scripted_sessions().factory,
            account="tester",
            on_logout=lambda: calls.append("logout"),
        )
        controller.push(_state())
        await app.settle()
        await app.set_props_and_fire("console-rail-settings", {}, "click")
        await app.set_props_and_fire("settings-logout", {}, "click")
        assert calls == ["logout"]

    run(scenario())
