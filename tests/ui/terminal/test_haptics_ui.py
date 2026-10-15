"""In the rendered app: the Settings switch, and a desktop Send through the real composer.

The haptic service is a recording double patched in at `page_haptic_service`; the rest of the
path (composer, prefs, mount wiring) is real.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from relaydeck.client.features.inventory.routing import RouteBridge
from relaydeck.client.features.settings.components import SettingsPage
from relaydeck.client.features.settings.prefs import PREFS_KEY, TerminalPrefs
from relaydeck.client.features.terminal import haptics
from tests.support.host_harness import render_component, run
from tests.support.ui.instant_inventory import valid_inventory
from tests.support.ui.navigation import _StubTransport
from tests.support.ui.retained_root import _open, _render


class Service:
    def __init__(self) -> None:
        self.calls = 0

    async def light_impact(self) -> None:
        self.calls += 1


class Store:
    """Preferences store double (async get/set), as the Root takes it in tests."""

    def __init__(self, haptics_on: bool) -> None:
        self.data = {PREFS_KEY: '{"haptics": %s}' % ("true" if haptics_on else "false")}

    async def get(self, key: str) -> str | None:
        return self.data.get(key)

    async def set(self, key: str, value: str) -> None:
        self.data[key] = value


def test_settings_switch_toggles_the_haptics_pref() -> None:
    changes: list[TerminalPrefs] = []

    async def scenario() -> None:
        app = await render_component(
            SettingsPage,
            prefs=TerminalPrefs(),
            on_prefs=changes.append,
            phone=False,
            account="synthetic",
            version="0.1.0",
            on_logout=lambda: None,
        )
        assert "Haptic feedback" in _texts(app.find("settings-pane"))
        assert "Not available in iPhone/iPad browsers" in _texts(app.find("settings-pane"))
        await app.set_props_and_fire("settings-haptics", {}, "click")
        assert changes == [TerminalPrefs(haptics=False)]

    run(scenario())


def _texts(control: Any) -> str:
    found: list[str] = []
    stack = [control]
    while stack:
        node = stack.pop()
        value = getattr(node, "value", None)
        if isinstance(value, str):
            found.append(value)
        text = getattr(node, "text", None)
        if isinstance(text, str):
            found.append(text)
        stack.extend(getattr(node, "controls", None) or [])
        content = getattr(node, "content", None)
        if content is not None:
            stack.append(content)
    return " ".join(found)


@pytest.mark.parametrize(
    "haptics_on,draft,expect",
    [(True, "hello", 1), (False, "hello", 0), (True, "", 0)],
    ids=["on-sent", "off-sent", "on-empty-draft"],
)
def test_desktop_send_taps_once_when_on_and_sent_and_never_otherwise(
    haptics_on: bool, draft: str, expect: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = Service()
    monkeypatch.setattr(haptics, "page_haptic_service", lambda: service)
    monkeypatch.setattr(_StubTransport, "snapshot", valid_inventory)
    # Ready-at-tap: record the tap count at the send's first suspension, i.e. after the
    # tap was scheduled and before the send's own work (delivery) starts.
    real_on_send = haptics.on_send
    taps_inside_send: list[int] = []

    def spy_on_send(send: Any, enabled: bool, ready: Any) -> Any:
        async def timed() -> Any:
            await asyncio.sleep(0)
            taps_inside_send.append(service.calls)
            return await send()

        return real_on_send(timed, enabled, ready)

    monkeypatch.setattr(haptics, "on_send", spy_on_send)

    async def scenario() -> None:
        app, _controller, _scripted, _pool, _now = await _render(
            bridge=RouteBridge(route="/agents", width=1280, height=844),
            preferences=Store(haptics_on),
        )
        try:
            await _open(app, "home-mac:term-1")
            await app.settle()
            if draft:
                await app.set_props_and_fire("composer-draft", {}, "change", draft)
                await app.settle()
            await app.set_props_and_fire("composer-send", {}, "click")
            await app.settle()
            await asyncio.sleep(0.05)  # the tap is a scheduled task; let it run
            await app.settle()
        finally:
            await app.set_props_and_fire("test-unmount", {}, "click")
        assert service.calls == expect
        # Tapped before the send's own work started, and only when the send was ready.
        assert taps_inside_send == [expect]

    run(scenario())
