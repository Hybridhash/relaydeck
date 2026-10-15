"""Dock order/bounds, short collapse, touch/media signals."""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import Any

import flet as ft
import pytest

from relaydeck.client.app import is_touch_platform
from relaydeck.client.features.terminal.input_modes import KEYBOARD_KEYS
from relaydeck.client.features.terminal.screen import TOOLBAR_KEY
from relaydeck.client.features.terminal.surfaces import (
    AGENT_DETAIL_KEY,
    AGENT_KEYS_SCROLL_KEY,
    mount_agent_screen,
)
from relaydeck.client.resources import visible_page_height
from tests.support.host_harness import render_component, run
from tests.support.terminal.sessions import _agent_session


async def _mounted(viewport_height: float, *, touch: bool = False, dock_open: bool = False) -> Any:
    session, _ops, _factory, _submitted = _agent_session()
    session.open()
    await session.confirm_takeover()
    if dock_open:
        session.set_keyboard(KEYBOARD_KEYS)
    app = await render_component(
        mount_agent_screen,
        session,
        width_tier="expanded",
        phone=False,
        viewport_height=viewport_height,
        touch=touch,
    )
    return app, session


def _detail_order(app: Any) -> list[str]:
    return [control.key for control in app.find(AGENT_DETAIL_KEY).controls]


def test_dock_sits_above_picture_and_composer() -> None:
    async def scenario() -> None:
        app, session = await _mounted(1032, dock_open=True)
        try:
            order = _detail_order(app)
            dock = order.index(AGENT_KEYS_SCROLL_KEY)
            picture = order.index("picture-upload-status")
            composer = order.index("agent-input-dock")
            assert dock < picture < composer
            assert order.index("terminal-flex-host") < dock
        finally:
            await session.dispose()

    run(scenario())


def _error_records(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        record.getMessage()
        for phase in ("setup", "call", "teardown")
        for record in caplog.get_records(phase)
        if record.levelno >= logging.ERROR
    ]


@pytest.fixture(autouse=True)
def _no_handler_errors(caplog: pytest.LogCaptureFixture) -> Any:
    """A dispatch error is caught and logged by the session: fail on it.

    Checked across setup, call and teardown (the old version only saw
    teardown records)."""
    caplog.set_level(logging.ERROR)
    yield
    assert _error_records(caplog) == []


async def _fire(app: Any, key: str, height: float, width: float = 900.0) -> None:
    # Flet's LayoutSizeChangeEvent decodes w/h.
    await app.set_props_and_fire(key, {}, "size_change", {"w": width, "h": height})


async def _fire_detail_height(app: Any, height: float) -> None:
    await _fire(app, "agent-detail-panel", height)


def _has(app: Any, key: str) -> bool:
    try:
        app.find(key)
    except KeyError:
        return False
    return True


def _overlay_fits(app: Any, detail: float) -> None:
    overlay = app.find("agent-keys-overlay")
    assert overlay.height is not None and overlay.height >= 0
    assert overlay.bottom + overlay.height <= detail


@pytest.mark.parametrize(
    ("viewport", "detail", "inline", "overlay", "collapsed"),
    [
        # Fallback detail would be viewport - 96; these differ so the
        # measured event must drive the result.
        (1032, 936, True, False, False),
        (1032, 700, False, True, False),
        (1376, 1280, True, False, False),
        (700, 604, False, True, True),
        (700, 330, False, True, True),
    ],
)
def test_dock_yields_to_terminal_rows(
    viewport: float, detail: float, inline: bool, overlay: bool, collapsed: bool
) -> None:
    async def scenario() -> None:
        app, session = await _mounted(viewport, dock_open=True)
        try:
            await _fire_detail_height(app, detail)
            order = _detail_order(app)
            assert (AGENT_KEYS_SCROLL_KEY in order) is inline
            assert _has(app, "agent-keys-overlay") is overlay
            assert _has(app, "agent-keys-toggle") is collapsed
            if overlay:
                _overlay_fits(app, detail)
        finally:
            await session.dispose()

    run(scenario())


