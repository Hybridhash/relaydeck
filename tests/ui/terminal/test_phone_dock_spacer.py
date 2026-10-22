"""The phone dock spacer follows the dock's shape without a measurement round trip."""

from __future__ import annotations

import flet as ft

from relaydeck.client.features.composer.domain import SUBMISSION_PROGRESS, SUBMISSION_SENDING
from relaydeck.client.features.terminal.agent_session import AGENT_LIVE
from relaydeck.client.features.terminal.surfaces import mount_agent_screen, phone_composer
from tests.support.host_harness import render_component, run
from tests.support.terminal.direct_typing import open_compose
from tests.support.terminal.sessions import _agent_session
from tests.support.ui.phone_terminal import _ancestry, _shape


async def _measure(app: object, height: float) -> None:
    await app.set_props_and_fire(  # type: ignore[attr-defined]
        "agent-input-dock-body", {}, "size_change", {"w": 390.0, "h": height}
    )


def test_compose_tap_renders_the_remembered_spacer_in_the_same_frame() -> None:
    async def scenario() -> None:
        session, _ops, _factory, _submitted = _agent_session()
        session.open()
        assert await session.confirm_takeover() == AGENT_LIVE
        app = await render_component(
            mount_agent_screen, session, phone=True, touch=True, viewport_height=700
        )
        try:
            term = session.current_term
            engine_shape = _shape(_ancestry(app, term))
            await _measure(app, 84.0)
            assert app.find("agent-input-spacer").height == 84.0
            await open_compose(app)
            await _measure(app, 128.0)
            assert app.find("agent-input-spacer").height == 128.0
            await app.set_props_and_fire("agent-workflow-toggle", {}, "click")
            await _measure(app, 90.0)
            assert app.find("agent-input-spacer").height == 90.0
            # Each known shape renders its height at once, with no new measurement.
            await open_compose(app)
            assert app.find("agent-input-spacer").height == 128.0
            await app.set_props_and_fire("agent-workflow-toggle", {}, "click")
            assert app.find("agent-input-spacer").height == 90.0
            assert _shape(_ancestry(app, term)) == engine_shape
        finally:
            await session.dispose()

    run(scenario())


def test_a_hidden_screen_measuring_zero_never_becomes_the_remembered_height() -> None:
    async def scenario() -> None:
        session, _ops, _factory, _submitted = _agent_session()
        session.open()
        assert await session.confirm_takeover() == AGENT_LIVE
        app = await render_component(
            mount_agent_screen, session, phone=True, touch=True, viewport_height=700
        )
        try:
            await _measure(app, 84.0)
            assert app.find("agent-input-spacer").height == 84.0
            # A parked or hidden screen reports 0 for a dock that still has a text box.
            await _measure(app, 0.0)
            session._notify()
            await app.settle()
            assert app.find("agent-input-spacer").height == 84.0
        finally:
            await session.dispose()

    run(scenario())


def test_a_progress_notice_keeps_the_composer_height() -> None:
    async def scenario() -> None:
        heights = {}
        for submission in (SUBMISSION_SENDING, SUBMISSION_PROGRESS):

            @ft.component
            def Probe(submission: str = submission) -> ft.Control:
                return phone_composer._phone_composer(
                    draft="hello",
                    markers=(),
                    can_send=True,
                    blockers=(),
                    submission=submission,
                    error=None,
                    review=None,
                    input_mode="text",
                    on_draft_change=lambda _t: None,
                    on_send=lambda: None,
                    on_confirm_blocked=lambda: None,
                    on_cancel_blocked=lambda: None,
                    on_clear_error=lambda: None,
                    toggle=None,
                    dark=False,
                    blocked=False,
                    visible=True,
                    focusable=True,
                )

            app = await render_component(Probe)
            row = app.find(phone_composer.STATUS_ROW_KEY)
            heights[submission] = (row.height, [c.content.value for c in row.content.controls])
        # A normal send never changes the row: "working" shows in the text box instead.
        assert heights[SUBMISSION_SENDING] == (phone_composer.STATUS_ROW_EMPTY, [])
        assert heights[SUBMISSION_PROGRESS][0] == phone_composer.STATUS_ROW_EMPTY

    run(scenario())
