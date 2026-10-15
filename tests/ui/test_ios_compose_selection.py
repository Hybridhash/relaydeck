"""pinned Flet round trips; no WebKit gesture emulation."""

from __future__ import annotations

import pytest
from flet.messaging.protocol import MessageAction

from relaydeck.client.components.appearance import AppearanceBridge
from relaydeck.client.features.inventory.routing import RouteBridge
from tests.support.host_harness import run
from tests.support.ui.phone_terminal import _ancestry, _shape
from tests.support.ui.retained_root import _render


@pytest.mark.parametrize("width", [360, 390])
@pytest.mark.parametrize("changed_chrome", [False, True])
def test_native_compose_edit_survives_parent_render_before_change_dispatch(
    width: int, changed_chrome: bool
) -> None:
    async def scenario() -> None:
        appearance = AppearanceBridge(True)
        app, controller, sessions, pool, _now = await _render(
            bridge=RouteBridge(width=width, height=844), appearance=appearance
        )
        try:
            await app.set_props_and_fire("agent-home-mac:term-1", {}, "click")
            session = pool.foreground.session
            session.on_draft_change("old draft")
            await app.set_props_and_fire("agent-compose-toggle", {}, "click")
            field = app.find("composer-draft")
            ancestry = _shape(_ancestry(app, field))
            term = session.current_term
            text = "new word\nwith native selection"
            # Dart sends UPDATE_CONTROL_PROPS before CONTROL_EVENT. A metadata
            # or retained-session render can land between those two messages.
            app.session.apply_patch(field._i, {"value": text})
            start = len(app.conn.sent)
            session._notify()
            controller.push(controller.state)
            if changed_chrome:
                appearance.set_dark(False)
                session.set_agent_status("blocked")
            await app.settle()
            assert app.find("composer-draft").value == text
            assert app.find("composer-draft")._i == field._i
            assert _shape(_ancestry(app, app.find("composer-draft"))) == ancestry
            patches = [m for m in app.conn.sent[start:] if m.action == MessageAction.PATCH_CONTROL]
            if not changed_chrome:
                # A working agent's dot may blink in the dock (opacity only); nothing else.
                assert all(
                    op[2] == "opacity" for message in patches for op in message.body.patch[1:]
                ), patches
            # No outgoing patch may rewrite the editor text or caret, even
            # when the editor's palette/Send label genuinely needs updating.
            assert not [
                op
                for message in patches
                for op in message.body.patch[1:]
                if op[2] == "selection"
                or (op[2] == "value" and len(op) == 4 and op[3] in ("old draft", text))
            ]
            await app.session.dispatch_event(field._i, "change", text)
            await app.settle()
            assert session.store.draft == text and app.find("composer-draft").value == text
            assert session.current_term is term
            assert sessions.prompted == [] and sessions.submitted == []
            assert not [data for _, _, socket in sessions.streams.opened for data in socket.sent]
            assert app.find("composer-draft").selection is None
            # Authoritative clear/restore must still replace the text, not
            # leave the native edit behind because the editor is memoized.
            session.on_draft_change("")
            await app.settle()
            assert app.find("composer-draft").value == ""
            assert app.find("composer-draft")._i == field._i
            session.on_draft_change(text)
            await app.settle()
            assert app.find("composer-draft").value == text
            assert _shape(_ancestry(app, app.find("composer-draft"))) == ancestry
        finally:
            await app.set_props_and_fire("test-unmount", {}, "click")

    run(scenario())


@pytest.mark.parametrize("width", [360, 390])
def test_compose_word_selection_and_equal_text_have_no_round_trip_render(width: int) -> None:
    async def scenario() -> None:
        app, _controller, _sessions, pool, _now = await _render(
            bridge=RouteBridge(width=width, height=844)
        )
        try:
            await app.set_props_and_fire("agent-home-mac:term-1", {}, "click")
            session = pool.foreground.session
            text = "select this word\n" * 16
            session.on_draft_change(text)
            await app.set_props_and_fire("agent-compose-toggle", {}, "click")
            field = app.find("composer-draft")
            ancestry = _shape(_ancestry(app, field))
            notifications: list[object] = []
            unsubscribe = session.subscribe(notifications.append)
            start = len(app.conn.sent)
            try:
                assert field.on_selection_change is None and field.selection is None
                for event, data in (
                    ("focus", ""),
                    ("click", ""),
                    ("click", ""),
                    (
                        "selection_change",
                        {
                            "selected_text": "word",
                            "selection": {
                                "base_offset": 12,
                                "extent_offset": 16,
                            },
                        },
                    ),
                    ("change", text),
                    ("change", text),
                ):
                    await app.session.dispatch_event(field._i, event, data)
                    await app.settle()
                assert notifications == []
                # Only the dock's working-dot blink (opacity) may patch meanwhile.
                assert not [
                    op
                    for m in app.conn.sent[start:]
                    if m.action == MessageAction.PATCH_CONTROL
                    for op in m.body.patch[1:]
                    if op[2] != "opacity"
                ]
                assert app.find("composer-draft") is field
                assert _shape(_ancestry(app, field)) == ancestry
                assert field.selection is None and field.value == text
                assert session.store.draft == text
            finally:
                unsubscribe()
        finally:
            await app.set_props_and_fire("test-unmount", {}, "click")

    run(scenario())
