"""The draft field keeps its Flutter wrapper path from the page root.

The earlier identity tests start at the agent screen. On a real iPad the split also
changes the AppShell side (list column, rail, retained layer, content padding), so
the wrapper signature is checked over the whole path from `Root` down to the field,
expanded tier, normal layout versus typing split.
"""

from __future__ import annotations

import asyncio
from typing import Any

from relaydeck.client.features.inventory.routing import RouteBridge
from relaydeck.client.hooks.support import TYPING_BLUR_DEBOUNCE_S
from tests.support.host_harness import run
from tests.support.ui.retained_root import _active, _open, _render
from tests.support.ui.tree_path import _path_to, _signature

DRAFT = "composer-draft"


def _path_rows(app: Any) -> list[tuple[Any, ...]]:
    return _signature(_path_to(app, DRAFT))


def _diff(normal: list[tuple[Any, ...]], split: list[tuple[Any, ...]]) -> list[str]:
    out: list[str] = []
    if len(normal) != len(split):
        out.append(f"depth {len(normal)} != {len(split)}")
    for index, (a, b) in enumerate(zip(normal, split, strict=False)):
        if a != b:
            out.append(f"[{index}] {a} != {b}")
    return out


def test_expanded_draft_path_from_root_keeps_its_wrappers_when_the_split_starts() -> None:
    async def scenario() -> None:
        bridge = RouteBridge(route="/", width=1376, height=1032)
        app, _controller, _sessions, _pool, _now = await _render(bridge=bridge, touch=True)
        await _open(app, "home-mac:term-1")
        await app.settle()
        field = _active(app, DRAFT)._i
        normal = _path_rows(app)

        # Keyboard: landscape visible height drops, composer gains focus.
        bridge.set_size(1376, 396)
        await app.settle()
        await app.set_props_and_fire(DRAFT, {}, "focus")
        await app.settle()
        assert _active(app, DRAFT)._i == field, "field remounted when the split started"
        split = _path_rows(app)
        assert app.find("rail-slot").visible is False, "the split did not start"
        assert _diff(normal, split) == []

        # Blur keeps the split on touch devices (the terminal can be used).
        await app.set_props_and_fire(DRAFT, {}, "blur")
        await asyncio.sleep(TYPING_BLUR_DEBOUNCE_S + 0.2)
        await app.settle()
        assert app.find("rail-slot").visible is False, "blur must not leave typing mode"
        assert _active(app, DRAFT)._i == field

        # Done leaves typing mode and restores the normal layout, same field.
        await app.set_props_and_fire("agent-typing-done", {}, "click")
        await app.settle()
        assert app.find("rail-slot").visible is True
        assert _active(app, DRAFT)._i == field
        assert _diff(normal, _path_rows(app)) == []

    run(scenario())
