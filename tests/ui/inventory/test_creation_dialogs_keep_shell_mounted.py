"""creation dialogs must not remount the app shell.

Opening/closing Start agent or New terminal previously swapped Root's
return value between a bare shell and a Stack, which remounts children
and resets component state (query text, and — once mounted — terminal
emulator state held in refs). The overlay Stack is now structural:
always present, with the scrim child added/removed. Proven through the
shipped root with real dispatched events: shell component state
survives dialog open/close.
"""

from __future__ import annotations

from typing import Any

import flet as ft

from relaydeck.client.features.a11y.focus import iter_controls
from tests.support.host_harness import run
from tests.support.ui.navigation import _render_root, _state


def test_shell_state_survives_sheet_open_close() -> None:
    async def scenario() -> None:
        app, _, _, _, _ = await _render_root(_state())
        assert app.find("rail-slot").visible
        assert isinstance(app.find("console-rail-console"), ft.TextButton)
        roots: list[Any] = [app.page.views[0].controls]
        assert any(isinstance(c, ft.Stack) for c in iter_controls(roots)), (
            "Root must always render the structural overlay Stack"
        )
        # Seed visible search state, then open/close the dialog around it.
        await app.set_props_and_fire("console-search", {}, "click")
        await app.set_props_and_fire("inventory-query", {"value": "web"}, "change", "web")
        assert app.find("inventory-query").value == "web"
        await app.set_props_and_fire("console-new-agent", {}, "click")
        assert app.find("start-agent-sheet") is not None
        assert app.find("inventory-query").value == "web", (
            "sheet open must not remount the shell (query state reset)"
        )
        await app.set_props_and_fire("sheet-cancel", {}, "click")
        assert app.find("inventory-query").value == "web", (
            "sheet close must not remount the shell (query state reset)"
        )

    run(scenario())
