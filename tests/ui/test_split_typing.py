"""touch typing mode (terminal 70 | input 30), no remount."""

from __future__ import annotations

import logging
from typing import Any

import flet as ft
import pytest

from relaydeck.client.features.composer.components import ComposerBox
from relaydeck.client.features.terminal.layout_budget import (
    TYPING_SPLIT_GAP,
    WINDOW_PADDING,
    typing_split_active,
    typing_split_lines,
    typing_split_widths,
)
from relaydeck.client.features.terminal.surfaces import (
    AGENT_DETAIL_KEY,
    AGENT_HEADER_KEY,
    AGENT_KEYS_SCROLL_KEY,
    AGENT_MORE_KEY,
    mount_agent_screen,
)
from tests.support.host_harness import render_component, run
from tests.support.terminal.sessions import _agent_session
from tests.support.ui.split_harness import split_mounted
from tests.support.ui.tree_path import _path_to, _signature, children_of, keys_of


@pytest.fixture(autouse=True)
def _no_handler_errors(caplog: pytest.LogCaptureFixture) -> Any:
    caplog.set_level(logging.ERROR)
    yield
    errors = [
        record.getMessage()
        for phase in ("setup", "call", "teardown")
        for record in caplog.get_records(phase)
        if record.levelno >= logging.ERROR
    ]
    assert errors == []


def _has(app: Any, key: str) -> bool:
    try:
        app.find(key)
    except KeyError:
        return False
    return True


def test_activation_rule_needs_a_touch_device_in_landscape_with_the_keyboard() -> None:
    def active(**kw: Any) -> bool:
        base: dict[str, Any] = {"focused": True, "viewport_width": 1376, "viewport_height": 396}
        return typing_split_active(**{**base, **kw})

    assert active(touch=True)
    assert not active(touch=False)  # a short desktop window is not a keyboard
    assert not active(touch=True, focused=False)
    # the browser keeps reporting the full height while the composer is
    # focused, so a focused composer in landscape splits on a touch device at any height.
    assert active(touch=True, viewport_height=1032)
    assert not active(touch=True, viewport_width=1032, viewport_height=1376)  # portrait
    # Landscape phones qualify at any width.
    assert active(touch=True, viewport_width=844, viewport_height=200)
    assert active(touch=True, viewport_width=932, viewport_height=230)
    assert not active(
        touch=True, viewport_width=390, viewport_height=450
    )  # portrait phone, keyboard
    # Without platform knowledge the legacy size rule applies.
    assert active()
    assert not active(viewport_width=1032, viewport_height=500)
    assert not active(viewport_width=1376, viewport_height=1032)  # desktop, keyboard closed


def test_widths_are_an_exact_70_30_split() -> None:
    terminal, side = typing_split_widths(1224.0)
    assert terminal + side + TYPING_SPLIT_GAP == pytest.approx(1224.0 - 40.0)
    assert side / (terminal + side) == pytest.approx(0.30, abs=0.002)
    terminal, side = typing_split_widths(844.0, padding=0.0, gap=8.0)
    assert 240 <= side <= 260  # the ~250 px phone input column


def test_field_lines_are_conservative_and_bounded() -> None:
    assert typing_split_lines(100, 200) == 2
    assert typing_split_lines(300, 100) == 8
    assert typing_split_lines(10_000, 0) == 40


def test_desktop_layout_is_unchanged_and_never_splits() -> None:
    async def scenario() -> None:
        session, _ops, _factory, _submitted = _agent_session()
        session.open()
        await session.confirm_takeover()
        app = await render_component(
            mount_agent_screen,
            session,
            width_tier="expanded",
            phone=False,
            viewport_height=300,
            touch=False,
            typing_split=True,
        )
        try:
            assert not _has(app, "agent-composer-slot")
            assert "agent-input-dock" in keys_of(app.find(AGENT_DETAIL_KEY))
            assert app.find("terminal-flex-host").margin.right == 0
        finally:
            await session.dispose()

    run(scenario())


def test_touch_normal_layout_uses_slots_with_a_spacer() -> None:
    async def scenario() -> None:
        app, session, _log = await split_mounted(split=False)
        try:
            order = keys_of(app.find(AGENT_DETAIL_KEY))
            assert "agent-input-spacer" in order and "agent-input-dock" not in order
            slot = app.find("agent-composer-slot")
            assert slot.right is not None and slot.bottom is not None and slot.width
            assert _has(app, "agent-input-controls")
            assert app.find("terminal-flex-host").margin.right == 0
            field = app.find("composer-draft")
            assert (field.min_lines, field.max_lines) == (3, 3)
            assert _has(app, "agent-window-state")  # full header
        finally:
            await session.dispose()

    run(scenario())


