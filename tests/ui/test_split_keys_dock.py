"""the split key dock sits directly under the composer frame, shows the
primary row first, and opens at the top each time. Synthetic session; no network.
"""

from __future__ import annotations

from typing import Any

from relaydeck.client.features.terminal.surfaces import AGENT_KEYS_SCROLL_KEY
from tests.support.host_harness import run
from tests.support.ui.split_harness import split_mounted
from tests.support.ui.tree_path import children_of

PRIMARY = ("escape", "tab", "shift-tab", "left", "up", "down", "right", "backspace", "enter")


async def _measure_composer(app: Any, height: float) -> None:
    """The composer frame reports its size, as the renderer does after a layout."""
    frame = app.find("agent-composer-slot").content
    await app.session.dispatch_event(frame._i, "size_change", {"w": 380.0, "h": height})
    await app.settle()


def _first_key(app: Any) -> str:
    queue = [app.find(AGENT_KEYS_SCROLL_KEY)]
    while queue:
        node = queue.pop(0)
        key = getattr(node, "key", None) or ""
        if key.startswith("shortcut-key-"):
            return key.removeprefix("shortcut-key-")
        queue.extend(children_of(node))
    raise AssertionError("no key buttons rendered")


def test_dock_sits_under_the_composer_frame_with_field_focused_and_blurred() -> None:
    async def scenario() -> None:
        app, session, _log = await split_mounted(split=True, dock=True)
        try:
            await _measure_composer(app, 180.0)
            slot = app.find("agent-composer-slot")
            dock = app.find("agent-split-dock")
            assert dock.top == slot.top + 180.0 + 8.0
            assert dock.bottom is None

            await app.set_props_and_fire("composer-draft", {}, "focus")
            await _measure_composer(app, 260.0)
            dock = app.find("agent-split-dock")
            assert dock.top == app.find("agent-composer-slot").top + 260.0 + 8.0

            await app.set_props_and_fire("composer-draft", {}, "blur")
            await _measure_composer(app, 180.0)
            dock = app.find("agent-split-dock")
            assert dock.top == app.find("agent-composer-slot").top + 180.0 + 8.0
        finally:
            await session.dispose()

    run(scenario())


def test_primary_row_is_first_in_both_keyboard_states() -> None:
    async def scenario() -> None:
        app, session, _log = await split_mounted(split=True, dock=True)
        try:
            assert _first_key(app) == PRIMARY[0]
            await app.set_props_and_fire("composer-draft", {}, "focus")
            assert _first_key(app) == PRIMARY[0]
        finally:
            await session.dispose()

    run(scenario())


def test_reopening_the_dock_starts_scrolled_to_the_top() -> None:
    async def scenario() -> None:
        app, session, _log = await split_mounted(split=True, dock=True)
        try:
            first = _scroll_key(app)
            await app.set_props_and_fire("agent-keys-toggle", {}, "click")  # close Keys
            await app.set_props_and_fire("agent-keys-toggle", {}, "click")  # open Keys again
            second = _scroll_key(app)
            assert second != first  # a fresh column mounts, so its scroll offset is zero
            assert _first_key(app) == PRIMARY[0]
        finally:
            await session.dispose()

    run(scenario())


def _scroll_key(app: Any) -> str:
    column = app.find("agent-split-dock").content
    return str(column.key)
