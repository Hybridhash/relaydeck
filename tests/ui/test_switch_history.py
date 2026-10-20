"""header agent switches do not grow the history stack.

Open agent A from the list (one history entry), switch to B and C with the header switcher
(no further entries), then a back gesture (route change to the list) lands on the list.
Actual Root controls and retained sessions; the recorded `go` calls are the route pushes.
"""

from __future__ import annotations

from typing import Any

import pytest

from relaydeck.client.features.inventory.routing import RouteBridge
from tests.support.host_harness import run
from tests.support.ui.instant_inventory import valid_inventory
from tests.support.ui.navigation import _StubTransport
from tests.support.ui.retained_root import _active, _open, _render


async def _fire(app: Any, key: str) -> None:
    control = _active(app, key)
    await app.session.dispatch_event(control._i, "click", "")
    await app.settle()


# The header agent switcher exists on the phone layout only.
@pytest.mark.parametrize("width", [390])
def test_switcher_picks_keep_one_history_entry_and_back_lands_on_list(
    width: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_StubTransport, "snapshot", valid_inventory)
    pushes: list[str] = []

    async def scenario() -> None:
        bridge = RouteBridge(route="/agents", width=width, height=844)
        app, _controller, _scripted, pool, _now = await _render(bridge=bridge, go=pushes.append)
        try:
            await _open(app, "home-mac:term-1")
            assert len(pushes) == 1, pushes
            assert bridge.state.selection == "home-mac:term-1"

            for key in ("home-mac:term-2", "home-mac:term-3"):
                await _fire(app, "agent-switcher-open")
                await _fire(app, f"agent-switcher-row-{key}")
            assert len(pushes) == 1, pushes
            assert bridge.state.selection == "home-mac:term-3"
            assert pool.foreground is not None

            # Back gesture: the browser's route change to the list.
            bridge.set_route("/agents")
            await app.settle()
            assert bridge.state.destination == "agents"
            assert bridge.state.selection == ""
            assert len(pushes) == 1, pushes
        finally:
            await app.set_props_and_fire("test-unmount", {}, "click")

    run(scenario())