def test_split_layout_places_input_beside_the_terminal() -> None:
    async def scenario() -> None:
        app, session, _log = await split_mounted(split=True)
        try:
            _terminal, side = typing_split_widths(1224.0)
            assert app.find("agent-composer-slot").width == side
            assert app.find("agent-input-controls").width == side
            assert app.find("terminal-flex-host").margin.right == side + TYPING_SPLIT_GAP
            assert keys_of(app.find(AGENT_DETAIL_KEY)) == [
                AGENT_HEADER_KEY,
                "terminal-flex-host",
                "agent-input-spacer",
            ]
            assert app.find("agent-input-spacer").height == 0
            assert _has(app, "agent-keys-toggle") and _has(app, "agent-workflow-group")
            assert _has(app, AGENT_MORE_KEY)  # slim header: title, dot, more
            assert not _has(app, "agent-window-state")
        finally:
            await session.dispose()

    run(scenario())


def test_message_box_shows_many_lines_scrolls_inside_and_reports_focus() -> None:
    async def scenario() -> None:
        app, session, log = await split_mounted(split=True)
        try:
            field = app.find("composer-draft")
            assert field.min_lines == field.max_lines and field.max_lines > 5
            assert not field.autofocus
            await app.set_props_and_fire("composer-draft", {}, "focus")
            await app.set_props_and_fire("composer-draft", {}, "blur")
            assert log == [True, False]
            assert _has(app, "composer-send") and _has(app, "composer-attach-picture")
        finally:
            await session.dispose()

    run(scenario())


def test_dock_opens_inside_the_input_column_never_over_the_terminal() -> None:
    async def scenario() -> None:
        app, session, _log = await split_mounted(split=True, dock=True)
        try:
            assert _has(app, AGENT_KEYS_SCROLL_KEY)
            assert not _has(app, "agent-keys-overlay")
            assert AGENT_KEYS_SCROLL_KEY not in keys_of(app.find(AGENT_DETAIL_KEY))
            dock = app.find("agent-split-dock")
            assert dock.right is not None and dock.width and dock.height <= 0.34 * 480
            # the dock sits under the draft frame, top-anchored.
            assert dock.top is not None and dock.bottom is None
        finally:
            await session.dispose()

    run(scenario())


def _identity_scenario(*, phone: bool, field_key: str) -> Any:
    async def scenario() -> None:
        app, session, _log = await split_mounted(split=False, phone=phone)
        try:
            await session.type_text("unsent draft")
            host = app.find("terminal-flex-host")._i
            tap = app.find("terminal-direct-tap")._i
            controller = session.controller
            field_id = app.find(field_key)._i
            normal = _signature(_path_to(app, field_key))
            await app.set_props_and_fire("toggle-split", {}, "click")
            assert app.find("terminal-flex-host")._i == host
            assert app.find("terminal-direct-tap")._i == tap
            assert app.find(field_key)._i == field_id  # the draft field is the same control
            assert _signature(_path_to(app, field_key)) == normal
            assert app.find(field_key).value == "unsent draft"
            await app.set_props_and_fire("toggle-split", {}, "click")
            assert app.find("terminal-flex-host")._i == host
            assert app.find(field_key)._i == field_id
            assert _signature(_path_to(app, field_key)) == normal
            assert app.find(field_key).value == "unsent draft"
            assert session.controller is controller
        finally:
            await session.dispose()

    return scenario


def test_expanded_draft_field_and_terminal_survive_entering_and_leaving() -> None:
    run(_identity_scenario(phone=False, field_key="composer-draft")())


def test_phone_draft_field_and_terminal_survive_entering_and_leaving() -> None:
    run(_identity_scenario(phone=True, field_key="composer-draft")())


def test_phone_landscape_uses_the_same_split_geometry() -> None:
    async def scenario() -> None:
        app, session, _log = await split_mounted(split=True, phone=True)
        try:
            _terminal, side = typing_split_widths(1224.0, padding=0.0, gap=8.0)
            dock = app.find("agent-input-dock")
            assert dock.width == side and dock.right == 0 and dock.bottom == 0
            assert app.find("terminal-flex-host").margin.right == side + 8
            assert app.find("agent-input-spacer").height == 0
            field = app.find("composer-draft")
            assert field.min_lines == field.max_lines and field.max_lines >= 2
        finally:
            await session.dispose()

    run(scenario())


