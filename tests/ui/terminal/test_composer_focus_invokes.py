"""Retired Composer focus calls finish within Flet's own deadline."""

from __future__ import annotations

import asyncio
from typing import Any

import flet as ft
import pytest
from flet.messaging.protocol import MessageAction

from relaydeck.client.features.composer import components as composer
from relaydeck.client.features.terminal.surfaces import phone_composer
from relaydeck.client.flet_invokes import install_invoke_result_guard
from tests.support.host_harness import render_component, run


@pytest.mark.parametrize("phone", [False, True])
@pytest.mark.parametrize("reply", [False, True])
def test_composer_pending_focus_finishes_after_target_retirement(
    phone: bool, reply: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    tasks: list[asyncio.Task[Any]] = []
    module = phone_composer if phone else composer
    spawn = module.spawn

    def track(coro: Any, *, label: str) -> asyncio.Task[Any]:
        task = spawn(coro, label=label)
        if label == "Show composer keyboard":
            tasks.append(task)
        return task

    monkeypatch.setattr(module, "spawn", track)

    @ft.component
    def Fixture() -> Any:
        shown, set_shown = ft.use_state(True)
        field = (
            phone_composer._PhoneComposerField(
                draft="kept draft",
                dark=True,
                blocked=False,
                compact=True,
                focusable=True,
                send_enabled=True,
                on_draft_change=lambda _t: None,
                on_send=lambda: None,
                keyboard="show",
            )
            if phone
            else composer.ComposerBox(
                draft="kept draft",
                markers=(),
                can_send=True,
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
                keyboard="show",
            )
        )
        return ft.Column(
            controls=[
                ft.Button("drop", key="drop", on_click=lambda _e: set_shown(False)),
                field if shown else ft.Text("gone", key="gone"),
            ]
        )

    async def scenario() -> None:
        app = await render_component(Fixture)
        install_invoke_result_guard(app.session)
        try:
            target = app.find(composer.DRAFT_KEY)
            toggle = next(
                node
                for node in app.session.index.values()
                if node.key == "composer-show-keyboard-semantics"
            )
            await app.session.dispatch_event(toggle._i, "tap", "")
            async with asyncio.timeout(2):
                while not any(m.action == MessageAction.INVOKE_METHOD for m in app.conn.sent):
                    await asyncio.sleep(0)
            assert len(tasks) == 1
            call = next(m.body for m in app.conn.sent if m.action == MessageAction.INVOKE_METHOD)
            assert call.control_id == target._i and call.name == "focus"
            assert call.call_id in app.session._Session__method_calls
            await app.set_props_and_fire("drop", {}, "click")
            assert target._i not in app.session.index
            if reply:
                app.session.handle_invoke_method_results(target._i, call.call_id, None, None)
            assert not tasks[0].done()
            async with asyncio.timeout(2):
                await asyncio.shield(tasks[0])
            assert not app.session._Session__method_calls
            assert not app.session._Session__method_call_results
            # A reply after the timeout is still an orphan, and does not crash the page.
            app.session.handle_invoke_method_results(target._i, call.call_id, None, None)
            assert app.find("gone").value == "gone"
        finally:
            app.session.close()
            await asyncio.gather(*tasks, return_exceptions=True)

    run(scenario())
