"""Pinned host dispatch proves focus gates/tap ancestry, not Safari focus itself."""

from __future__ import annotations

import asyncio

import flet as ft
import pytest
from flet.data_channel import _ProtocolMuxedDataChannel
from flet.messaging.protocol import MessageAction
from relaydeck_terminal import protocol

from relaydeck.client.features.inventory.routing import RouteBridge
from relaydeck.client.features.terminal.surfaces import direct_typing
from tests.support.host_harness import run
from tests.support.ui.phone_terminal import _ancestry, _shape
from tests.support.ui.retained_root import _active, _render


@pytest.mark.parametrize("exit_mode", ["direct", "reading-list"])
def test_compose_focus_is_released_and_hidden_field_cannot_request_focus(exit_mode: str) -> None:
    async def scenario() -> None:
        app, _controller, sessions, pool, _now = await _render(
            bridge=RouteBridge(width=390, height=844)
        )
        try:
            await app.set_props_and_fire("agent-home-mac:term-1", {}, "click")
            await app.set_props_and_fire("agent-takeover-confirm", {}, "click")
            session = pool.foreground.session
            session.on_draft_change("kept multiline\nphone draft")
            await app.set_props_and_fire("agent-compose-toggle", {}, "click")
            field = _active(app, "composer-draft")
            assert field.can_request_focus and not field.disabled and not field.autofocus
            term, writer, binding = (
                session.current_term,
                session.controller,
                session._viewer_binding,
            )
            ancestry = _shape(_ancestry(app, term))
            await app.set_props_and_fire("composer-draft", {}, "focus")
            start = len(app.conn.sent)
            await app.set_props_and_fire(
                "agent-workflow-toggle" if exit_mode == "direct" else "terminal-back", {}, "click"
            )
            hidden = app.find("composer-draft")
            assert hidden._i == field._i
            # can_request_focus is a real Flet property; disabled additionally
            # prevents edits. There is no invented TextField.blur/focused API.
            assert hidden.can_request_focus is False and hidden.disabled is True
            patches = [
                m.body.patch
                for m in app.conn.sent[start:]
                if m.action == MessageAction.PATCH_CONTROL
            ]
            # Real carrier patches set both supported properties on the same
            # node; this is not an invented Python-only "focused" attribute.
            assert any(
                any(
                    len(focus) == 4
                    and focus[2:] == ["can_request_focus", False]
                    and any(
                        len(op) == 4 and op[1] == focus[1] and op[2:] == ["disabled", True]
                        for op in patch
                    )
                    for focus in patch
                )
                for patch in patches
            ), patches
            with pytest.raises(KeyError):
                _active(app, "composer-draft")
            await app.set_props_and_fire(
                "composer-draft", {"value": "late edit"}, "change", "late edit"
            )
            assert session.store.draft == "kept multiline\nphone draft"
            if exit_mode == "reading-list":
                assert not session.foreground
                await app.set_props_and_fire("agent-home-mac:term-1", {}, "click")
                assert session.lease.mode == "view" and len(sessions.ops.attaches) == 1
                await app.set_props_and_fire("agent-takeover", {}, "click")
                await app.set_props_and_fire("agent-takeover-confirm", {}, "click")
            else:
                await app.set_props_and_fire("agent-compose-toggle", {}, "click")
            restored = _active(app, "composer-draft")
            assert restored._i == field._i and restored.can_request_focus and not restored.disabled
            assert restored.value == "kept multiline\nphone draft"
            assert session.current_term is term
            if exit_mode == "reading-list":
                assert session.controller is not writer and session.lease.mode == "control"
            else:
                assert session.controller is writer
            assert session._viewer_binding == binding
            assert _shape(_ancestry(app, term)) == ancestry
            assert len(sessions.ops.attaches) == (2 if exit_mode == "reading-list" else 1)
        finally:
            await app.set_props_and_fire("test-unmount", {}, "click")

    run(scenario())


def _assert_unobstructed_viewport(app: object, term: object) -> None:
    def narrow_scroll(control: object) -> bool:
        body = getattr(control, "_b", None)
        if body is not None:
            return narrow_scroll(body)
        return (
            getattr(control, "key", None) == "terminal-scroll-overlay"
            and getattr(control, "width", None) == 32
            and getattr(control, "right", None) == 8
        )

    def bottom_dock(control: object, branch: object) -> bool:
        body = getattr(control, "_b", None)
        if body is not None:
            return bottom_dock(body, branch)
        if not (
            getattr(control, "key", None) == "agent-input-dock"
            and getattr(control, "bottom", None) == 0
            and getattr(control, "top", None) is None
        ):
            return False
        spacer = app.find("agent-input-spacer")
        return spacer.height is not None and spacer.height > 0 and branch in _ancestry(app, spacer)

    def hidden(control: object) -> bool:
        if getattr(control, "visible", True) is False:
            return True
        # Declarative Components are virtual parents; evaluate their actual
        # rendered body rather than the Component's default visible property.
        body = getattr(control, "_b", None)
        if isinstance(body, list | tuple):
            return bool(body) and all(hidden(c) for c in body)
        return body is not None and hidden(body)

    path = _ancestry(app, term)
    for index, node in enumerate(path[:-1]):
        if isinstance(node, ft.GestureDetector):
            assert node.key == "terminal-direct-tap" and node.on_tap_down is None
            assert node.on_pan_update is None and node.on_long_press_start is None
        assert getattr(node, "on_click", None) is None
        assert getattr(node, "disabled", False) is False
        assert getattr(node, "ignore_interactions", False) is not True
        assert getattr(node, "visible", True) is not False
        if isinstance(node, ft.Stack):
            branch = path[index + 1]
            above = node.controls[node.controls.index(branch) + 1 :]
            # AppShell's top Row has no painted/hit-tested children in a
            # compact retained detail. Flutter RenderFlex has no hitTestSelf.
            assert all(
                hidden(c)
                or narrow_scroll(c)
                or bottom_dock(c, branch)
                or getattr(c, "ignore_interactions", False) is True
                or (
                    isinstance(c, ft.Row)
                    and c.on_scroll is None
                    and all(child.visible is False for child in c.controls)
                )
                for c in above
            ), [(c.key, c.visible) for c in above]


