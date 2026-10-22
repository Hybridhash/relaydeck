"""Split tools stay over the terminal, independent of composer focus and height."""

from __future__ import annotations

from typing import Any

import pytest

from tests.support.host_harness import run
from tests.support.ui.split_harness import split_mounted
from tests.support.ui.tree_path import children_of


async def _measure_composer(app: Any, height: float) -> None:
    """The composer frame reports its size, as the renderer does after a layout."""
    frame = app.find("agent-composer-slot").content
    await app.session.dispatch_event(frame._i, "size_change", {"w": 380.0, "h": height})
    await app.settle()


def _first_key(app: Any) -> str:
    queue = [app.find("tools-popover-scroll")]
    while queue:
        node = queue.pop(0)
        key = getattr(node, "key", None) or ""
        if key.startswith("tools-key-"):
            return key.removeprefix("tools-key-")
        queue.extend(children_of(node))
    raise AssertionError("no key buttons rendered")


def test_tools_stay_over_the_terminal_with_field_focused_and_blurred() -> None:
    async def scenario() -> None:
        app, session, _log = await split_mounted(split=True, dock=True)
        try:
            await app.set_props_and_fire("agent-tools-toggle", {}, "click")
            await _measure_composer(app, 180.0)
            panel = app.find("tools-popover-panel")
            bounds = (panel.top, panel.right, panel.bottom)
            assert panel.right > app.find("agent-composer-slot").width

            await app.set_props_and_fire("composer-draft", {}, "focus")
            await _measure_composer(app, 260.0)
            panel = app.find("tools-popover-panel")
            assert (panel.top, panel.right, panel.bottom) == bounds

            await app.set_props_and_fire("composer-draft", {}, "blur")
            await _measure_composer(app, 180.0)
            panel = app.find("tools-popover-panel")
            assert (panel.top, panel.right, panel.bottom) == bounds
            assert app.find("tools-popover").visible
            with pytest.raises(KeyError):
                app.find("agent-split-dock")
        finally:
            await session.dispose()

    run(scenario())


def test_primary_row_is_first_in_both_keyboard_states() -> None:
    async def scenario() -> None:
        app, session, _log = await split_mounted(split=True, dock=True)
        try:
            await app.set_props_and_fire("agent-tools-toggle", {}, "click")
            assert _first_key(app) == "esc"
            await app.set_props_and_fire("composer-draft", {}, "focus")
            assert _first_key(app) == "esc"
        finally:
            await session.dispose()

    run(scenario())


def test_reopening_tools_keeps_the_same_controls_and_primary_keys() -> None:
    async def scenario() -> None:
        app, session, _log = await split_mounted(split=True, dock=True)
        try:
            await app.set_props_and_fire("agent-tools-toggle", {}, "click")
            first = app.find("tools-popover-scroll")._i
            composer = app.find("composer-draft")._i
            await app.set_props_and_fire("agent-tools-toggle", {}, "click")
            assert not app.find("tools-popover").visible
            await app.set_props_and_fire("agent-tools-toggle", {}, "click")
            assert app.find("tools-popover").visible
            assert app.find("tools-popover-scroll")._i == first
            assert app.find("composer-draft")._i == composer
            assert _first_key(app) == "esc"
        finally:
            await session.dispose()

    run(scenario())
