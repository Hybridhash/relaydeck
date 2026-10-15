"""Opening and closing Keys in the split (field blurred, keyboard down)
keeps typing mode without marking the field focused, so the draft stays capped.
Real `use_typing_focus` wiring, synthetic session; no network.
"""

from __future__ import annotations

from typing import Any

import flet as ft

from relaydeck.client.features.terminal.surfaces import mount_agent_screen
from relaydeck.client.hooks.typing_focus import use_typing_focus
from tests.support.host_harness import render_component, run
from tests.support.terminal.sessions import _agent_session


@ft.component
def _CapHarness(session: Any) -> Any:
    _cancel, on_focus, _set, typing_focus, exit_typing, field_focused, keep = use_typing_focus(
        sticky=True, viewport=(1376.0, 800.0), route=("agents", "agent-1")
    )
    return ft.Container(
        expand=True,
        content=mount_agent_screen(  # type: ignore[call-arg]
            session,
            width_tier="expanded",
            phone=False,
            viewport_height=800,
            viewport_width=1376.0,
            touch=True,
            typing_split=typing_focus,
            on_composer_focus=on_focus,
            on_keep_typing=keep,
            on_typing_exit=exit_typing,
            keyboard_up=field_focused,
        ),
    )


def test_keys_open_and_closed_in_the_blurred_split_keep_the_draft_capped() -> None:
    async def scenario() -> None:
        session, _ops, _factory, _submitted = _agent_session()
        session.open()
        await session.confirm_takeover()
        app = await render_component(_CapHarness, session)
        try:
            await app.set_props_and_fire(
                "agent-detail-panel", {}, "size_change", {"w": 1376.0, "h": 800.0}
            )
            await app.set_props_and_fire("composer-draft", {}, "focus")
            focused = app.find("composer-draft").max_lines
            await app.set_props_and_fire("composer-draft", {}, "blur")
            capped = app.find("composer-draft").max_lines
            assert capped < focused  # the 40% cap is in force while the field is blurred

            await app.set_props_and_fire("agent-keys-toggle", {}, "click")  # open Keys
            await app.set_props_and_fire("agent-keys-toggle", {}, "click")  # close Keys

            # Closing Keys must not mark the field focused: the cap is unchanged.
            assert app.find("composer-draft").max_lines == capped
            assert app.find("agent-composer-slot") is not None  # still in the split
        finally:
            await session.dispose()

    run(scenario())
