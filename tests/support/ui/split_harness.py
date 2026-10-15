"""Shared split-typing harness (agent detail in a touch, expanded or phone layout).

Used by the split test modules; kept in support so no test module imports another.
"""

from __future__ import annotations

from typing import Any

import flet as ft

from relaydeck.client.features.terminal.input_modes import KEYBOARD_KEYS
from relaydeck.client.features.terminal.surfaces import mount_agent_screen
from tests.support.host_harness import render_component
from tests.support.terminal.sessions import _agent_session


@ft.component
def SplitHarness(
    session: Any, focus_log: list[bool], initial: bool = False, phone: bool = False
) -> Any:
    split, set_split = ft.use_state(initial)
    return ft.Column(
        expand=True,
        controls=[
            ft.Button("toggle", key="toggle-split", on_click=lambda _e: set_split(not split)),
            ft.Container(
                expand=True,
                content=mount_agent_screen(  # type: ignore[call-arg]
                    session,
                    width_tier="compact" if phone else "expanded",
                    phone=phone,
                    viewport_height=300 if split else 800,
                    touch=True,
                    typing_split=split,
                    on_composer_focus=focus_log.append,
                ),
            ),
        ],
    )


async def split_mounted(
    *, split: bool, dock: bool = False, phone: bool = False
) -> tuple[Any, Any, list[bool]]:
    session, _ops, _factory, _submitted = _agent_session()
    session.open()
    await session.confirm_takeover()
    if dock:
        session.set_keyboard(KEYBOARD_KEYS)
    log: list[bool] = []
    app = await render_component(SplitHarness, session, log, split, phone)
    await app.set_props_and_fire("agent-detail-panel", {}, "size_change", {"w": 1224.0, "h": 480.0})
    return app, session, log