def test_phone_portrait_is_unchanged() -> None:
    async def scenario() -> None:
        app, session, _log = await split_mounted(split=False, phone=True)
        try:
            assert app.find("terminal-flex-host").margin.right == 0
            field = app.find("composer-draft")
            assert (field.min_lines, field.max_lines) == (2, 2)
        finally:
            await session.dispose()

    run(scenario())


def test_composer_defaults_are_unchanged() -> None:
    async def scenario() -> None:
        app = await render_component(
            ComposerBox,
            draft="",
            markers=(),
            can_send=False,
            blockers=(),
            submission="idle",
            error=None,
            review=None,
            input_mode="text",
            on_draft_change=lambda _t: None,
            on_send=lambda: None,
            on_confirm_blocked=lambda: None,
            on_cancel_blocked=lambda: None,
            on_clear_error=lambda: None,
        )
        field = app.find("composer-draft")
        assert (field.min_lines, field.max_lines) == (1, 5)
        assert not field.autofocus
        # Focus is tracked for the Ctrl/Cmd+Enter shortcut only; with no on_focus_change the
        # handlers report nowhere and must not raise.
        field.on_focus(None)
        field.on_blur(None)
        assert app.find("composer-box").expand is None

    run(scenario())


def _walk(node: Any) -> list[Any]:
    out = [node]
    for child in children_of(node):
        out.extend(_walk(child))
    return out


@pytest.mark.parametrize("phone", [False, True])
@pytest.mark.parametrize("split", [False, True])
def test_positioned_controls_never_carry_a_size_observer(split: bool, phone: bool) -> None:
    """Flet wraps the size observer outside Positioned; on a Stack child that
    breaks the parent data and renders a grey error box on the device."""

    async def scenario() -> None:
        app, session, _log = await split_mounted(split=split, phone=phone)
        try:
            positioned = [
                node
                for node in _walk(app.page.views[0].controls)
                if any(
                    getattr(node, a, None) is not None for a in ("left", "top", "right", "bottom")
                )
            ]
            assert positioned
            assert [n.key for n in positioned if getattr(n, "on_size_change", None)] == []
        finally:
            await session.dispose()

    run(scenario())


def test_device_matrix_for_the_typing_split_sec_1c() -> None:
    """which device classes split with the composer focused.

    `touch` is `is_touch_platform(page.platform)`. A real iPad reports platform `ios`
    (Flutter maps `MacIntel` with maxTouchPoints > 2 to iOS), so iPad Safari and Firefox
    (desktop-site UA) match the iPhone rows; a Mac and a non-touch Windows PC never do.
    """
    from relaydeck.client.app import is_touch_platform

    cases = [
        # (device, platform, width, height, expected)
        ("iPhone Safari landscape", ft.PagePlatform.IOS, 844, 390, True),
        ("iPhone Safari portrait", ft.PagePlatform.IOS, 390, 844, False),
        ("iPad landscape (keyboard open)", ft.PagePlatform.IOS, 1376, 396, True),
        ("iPad landscape (keyboard not reported)", ft.PagePlatform.IOS, 1376, 1032, True),
        ("iPad portrait", ft.PagePlatform.IOS, 1032, 1376, False),
        ("Android tablet landscape", ft.PagePlatform.ANDROID, 1280, 720, True),
        ("Mac Safari/Chrome window", ft.PagePlatform.MACOS, 1376, 1032, False),
        ("Mac Safari short window", ft.PagePlatform.MACOS, 1376, 396, False),
        ("Windows PC", ft.PagePlatform.WINDOWS, 1376, 396, False),
        ("Unknown platform", None, 1376, 396, False),
    ]
    for device, platform, width, height, expected in cases:
        touch = is_touch_platform(platform)
        got = typing_split_active(
            focused=True, viewport_width=width, viewport_height=height, touch=touch
        )
        assert got is expected, device


def test_split_frame_position_and_hint_row_are_the_same_empty_and_typed() -> None:
    """the frame is top-anchored and its hint row keeps its height, so
    the first keystroke (which clears the hint) does not move or resize the frame."""
    from relaydeck.client.features.composer.components import SPLIT_HINT_HEIGHT, VALIDATION_KEY

    async def scenario() -> None:
        app, session, _log = await split_mounted(split=True)
        try:
            empty_top = app.find("agent-composer-slot").top
            empty_hint = app.find(VALIDATION_KEY).height
            field = app.find("composer-draft")._i
            await session.type_text("hello")
            await app.settle()
            assert app.find("agent-composer-slot").top == empty_top
            assert app.find(VALIDATION_KEY).height == empty_hint == SPLIT_HINT_HEIGHT
            assert app.find("composer-draft")._i == field
            assert app.find("agent-composer-slot").bottom is None  # top-anchored
        finally:
            await session.dispose()

    run(scenario())


