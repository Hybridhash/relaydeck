"""Real Flet dispatch proves sticky focus intent; OS keyboard behavior needs a device."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import flet as ft
import pytest
from flet.messaging.protocol import MessageAction

from relaydeck.client.features.inventory.routing import RouteBridge
from relaydeck.client.features.terminal.diagnostics import diagnostics_logger
from relaydeck.client.hooks.typing_focus import TYPING_EXIT_REASONS, use_typing_focus
from tests.support.host_harness import render_component, run
from tests.support.terminal.direct_typing import open_compose
from tests.support.ui.phone_terminal import _ancestry, _shape
from tests.support.ui.retained_root import _active, _render


def _toggle(app: Any, key: str) -> Any:
    """The keyboard toggle control with `key`, found in the composer field's suffix."""
    suffix = app.find("composer-draft").suffix_icon
    if isinstance(suffix, ft.Row):
        for node in (suffix.controls[1], suffix.controls[1].content):
            if node.key == key:
                return node
    raise KeyError(key)


def _target(app: Any, key: str) -> Any:
    return _toggle(app, key) if key.startswith("composer-") else app.find(key)


def _assert_show(app: Any) -> None:
    """Keyboard down in compose mode: the same toggle offers Show keyboard (up arrow)."""
    with pytest.raises(KeyError):
        _toggle(app, "composer-hide-keyboard")
    show = _toggle(app, "composer-show-keyboard")
    assert (show.width, show.height) == (44, 44)
    # A touch passes through to the text field, which shows the keyboard inside the gesture.
    assert show.ignore_interactions
    arrow, keyboard = show.content.controls
    assert (arrow.icon, keyboard.icon) == (ft.Icons.KEYBOARD_ARROW_UP, ft.Icons.KEYBOARD)
    semantics = _toggle(app, "composer-show-keyboard-semantics")
    assert semantics.label == "Show keyboard" and semantics.button


@pytest.mark.parametrize("width", [390, 768])
# The pencil that also dismissed a focused composer is gone; Hide keyboard is the explicit action.
@pytest.mark.parametrize("action", ["composer-hide-keyboard"])
def test_explicit_dismiss_pulses_focus_gate_without_changing_draft_or_terminal(
    width: int, action: str
) -> None:
    async def scenario() -> None:
        app, _controller, sessions, pool, _now = await _render(
            bridge=RouteBridge(width=width, height=844), touch=True
        )
        try:
            await app.set_props_and_fire("agent-home-mac:term-1", {}, "click")
            await app.set_props_and_fire("agent-takeover-confirm", {}, "click")
            session = pool.foreground.session
            draft = "untouched\nmultiline draft"
            session.on_draft_change(draft)
            await open_compose(app)
            _assert_show(app)
            await app.set_props_and_fire("composer-draft", {}, "focus")
            field = _active(app, "composer-draft")
            term, controller, lease = session.current_term, session.controller, session.lease
            ancestry = _shape(_ancestry(app, term))
            attaches = list(sessions.ops.attaches)
            hide = _toggle(app, "composer-hide-keyboard")
            assert (hide.width, hide.height) == (44, 44)
            assert hide.icon == ft.Icons.KEYBOARD_HIDE
            semantics = _toggle(app, "composer-hide-keyboard-semantics")
            assert semantics.label == "Hide keyboard" and semantics.button
            assert field.on_tap_outside is not None and field.on_submit is None
            await app.set_props_and_fire("composer-draft", {}, "tap_outside", {"x": 40, "y": 80})
            assert _toggle(app, "composer-hide-keyboard") is hide
            assert _active(app, "composer-draft").can_request_focus

            start = len(app.conn.sent)
            target = _target(app, action)._i
            await app.session.dispatch_event(target, "click", "")
            await asyncio.sleep(0.02)
            gated = app.find("composer-draft")
            assert gated._i == field._i and not gated.can_request_focus and gated.disabled
            await app.session.dispatch_event(target, "click", "")  # a repeat is ignored
            await app.settle()
            restored = _active(app, "composer-draft")
            assert restored._i == field._i and restored.can_request_focus and not restored.disabled
            assert restored.value == session.store.draft == draft
            _assert_show(app)
            patches = [
                message.body.patch
                for message in app.conn.sent[start:]
                if message.action == MessageAction.PATCH_CONTROL
            ]
            gates = [
                op[3]
                for patch in patches
                for op in patch
                if len(op) == 4 and op[2] == "can_request_focus"
            ]
            assert gates == [False, True]
            assert not any(m.action == MessageAction.INVOKE_METHOD for m in app.conn.sent[start:])
            assert session.current_term is term and session.controller is controller
            assert session.lease is lease and sessions.ops.attaches == attaches
            assert _shape(_ancestry(app, term)) == ancestry
            await app.set_props_and_fire("composer-draft", {}, "focus")
            assert _toggle(app, "composer-hide-keyboard") is not None
            await app.set_props_and_fire("agent-workflow-toggle", {}, "click")
            for key in ("composer-hide-keyboard", "composer-show-keyboard"):
                with pytest.raises(KeyError):
                    _active(app, key)
            assert not app.find("composer-draft").can_request_focus
            assert session.store.draft == draft
        finally:
            await app.set_props_and_fire("test-unmount", {}, "click")

    run(scenario())