def test_measured_height_not_fallback_drives_layout() -> None:
    async def scenario() -> None:
        app, session = await _mounted(1032, dock_open=True)
        try:
            assert AGENT_KEYS_SCROLL_KEY in _detail_order(app)  # fallback 936: inline
            await _fire_detail_height(app, 560)
            assert AGENT_KEYS_SCROLL_KEY not in _detail_order(app)
            assert _has(app, "agent-keys-overlay")
        finally:
            await session.dispose()

    run(scenario())


def test_keyboard_open_hides_secondary_chrome_and_dock_is_overlay() -> None:
    async def scenario() -> None:
        app, session = await _mounted(560, dock_open=True)
        try:
            await _fire_detail_height(app, 464)
            order = _detail_order(app)
            assert AGENT_KEYS_SCROLL_KEY not in order
            assert "picture-upload-status" not in order
            _overlay_fits(app, 464)
            assert app.find("terminal-flex-host").expand is True
            assert app.find("agent-input-dock").expand in (None, False)
        finally:
            await session.dispose()

    run(scenario())


def test_default_font_captures_row_metric_from_real_size_events() -> None:
    async def scenario() -> None:
        app, session = await _mounted(1032, dock_open=False)  # font_size=None
        try:
            # Host box 520px (44px overhead) with 13 reported rows => ~36px rows (> 16px estimate):
            # a 700px panel then has far fewer than 12 rows and collapses chrome.
            await _fire(app, "terminal-flex-host", 520.0)
            for listener in list(app.find("terminal-surface")._size_listeners):
                listener(96, 13)  # the engine's own cols x rows report
            await app.settle()
            await _fire_detail_height(app, 700)
            assert not _has(app, "agent-keys-toggle")  # one sample is not trusted yet
            await _fire(app, "terminal-flex-host", 522.0)  # second agreeing sample locks it
            assert _has(app, "agent-keys-toggle")
        finally:
            await session.dispose()

    run(scenario())


def test_short_height_collapses_key_row_and_status_into_keys_toggle() -> None:
    async def scenario() -> None:
        app, session = await _mounted(550, dock_open=False)
        try:
            with pytest.raises(KeyError):
                app.find("terminal-shortcuts")
            with pytest.raises(KeyError):
                app.find(TOOLBAR_KEY)
            assert app.find("agent-keys-toggle") is not None
            # The toggle reopens the dock; the terminal surface survives.
            await app.set_props_and_fire("agent-keys-toggle", {}, "click")
            assert app.find(AGENT_KEYS_SCROLL_KEY) is not None
            assert _has(app, "agent-keys-overlay")
            assert app.find("terminal-surface") is not None
        finally:
            await session.dispose()

    run(scenario())


def test_tall_height_keeps_key_row_status_and_no_toggle() -> None:
    async def scenario() -> None:
        app, session = await _mounted(1032, dock_open=False)
        try:
            assert app.find("terminal-shortcuts") is not None
            assert app.find(TOOLBAR_KEY) is not None
            with pytest.raises(KeyError):
                app.find("agent-keys-toggle")
        finally:
            await session.dispose()

    run(scenario())


def test_touch_threading_shows_scroll_overlay_on_expanded() -> None:
    async def scenario() -> None:
        app, session = await _mounted(1032, touch=True)
        try:
            assert app.find("terminal-scroll-overlay").visible is True
        finally:
            await session.dispose()
        app, session = await _mounted(1032, touch=False)
        try:
            assert app.find("terminal-scroll-overlay").visible is False
        finally:
            await session.dispose()

    run(scenario())


def test_touch_platform_detection() -> None:
    assert is_touch_platform(ft.PagePlatform.IOS) is True
    assert is_touch_platform(ft.PagePlatform.ANDROID) is True
    assert is_touch_platform(ft.PagePlatform.MACOS) is False
    assert is_touch_platform(ft.PagePlatform.WINDOWS) is False
    assert is_touch_platform(None) is False


def _page(height: Any, bottom: Any) -> Any:
    insets = SimpleNamespace(bottom=bottom)
    return SimpleNamespace(height=height, media=SimpleNamespace(view_insets=insets))