@ft.component
def _PolishHarness(session: Any, keyboard_up: bool) -> Any:
    return ft.Container(
        expand=True,
        content=mount_agent_screen(  # type: ignore[call-arg]
            session,
            width_tier="expanded",
            phone=False,
            viewport_height=900,
            touch=True,
            typing_split=True,
            keyboard_up=keyboard_up,
            on_composer_focus=lambda _focused: None,
        ),
    )


def test_split_polish_draft_is_capped_with_the_keyboard_down_and_fills_it_when_up() -> None:
    """keyboard down, the draft is sized to content (at most ~40% of the
    column); keyboard up, the draft fills what is left."""
    from relaydeck.client.features.composer.components import DRAFT_KEY

    async def lines(keyboard_up: bool) -> int:
        session, _ops, _factory, _submitted = _agent_session()
        session.open()
        await session.confirm_takeover()
        app = await render_component(_PolishHarness, session, keyboard_up)
        await app.set_props_and_fire(
            "agent-detail-panel", {}, "size_change", {"w": 1224.0, "h": 900.0}
        )
        count = app.find(DRAFT_KEY).max_lines
        await session.dispose()
        return count

    down = run(lines(False))
    up = run(lines(True))
    assert 3 <= down <= 40 and down < up


def test_split_polish_key_row_lives_in_the_column_and_keys_toggle_is_present() -> None:
    async def scenario() -> None:
        app, session, _log = await split_mounted(split=True)
        try:
            assert app.find("terminal-flex-host") is not None
            # The terminal window does not draw its own key row under the terminal.
            assert not _has(app, "terminal-keys")
            assert _has(app, "agent-keys-toggle")
        finally:
            await session.dispose()

    run(scenario())


def test_split_polish_animation_props_are_identical_in_both_layouts() -> None:
    async def scenario() -> None:
        app, session, _log = await split_mounted(split=False)
        try:
            normal = {
                key: (
                    app.find(key).animate_position is not None,
                    app.find(key).animate_size is not None,
                )
                for key in ("agent-composer-slot", "agent-input-controls")
            }
            await app.set_props_and_fire("toggle-split", {}, "click")
            split = {
                key: (
                    app.find(key).animate_position is not None,
                    app.find(key).animate_size is not None,
                )
                for key in ("agent-composer-slot", "agent-input-controls")
            }
            assert normal == split and all(all(value) for value in normal.values())
        finally:
            await session.dispose()

    run(scenario())


@ft.component
def _UnmeasuredHarness(session: Any, viewport_width: float) -> Any:
    """a split screen before any size event has measured it."""
    return ft.Container(
        expand=True,
        content=mount_agent_screen(  # type: ignore[call-arg]
            session,
            width_tier="expanded",
            phone=False,
            viewport_height=800,
            viewport_width=viewport_width,
            touch=True,
            typing_split=True,
        ),
    )


def test_unmeasured_split_uses_the_viewport_width_not_the_800_fallback() -> None:
    async def scenario() -> None:
        session, _ops, _factory, _submitted = _agent_session()
        session.open()
        await session.confirm_takeover()
        app = await render_component(_UnmeasuredHarness, session, 1376.0)
        try:
            _terminal, side = typing_split_widths(1376.0)
            assert side > 0.30 * (1376.0 - 2 * WINDOW_PADDING - TYPING_SPLIT_GAP) - 2
            assert side != typing_split_widths(800.0)[1]
            assert app.find("agent-composer-slot").width == side
            assert app.find("agent-input-controls").width == side
            assert _has(app, "agent-keys-toggle") and _has(app, "agent-workflow-group")
        finally:
            await session.dispose()

    run(scenario())


def test_measured_width_wins_once_a_size_event_arrives() -> None:
    async def scenario() -> None:
        app, session, _log = await split_mounted(split=True)
        try:
            await app.set_props_and_fire(
                "agent-detail-panel", {}, "size_change", {"w": 900.0, "h": 480.0}
            )
            _terminal, side = typing_split_widths(900.0)
            assert app.find("agent-composer-slot").width == side
        finally:
            await session.dispose()

    run(scenario())