@pytest.mark.parametrize("width", [390, 768])
def test_terminal_tap_keeps_phone_composer_focused(width: int) -> None:
    """Only the Hide keyboard button and the compose pencil close the keyboard."""

    async def scenario() -> None:
        app, _controller, sessions, pool, _now = await _render(
            bridge=RouteBridge(width=width, height=844), touch=True
        )
        try:
            await app.set_props_and_fire("agent-home-mac:term-1", {}, "click")
            await app.set_props_and_fire("agent-takeover-confirm", {}, "click")
            session = pool.foreground.session
            session.on_draft_change("kept draft")
            await open_compose(app)
            await app.set_props_and_fire("composer-draft", {}, "focus")
            field = _active(app, "composer-draft")
            hide = _toggle(app, "composer-hide-keyboard")
            tap = app.find("terminal-direct-tap")
            assert tap.on_tap_down is None
            attaches = list(sessions.ops.attaches)
            start = len(app.conn.sent)
            for _ in range(3):
                outside = {"x": 40, "y": 80}
                await app.set_props_and_fire("composer-draft", {}, "tap_outside", outside)
                await app.session.dispatch_event(tap._i, "tap", "")
                await asyncio.sleep(0.02)
                kept = _active(app, "composer-draft")
                assert kept._i == field._i and kept.can_request_focus and not kept.disabled
            await app.settle()
            assert _toggle(app, "composer-hide-keyboard") is hide
            assert _active(app, "composer-draft").value == session.store.draft == "kept draft"
            gates = [
                op
                for message in app.conn.sent[start:]
                if message.action == MessageAction.PATCH_CONTROL
                for op in message.body.patch
                if len(op) == 4 and op[2] in ("can_request_focus", "disabled")
            ]
            assert gates == []
            assert not any(m.action == MessageAction.INVOKE_METHOD for m in app.conn.sent[start:])
            assert sessions.ops.attaches == attaches
            await app.session.dispatch_event(_toggle(app, "composer-hide-keyboard")._i, "click", "")
            await asyncio.sleep(0.02)
            assert not app.find("composer-draft").can_request_focus
        finally:
            await app.set_props_and_fire("test-unmount", {}, "click")

    run(scenario())


