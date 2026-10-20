"""Phone tools popover: keys, mode switch and actions, and opening it never moves the terminal."""

from __future__ import annotations

from typing import Any

import pytest

from relaydeck.client.features.terminal import input_modes
from relaydeck.client.features.terminal.key_bar import KEY_BAR_GROUPS, KeyBarItem
from relaydeck.client.features.terminal.surfaces.tools_popover import ToolAction, ToolsPopover
from tests.support.host_harness import render_component, run
from tests.support.terminal.sessions import _agent_session
from tests.support.terminal.touch_keyboard import _Screen

ITEMS = {item.id: item for group in KEY_BAR_GROUPS for item in group}


def _has(app: Any, key: str) -> bool:
    try:
        app.find(key)
    except KeyError:
        return False
    return True


def _popover(**over: Any) -> dict[str, Any]:
    calls: dict[str, list[Any]] = {"item": [], "mode": [], "close": [], "action": []}
    props: dict[str, Any] = {
        "top": 32,
        "on_close": lambda: calls["close"].append(1),
        "on_item": lambda item, hold: calls["item"].append((item.id, hold)),
        "modifier_states": {"ctrl": "locked"},
        "keys_enabled": True,
        "show_keys": True,
        "mode": "compose",
        "on_mode": lambda mode: calls["mode"].append(mode),
        "actions": (
            ToolAction("tools-paste", "Paste to terminal", None, lambda: calls["action"].append(1)),
        ),
    }
    props.update(over)
    props["calls"] = calls
    return props


async def _render(props: dict[str, Any]) -> tuple[Any, dict[str, list[Any]]]:
    calls = props.pop("calls")
    return await render_component(ToolsPopover, **props), calls


def test_a_key_sends_its_item_and_keeps_the_panel_open() -> None:
    async def scenario() -> None:
        app, calls = await _render(_popover())
        await app.set_props_and_fire("tools-key-esc", {}, "click")
        await app.set_props_and_fire("tools-key-up", {}, "long_press")
        assert calls["item"] == [("key-esc", False), ("key-up", True)]
        assert calls["close"] == []

    run(scenario())


def test_modifiers_show_the_shared_latch_and_keys_hide_when_not_offered() -> None:
    async def scenario() -> None:
        app, _calls = await _render(_popover())
        assert app.find("tools-mod-ctrl").data == {"latch": "locked"}
        quiet, _ = await _render(_popover(show_keys=False))
        assert _has(quiet, "tools-mod-ctrl") and not _has(quiet, "tools-key-esc")

    run(scenario())


def test_mode_switch_and_actions_close_the_panel() -> None:
    async def scenario() -> None:
        app, calls = await _render(_popover())
        await app.set_props_and_fire("tools-mode-direct", {}, "click")
        await app.set_props_and_fire("tools-paste", {}, "click")
        assert calls["mode"] == ["direct"] and calls["action"] == [1]
        assert len(calls["close"]) == 2

    run(scenario())


def test_async_actions_like_select_and_paste_run_to_completion() -> None:
    async def scenario() -> None:
        done: list[str] = []

        async def select() -> None:
            done.append("select")

        async def paste() -> None:
            done.append("paste")

        actions = (
            ToolAction("tools-select-text", "Select text", None, select),
            ToolAction("tools-paste", "Paste to terminal", None, paste),
        )
        app, calls = await _render(_popover(actions=actions))
        await app.set_props_and_fire("tools-select-text", {}, "click")
        await app.set_props_and_fire("tools-paste", {}, "click")
        assert done == ["select", "paste"] and len(calls["close"]) == 2

    run(scenario())


@pytest.mark.parametrize("key", ["tools-popover-close", "tools-popover-scrim"])
def test_close_button_and_outside_tap_close(key: str) -> None:
    async def scenario() -> None:
        app, calls = await _render(_popover())
        await app.set_props_and_fire(key, {}, "tap" if key.endswith("scrim") else "click")
        assert calls["close"] == [1]

    run(scenario())


def test_every_bar_key_is_offered() -> None:
    async def scenario() -> None:
        app, _calls = await _render(_popover())
        keys = [item for item in ITEMS.values() if item.kind != "action"]
        assert all(_has(app, f"tools-{item.id}") for item in keys)
        assert isinstance(keys[0], KeyBarItem)

    run(scenario())


def _shown(app: Any, key: str) -> bool:
    return _has(app, key) and app.find(key).visible is not False


def _layers(app: Any) -> list[int]:
    return [control._i for control in app.find("phone-select-stack").controls]


def test_opening_the_popover_never_moves_the_terminal_or_the_dock() -> None:
    async def scenario() -> None:
        session, _ops, _factory, _submitted = _agent_session()
        session.open()
        await session.confirm_takeover()
        session.set_workflow(input_modes.WORKFLOW_AGENT_COMPOSER)
        app = await render_component(_Screen, session, True, True)
        try:
            closed = _layers(app)
            spacer = app.find("agent-input-spacer").height
            assert not _shown(app, "tools-popover")
            await app.set_props_and_fire("terminal-tools-button", {}, "click")
            assert _shown(app, "tools-popover")
            assert app.find("terminal-scroll-overlay").visible is False
            # The popover stays mounted and only shows: every layer keeps its identity.
            assert _layers(app) == closed
            assert app.find("agent-input-spacer").height == spacer
            await app.set_props_and_fire("tools-key-esc", {}, "click")
            assert _shown(app, "tools-popover")
            await app.set_props_and_fire("tools-popover-close", {}, "click")
            assert not _shown(app, "tools-popover") and _layers(app) == closed
            assert app.find("agent-input-spacer").height == spacer
        finally:
            await session.dispose()

    run(scenario())


def test_choosing_direct_input_opens_the_typing_row() -> None:
    async def scenario() -> None:
        session, _ops, _factory, _submitted = _agent_session()
        session.open()
        await session.confirm_takeover()
        app = await render_component(_Screen, session, True, True)
        try:
            assert app.find("agent-tab-row").visible is False
            await app.set_props_and_fire("terminal-tools-button", {}, "click")
            await app.set_props_and_fire("tools-mode-direct", {}, "click")
            assert not _shown(app, "tools-popover")
            assert app.find("agent-tab-row").visible is True
        finally:
            await session.dispose()

    run(scenario())


@pytest.mark.parametrize("phone", [True, False])
def test_a_paste_review_floats_above_the_dock_without_moving_the_terminal(phone: bool) -> None:
    async def scenario() -> None:
        session, _ops, _factory, _submitted = _agent_session()
        session.open()
        await session.confirm_takeover()
        session.set_workflow(input_modes.WORKFLOW_AGENT_COMPOSER)
        app = await render_component(_Screen, session, phone, phone)
        try:
            closed = _layers(app)
            frame = app.find("agent-detail-panel")._i
            await session.request_paste("one\ntwo\nthree", require_review=True)
            await app.settle()
            assert _has(app, "paste-review-layer") and _has(app, "paste-review-confirm")
            opened = _layers(app)
            assert opened[:-1] == closed and len(opened) == len(closed) + 1
            assert app.find("agent-detail-panel")._i == frame
            await app.set_props_and_fire("paste-review-cancel", {}, "click")
            assert not _has(app, "paste-review-layer") and _layers(app) == closed
        finally:
            await session.dispose()

    run(scenario())
