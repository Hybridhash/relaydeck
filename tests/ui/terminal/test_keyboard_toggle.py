"""Actual carrier dispatch and focus-gate ordering; Safari dismissal remains device proof."""

import asyncio
from typing import Any

import pytest
from flet.messaging.protocol import MessageAction

from relaydeck.client.features.terminal.surfaces import direct_typing
from tests.support.host_harness import run
from tests.support.terminal.direct_typing import _direct, _typing_key, open_compose


@pytest.mark.parametrize("surface", ["agent-direct", "agent-compose", "shell"])
def test_keyboard_toggle_gates_focus_before_hide_and_preserves_retained_session(
    surface: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = [1.0]
    monkeypatch.setattr(direct_typing, "_typing_now", lambda: now[0])

    async def scenario() -> None:
        shell = surface == "shell"
        app, session, _socket, focuses = await _direct(shell)
        try:
            key = "shell-keyboard-toggle" if shell else "agent-workflow-toggle"
            semantics = "shell-keyboard-semantics" if shell else "agent-keyboard-semantics"
            field = app.find(_typing_key(app))
            field_id = field._i
            term, controller = session.current_term, session.controller
            if surface == "agent-compose":
                await open_compose(app)
                field = app.find(_typing_key(app))
                assert not field.visible and field.disabled
                now[0] += 0.5
                await app.set_props_and_fire("agent-workflow-toggle", {}, "click")
                field = app.find(_typing_key(app))
            assert field.visible and field.can_request_focus
            assert app.find(key).selected and app.find(semantics).selected
            start = len(app.conn.sent)
            now[0] += 0.5
            button = app.find(key)
            await app.session.dispatch_event(button._i, "click", "")
            await asyncio.sleep(0.02)
            field = app.find(_typing_key(app))
            assert field.visible and field.disabled and not field.can_request_focus
            assert not app.find(key).selected and not app.find(semantics).selected
            await app.settle()
            field = app.find(_typing_key(app))
            if surface == "shell":
                assert not field.visible and field.disabled and not field.can_request_focus
            else:
                # Direct input keeps its field and keys; tapping the field reopens the keyboard.
                assert field.visible and field.can_request_focus
            assert app.find("mobile-keybar").visible is True
            patches = [
                message.body.patch
                for message in app.conn.sent[start:]
                if message.action == MessageAction.PATCH_CONTROL
            ]

            def index(prop: str, value: Any) -> int:
                indices = [
                    i
                    for i, patch in enumerate(patches)
                    if any(
                        # Flet patches use subtree object indices, not _i.
                        # The real field's gated/visible state is asserted
                        # above between these two carrier updates.
                        len(op) == 4 and op[2:] == [prop, value]
                        for op in patch
                    )
                ]
                assert indices, (field._i, prop, patches)
                return indices[0]

            if surface == "shell":
                assert index("can_request_focus", False) < index("visible", False)
            else:
                index("can_request_focus", False)  # the focus gate still closes first
            count = len(focuses)
            now[0] += 0.5
            await app.set_props_and_fire(key, {}, "click")
            field = app.find(_typing_key(app))
            assert field.visible and field.can_request_focus and not field.disabled
            assert len(focuses) == count + 1
            assert app.find(key).selected and app.find(semantics).selected
            # Duplicate delivery of the same physical tap does not reopen.
            now[0] += 0.5
            await app.session.dispatch_event(app.find(key)._i, "click", "")
            now[0] += 0.02
            await app.session.dispatch_event(app.find(key)._i, "click", "")
            await app.settle()
            field = app.find(_typing_key(app))
            assert field.visible is (surface != "shell") and not app.find(semantics).selected
            assert field._i == field_id
            assert session.current_term is term and session.controller is controller
            if not shell:
                assert session.store.draft == "retained compose draft"
        finally:
            await session.dispose()

    run(scenario())


@pytest.mark.parametrize("shell", [False, True])
def test_leaving_agent_or_shell_closes_keyboard_intent(shell: bool) -> None:
    async def scenario() -> None:
        app, session, _socket, _focuses = await _direct(shell)
        try:
            field_id = app.find(_typing_key(app))._i
            term = session.current_term
            session.set_foreground(False)
            await app.settle()
            field = app.find(_typing_key(app))
            assert field.disabled and not field.can_request_focus
            session.set_foreground(True)
            await app.settle()
            assert app.find(_typing_key(app)).visible is not shell
            assert app.find(_typing_key(app))._i == field_id
            assert session.current_term is term
        finally:
            await session.dispose()

    run(scenario())


def test_compose_and_leaving_screen_close_direct_keyboard_without_losing_draft(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [1.0]
    monkeypatch.setattr(direct_typing, "_typing_now", lambda: now[0])

    async def scenario() -> None:
        app, session, _socket, _focuses = await _direct(False)
        try:
            field = app.find(_typing_key(app))
            term = session.current_term
            await open_compose(app)
            field = app.find(_typing_key(app))
            assert not field.visible and field.disabled and not field.can_request_focus
            assert app.find("composer-draft").value == "retained compose draft"
            now[0] += 0.5
            await app.set_props_and_fire("agent-workflow-toggle", {}, "click")
            field = app.find(_typing_key(app))
            assert field.visible
            session.set_foreground(False)
            await app.settle()
            field = app.find(_typing_key(app))
            assert field.disabled and not field.can_request_focus
            session.set_foreground(True)
            await app.settle()
            field = app.find(_typing_key(app))
            assert field.visible
            assert session.store.draft == "retained compose draft"
            assert session.current_term is term
        finally:
            await session.dispose()

    run(scenario())


def test_tapping_the_idle_direct_field_reopens_direct_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [100.0]
    monkeypatch.setattr(direct_typing, "_typing_now", lambda: now[0])

    async def scenario() -> None:
        app, session, _socket, focuses = await _direct(False)
        try:
            now[0] += 0.5
            await app.set_props_and_fire("agent-workflow-toggle", {}, "click")
            await app.settle()
            field = app.find(_typing_key(app))
            assert field.visible and field.can_request_focus
            assert not app.find("agent-keyboard-semantics").selected
            assert app.find("mobile-keybar").visible is True
            # iOS raises its keyboard only for a tap on the field: the icon moves inside it.
            assert field.suffix_icon is not None
            assert app.find("agent-keyboard-semantics").visible is False
            count = len(focuses)
            await app.set_props_and_fire(_typing_key(app), {}, "focus")
            await app.settle()
            assert app.find("agent-keyboard-semantics").selected
            assert app.find("agent-keyboard-semantics").visible is not False
            assert app.find(_typing_key(app)).suffix_icon is None
            assert len(focuses) == count + 1
        finally:
            await session.dispose()

    run(scenario())