def test_send_is_not_a_typing_exit_and_existing_explicit_reasons_still_work() -> None:
    callbacks: list[Any] = []

    @ft.component
    def Harness(route: tuple[str, str]) -> Any:
        _cancel, _focus, _set, active, leave, _focused, keep = use_typing_focus(
            sticky=True, viewport=(844, 390), route=route
        )
        callbacks[:] = [leave, keep]
        return ft.Text(str(active), key="typing-active")

    async def scenario() -> None:
        app = await render_component(Harness, ("agents", "one"))
        assert "send" not in TYPING_EXIT_REASONS
        with pytest.raises(ValueError, match="unknown typing exit reason: send"):
            callbacks[0]("send")
        for reason in TYPING_EXIT_REASONS:
            callbacks[1]()
            await app.settle()
            assert app.find("typing-active").value == "True"
            callbacks[0](reason)
            await app.settle()
            assert app.find("typing-active").value == "False"

    run(scenario())


def test_send_and_outside_scroll_keep_phone_composer_editable(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)

    async def scenario() -> None:
        app, _controller, _sessions, pool, _now = await _render(
            bridge=RouteBridge(width=390, height=844), touch=True
        )
        try:
            await app.set_props_and_fire("agent-home-mac:term-1", {}, "click")
            await app.set_props_and_fire("agent-takeover-confirm", {}, "click")
            session = pool.foreground.session
            session.on_draft_change("send this once")
            await open_compose(app)
            await app.set_props_and_fire("composer-draft", {}, "focus")
            field = _active(app, "composer-draft")
            caplog.clear()
            await app.session.dispatch_event(field.suffix_icon.controls[0]._i, "click", "")
            await app.settle()
            after_send = _active(app, "composer-draft")
            assert after_send._i == field._i
            assert after_send.can_request_focus and not after_send.disabled
            assert "typing_exit" not in caplog.text
            await app.set_props_and_fire("composer-draft", {}, "tap_outside", {"x": 10, "y": 20})
            assert _active(app, "composer-draft").can_request_focus
            assert _toggle(app, "composer-hide-keyboard") is not None
            await app.set_props_and_fire("composer-draft", {}, "blur")
            _assert_show(app)
            assert "composer_focus focused=False since_send_ms=" in caplog.text
            focus_log = next(
                record
                for record in reversed(caplog.records)
                if record.msg == "composer_focus focused=%s since_send_ms=%s"
            )
            assert focus_log.args[0] is False and focus_log.args[1] >= 0
            assert "send this once" not in caplog.text
        finally:
            await app.set_props_and_fire("test-unmount", {}, "click")

    logger = diagnostics_logger()
    logger.addHandler(caplog.handler)
    try:
        run(scenario())
    finally:
        logger.removeHandler(caplog.handler)


@pytest.mark.parametrize("width", [390, 768])
def test_show_keyboard_asks_the_field_for_focus_for_screen_readers_and_keeps_the_draft(
    width: int,
) -> None:
    async def scenario() -> None:
        app, _controller, sessions, pool, _now = await _render(
            bridge=RouteBridge(width=width, height=844), touch=True
        )
        try:
            await app.set_props_and_fire("agent-home-mac:term-1", {}, "click")
            await app.set_props_and_fire("agent-takeover-confirm", {}, "click")
            session = pool.foreground.session
            session.on_draft_change("kept draft")
            await open_compose(app)
            _assert_show(app)
            field = _active(app, "composer-draft")
            attaches = list(sessions.ops.attaches)
            start = len(app.conn.sent)
            await app.session.dispatch_event(
                _toggle(app, "composer-show-keyboard-semantics")._i, "tap", ""
            )
            await app.settle()
            calls = [
                m.body for m in app.conn.sent[start:] if m.action == MessageAction.INVOKE_METHOD
            ]
            assert [call.name for call in calls] == ["focus"]
            assert calls[0].control_id == field._i
            assert _active(app, "composer-draft").value == session.store.draft == "kept draft"
            await app.set_props_and_fire("composer-draft", {}, "focus")
            assert _toggle(app, "composer-hide-keyboard") is not None
            with pytest.raises(KeyError):
                _toggle(app, "composer-show-keyboard")
            assert sessions.ops.attaches == attaches
        finally:
            await app.set_props_and_fire("test-unmount", {}, "click")

    run(scenario())