def test_visible_height_subtracts_keyboard_insets() -> None:
    assert visible_page_height(_page(884, 0)) == 884
    assert visible_page_height(_page(884, 520)) == 364
    assert visible_page_height(SimpleNamespace(height=884, media=None)) == 884
    assert visible_page_height(SimpleNamespace(height=None, media=None)) == 800
    assert visible_page_height(_page(100, 500)) == 0


class _Page:
    def __init__(self, width: float, height: float, inset: float = 0.0) -> None:
        self.width, self.height = width, height
        self.media = SimpleNamespace(view_insets=SimpleNamespace(bottom=inset))
        self.on_resize: Any = None
        self.on_media_change: Any = None

    def resize(self, width: float | None = None, height: float | None = None) -> None:
        self.width = width if width is not None else self.width
        self.height = height if height is not None else self.height
        self.on_resize(None)

    def inset(self, bottom: float) -> None:
        self.media.view_insets.bottom = bottom
        self.on_media_change(None)


def _owned(page: _Page) -> Any:
    from relaydeck.client.features.inventory.routing import RouteBridge
    from relaydeck.client.resources import VisibleSize

    clock = SimpleNamespace(now=0.0)
    bridge = RouteBridge(width=page.width, height=page.height)
    owner = VisibleSize(page, bridge, clock=lambda: clock.now)
    owner.install()
    return bridge, owner, clock


def test_inset_before_resize_keeps_keyboard_height() -> None:
    page = _Page(1376, 1032)
    bridge, _owner, _clock = _owned(page)
    page.inset(520)
    assert bridge.state.height == 512
    page.resize(height=512)  # browser then shrinks the page too
    assert bridge.state.height == 512
    page.resize(height=512)
    assert bridge.state.height == 512


def test_resize_before_inset_is_not_double_subtracted() -> None:
    page = _Page(1376, 1032)
    bridge, _owner, _clock = _owned(page)
    page.resize(height=512)  # zero inset yet: indistinguishable from a window resize
    assert bridge.state.height == 512
    page.inset(520)  # the keyboard inset arrives afterwards
    assert bridge.state.height == 512  # NOT 0


def test_keyboard_close_restores_full_height_in_either_order() -> None:
    page = _Page(1376, 1032)
    bridge, _owner, _clock = _owned(page)
    page.resize(height=512)
    page.inset(520)
    page.inset(0)  # media first
    page.resize(height=1032)
    assert bridge.state.height == 1032
    page.resize(height=512)
    page.inset(520)
    page.resize(height=1032)  # resize first
    page.inset(0)
    assert bridge.state.height == 1032


def test_browser_that_does_not_shrink_subtracts_once() -> None:
    page = _Page(1376, 1032)
    bridge, _owner, _clock = _owned(page)
    page.inset(520)
    page.resize(height=1032)
    assert bridge.state.height == 512
    page.inset(0)
    assert bridge.state.height == 1032


def test_rotation_while_keyboard_open() -> None:
    page = _Page(1376, 1032)
    bridge, _owner, _clock = _owned(page)
    page.inset(520)
    assert bridge.state.height == 512
    # Rotate to portrait; the browser does not shrink the page: subtract once.
    page.resize(width=1032, height=1376)
    assert bridge.state.height == 856
    page.inset(0)
    assert bridge.state.height == 1376
    # Rotate back with a browser that also shrinks the page.
    page.inset(520)
    page.resize(width=1376, height=512)
    assert bridge.state.height == 512


def test_real_window_shrink_is_accepted_after_grace() -> None:
    page = _Page(1376, 1032)
    bridge, owner, clock = _owned(page)
    page.resize(height=600)
    clock.now = owner.GRACE_SECONDS + 1
    page.resize(height=600)
    assert bridge.state.height == 600
    page.inset(300)  # later keyboard: page not shrunk by it => subtract once
    assert bridge.state.height == 300


