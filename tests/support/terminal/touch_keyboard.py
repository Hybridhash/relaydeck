"""Shared agent surface for native touch-keyboard and scroll-overlay checks."""

from __future__ import annotations

from typing import Any

import flet as ft

from relaydeck.client.features.terminal.surfaces import mount_agent_screen


@ft.component
def _Screen(session: Any, touch: bool, phone: bool = False) -> Any:
    return ft.Container(
        expand=True,
        content=mount_agent_screen(  # type: ignore[call-arg]
            session,
            width_tier="compact" if phone else "expanded",
            phone=phone,
            viewport_height=800,
            viewport_width=390.0 if phone else 1376.0,
            touch=touch,
        ),
    )
