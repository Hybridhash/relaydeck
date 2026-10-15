"""Tablet More-keys dock dedupes against the inline key row."""

from __future__ import annotations

from typing import Any

import pytest

from relaydeck.client.components.design_system.terminal_window import KEY_ROW
from relaydeck.client.features.terminal.keys import (
    DOCK_KEYS,
    KEYS_DISMISS_KEY,
    KEYS_MOD_ROW_KEY,
    MOD_ALT,
    MOD_CTRL,
    MOD_SHIFT,
    ToolsKeysDock,
    modifier_button_key,
)
from tests.support.host_harness import render_component, run

INLINE = frozenset(name for name, _, _ in KEY_ROW)
EXTRA = frozenset(name for name, _, _ in DOCK_KEYS if name not in INLINE)


def _noop(*args: Any, **kwargs: Any) -> None:
    return None


def _deduped_names(app: Any) -> list[str]:
    row = app.find("tools-keys-deduped")
    assert row.wrap is True
    names = []
    for control in (key for group in row.controls for key in group.controls):
        assert str(control.key).startswith("shortcut-key-"), control.key
        names.append(str(control.key).removeprefix("shortcut-key-"))
    return names


def test_tablet_dock_agent_page_shows_only_non_inline_keys() -> None:
    assert EXTRA, "deduped dock set must be non-empty"

    async def scenario() -> None:
        app = await render_component(
            ToolsKeysDock,
            armed=frozenset(),
            enabled=True,
            on_key=_noop,
            on_toggle_modifier=_noop,
            on_dismiss=_noop,
            presentation="agent",
            dark=True,
            dedupe_inline=True,
        )
        await app.settle()
        shown = _deduped_names(app)
        assert shown, "agent page must never be empty"
        for name in shown:
            assert name not in INLINE, name
        # The agent quick set duplicates the inline row key for key, so
        # the page falls back to the full deduped set (spec item 1).
        assert frozenset(shown) == EXTRA
        assert app.find("tools-page-switch") is not None
        assert app.find(KEYS_DISMISS_KEY) is not None
        with pytest.raises(KeyError):
            app.find(KEYS_MOD_ROW_KEY)

    run(scenario())


def test_tablet_dock_terminal_page_keeps_modifiers_and_routes_keys() -> None:
    received: list[str] = []
    toggled: list[str] = []

    async def scenario() -> None:
        app = await render_component(
            ToolsKeysDock,
            armed=frozenset({MOD_CTRL}),
            enabled=True,
            on_key=lambda name, *_: received.append(name),
            on_toggle_modifier=lambda mod, *_: toggled.append(mod),
            on_dismiss=_noop,
            presentation="terminal",
            dark=True,
            dedupe_inline=True,
        )
        await app.settle()
        assert frozenset(_deduped_names(app)) == EXTRA
        assert app.find(KEYS_MOD_ROW_KEY) is not None
        assert app.find(modifier_button_key(MOD_CTRL)) is not None
        assert app.find(modifier_button_key(MOD_ALT)) is not None
        assert app.find(modifier_button_key(MOD_SHIFT)) is not None
        assert app.find(KEYS_DISMISS_KEY) is not None
        await app.set_props_and_fire("shortcut-key-home", {}, "click")
        assert received == ["home"]
        await app.set_props_and_fire(modifier_button_key(MOD_ALT), {}, "click")
        assert toggled == [MOD_ALT]

    run(scenario())


def test_phone_dock_keeps_legacy_stretched_grid() -> None:
    async def scenario() -> None:
        app = await render_component(
            ToolsKeysDock,
            armed=frozenset(),
            enabled=True,
            on_key=_noop,
            on_toggle_modifier=_noop,
            on_dismiss=_noop,
            presentation="agent",
            dark=True,
        )
        await app.settle()
        with pytest.raises(KeyError):
            app.find("tools-keys-deduped")
        # Legacy grid keeps its own key namespace and duplicates.
        assert app.find("tools-key-backspace") is not None

    run(scenario())


def test_compact_dock_wraps_every_key_in_tight_groups() -> None:
    """Collapsed (keyboard-open) overlay: inline row hidden, so ALL agent keys
    show, as tight Row groups inside the Wrap (Flet 1.0.3 grey-box rule)."""

    async def scenario() -> None:
        app = await render_component(
            ToolsKeysDock,
            armed=frozenset(),
            enabled=True,
            on_key=_noop,
            on_toggle_modifier=_noop,
            on_dismiss=_noop,
            presentation="agent",
            dark=True,
            compact=True,
            max_height=160,
        )
        await app.settle()
        row = app.find("tools-keys-deduped")
        assert row.wrap is True
        assert all(group.tight and not group.wrap for group in row.controls)
        shown = set(_deduped_names(app))
        assert INLINE & shown  # inline keys are NOT deduped away here
        assert app.find(KEYS_DISMISS_KEY) is not None

    run(scenario())