def test_baseline_survives_grace_while_keyboard_inset_is_active() -> None:
    page = _Page(1376, 1032)
    bridge, owner, clock = _owned(page)
    page.resize(height=512)  # shrunk resize, inset not yet reported
    page.inset(520)
    assert bridge.state.height == 512
    clock.now = 2.0  # past the grace period, keyboard still open
    page.resize(height=512)
    assert bridge.state.height == 512  # NOT 0
    page.inset(520)  # repeated media event
    clock.now = 10.0
    page.resize(height=512)
    assert bridge.state.height == 512
    page.inset(0)
    page.resize(height=1032)
    assert bridge.state.height == 1032


def test_first_shrunk_rotation_uses_previous_orientation_baseline() -> None:
    page = _Page(1376, 1032)
    bridge, owner, _clock = _owned(page)
    page.inset(520)
    page.resize(height=512)  # landscape, browser shrank the page
    assert bridge.state.height == 512
    page.resize(width=1032, height=856)  # rotate with the keyboard still open
    assert (bridge.state.width, bridge.state.height) == (1032, 856)  # NOT 336
    page.inset(520)  # delayed repeated media event
    assert bridge.state.height == 856


def test_resume_while_keyboard_open_keeps_visible_height() -> None:
    page = _Page(1376, 1032)
    bridge, owner, clock = _owned(page)
    page.resize(height=512)
    page.inset(520)
    clock.now = 5.0
    owner.sync_media()  # what resume_terminals() calls
    assert bridge.state.height == 512


def test_handlers_are_owned_and_released() -> None:
    page = _Page(800, 600)
    _bridge, owner, _clock = _owned(page)
    assert page.on_resize == owner.sync and page.on_media_change == owner.sync_media
    mine = page.on_resize
    owner.uninstall()
    assert page.on_resize is None and page.on_media_change is None
    page.on_resize = lambda e: None  # replacement handler survives a second uninstall
    owner.uninstall()
    assert page.on_resize is not None and mine is not page.on_resize


def test_resumed_session_rederives_size_without_resize_event() -> None:
    """A resumed Flet session keeps the bridge; a viewport change made while
    disconnected (reload at 390, Split View, rotation) sends no resize event."""
    from relaydeck.client.resources import AppResources

    page = _Page(1376, 1032)
    bridge, owner, _clock = _owned(page)
    page.width, page.height = 390, 844  # changed while disconnected: no event
    assert bridge.state.width == 1376
    from relaydeck.client.retained_sessions import RetainedSessions

    async def scenario() -> None:
        resources = AppResources(
            props={},
            client=None,
            owns_client=False,
            retained=RetainedSessions(),
            bridge=bridge,
            controller=None,
            transport=None,
            commands=None,
            visible_size=owner,
        )  # type: ignore[arg-type]
        resources.resume_terminals(transport=object(), current=lambda: True, closed=asyncio.Event())
        assert resources._ui_watchdog is not None
        resources._ui_watchdog.cancel()
        await asyncio.gather(resources._ui_watchdog, return_exceptions=True)

    run(scenario())
    assert (bridge.state.width, bridge.state.height) == (390, 844)


def test_error_guard_sees_call_phase_dispatch_errors(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Negative test: a malformed size event must be caught by the guard."""

    async def scenario() -> None:
        app, session = await _mounted(1032, dock_open=True)
        try:
            await app.set_props_and_fire(
                "agent-detail-panel", {}, "size_change", {"width": 900.0, "height": 560.0}
            )
        finally:
            await session.dispose()

    run(scenario())
    assert _error_records(caplog), "guard must see the call-phase dispatch error"
    caplog.clear()  # the expected error must not fail this test at teardown


def test_no_room_keeps_dock_out_of_main_column_and_keeps_toggle() -> None:
    async def scenario() -> None:
        app, session = await _mounted(300, dock_open=True)
        try:
            await _fire_detail_height(app, 180)
            order = _detail_order(app)
            assert AGENT_KEYS_SCROLL_KEY not in order  # no unbounded inline dock
            assert not _has(app, "agent-keys-overlay")  # nothing fits above the composer
            assert "terminal-flex-host" in order and "agent-input-dock" in order
            assert _has(app, "agent-keys-toggle")  # dismiss/reopen path stays
            assert app.find("terminal-flex-host").expand is True
        finally:
            await session.dispose()

    run(scenario())