def test_touch_phone_direct_dock_has_reserved_space_below_terminal() -> None:
    async def scenario() -> None:
        app, _controller, _sessions, pool, _now = await _render(
            bridge=RouteBridge(width=390, height=844), touch=True
        )
        try:
            await app.set_props_and_fire("agent-home-mac:term-1", {}, "click")
            await app.set_props_and_fire("agent-takeover-confirm", {}, "click")
            await app.set_props_and_fire("agent-workflow-toggle", {}, "click")
            spacer = app.find("agent-input-spacer")
            dock = app.find("agent-input-dock")
            assert spacer.height is not None and spacer.height > 0
            assert dock.bottom == 0 and dock.top is None
            _assert_unobstructed_viewport(app, pool.foreground.session.current_term)
        finally:
            await app.set_props_and_fire("test-unmount", {}, "click")

    run(scenario())


@pytest.mark.parametrize("origin", [protocol.ORIGIN_TEXT, protocol.ORIGIN_KEY])
def test_direct_field_is_outside_clear_retained_viewport_across_native_input_and_resize(
    origin: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = [1000.0]
    monkeypatch.setattr(direct_typing, "_typing_now", lambda: now[0])

    async def scenario() -> None:
        bridge = RouteBridge(width=390, height=844)
        app, _controller, _sessions, pool, _now = await _render(bridge=bridge, touch=True)
        try:
            await app.set_props_and_fire("agent-home-mac:term-1", {}, "click")
            await app.set_props_and_fire("agent-takeover-confirm", {}, "click")
            session = pool.foreground.session
            channel = _ProtocolMuxedDataChannel(85, app.conn)
            app.conn.data_channel_for = lambda number: channel
            await app.set_props_and_fire(
                "terminal-surface",
                {},
                "data_channel_open",
                {"channel_name": "terminal", "channel_id": 85},
            )
            term = session.current_term
            ancestry = _shape(_ancestry(app, term))
            channel._deliver(protocol.encode_input(b"draft only", protocol.ORIGIN_TEXT))
            await app.settle()
            now[0] += 1.0
            await app.set_props_and_fire("agent-workflow-toggle", {}, "click")
            _assert_unobstructed_viewport(app, term)
            assert [c.key for c in app.find("terminal-viewport-stack").controls] == [
                "terminal-engine-slot",
                "terminal-cached-screen",
                "terminal-connecting-layer",
                "terminal-scroll-controls",
            ]
            assert app.find("terminal-cached-screen").visible is False
            assert app.find("terminal-cached-screen").ignore_interactions is True
            # Excluded from the tree, not merely invisible.
            with pytest.raises(KeyError):
                app.find("terminal-toolbar")
            field = _active(app, "agent-workflow-direct-typing")
            assert field.hint_text == "Type to terminal" and field.value == "  "
            assert app.find("terminal-viewport-stack") not in _ancestry(app, field)
            channel._deliver(protocol.encode_input(b"", protocol.ORIGIN_TEXT))
            channel._deliver(protocol.encode_input(b"mouse", protocol.ORIGIN_MOUSE))
            await app.settle()
            assert _active(app, "agent-workflow-direct-typing")._i == field._i
            await app.set_props_and_fire("key-enter", {}, "click")
            assert _active(app, "agent-workflow-direct-typing")._i == field._i
            bridge.set_size(390, 544)
            await app.settle()
            _assert_unobstructed_viewport(app, term)
            channel._deliver(protocol.encode_input(b"a", origin))
            await app.settle()
            assert _active(app, "agent-workflow-direct-typing")._i == field._i
            await app.set_props_and_fire("terminal-back", {}, "click")
            await app.set_props_and_fire("agent-home-mac:term-1", {}, "click")
            # Parking closes keyboard intent; returning keeps the field and
            # terminal identities, but requires a fresh user gesture to type.
            assert app.find("agent-workflow-direct-typing").visible is False
            assert session.lease.mode == "view" and session.controller.key_generation() is None
            # Returning from the list is watching. Typing requires fresh,
            # explicit takeover, preserving the native tree and field instance.
            await app.set_props_and_fire("agent-takeover", {}, "click")
            await app.set_props_and_fire("agent-takeover-confirm", {}, "click")
            # The keyboard button, not a tap, opens phone Direct input.
            now[0] += 1.0
            await app.set_props_and_fire("terminal-keyboard-button", {}, "click")
            assert _active(app, "agent-workflow-direct-typing")._i == field._i
            await app.set_props_and_fire("agent-compose-toggle", {}, "click")
            await app.settle()
            await asyncio.sleep(0.06)
            await app.settle()
            now[0] += 1.0
            await app.set_props_and_fire("agent-workflow-toggle", {}, "click")
            assert _active(app, "agent-workflow-direct-typing")._i == field._i
            assert session.current_term is term and _shape(_ancestry(app, term)) == ancestry
        finally:
            await app.set_props_and_fire("test-unmount", {}, "click")

    run(scenario())
