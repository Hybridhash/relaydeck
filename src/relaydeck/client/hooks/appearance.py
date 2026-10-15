"""Appearance hook of `Root`.

Plain functions called from `Root` at the position the inline code used to occupy, so hook
order and the control tree are unchanged. Each takes only what it reads (keyword-only) and
returns the names the rest of `Root` still uses.
"""

from __future__ import annotations

from typing import Any

import flet as ft

from relaydeck.client.components.appearance import AppearanceBridge

# design tokens (minimal first theme, centrally configurable).


def use_appearance(*, appearance: AppearanceBridge | None, dark: bool) -> Any:
    """Subscribe to the appearance bridge; returns the current `dark` flag."""
    dark, set_dark = ft.use_state(appearance.dark if appearance is not None else dark)

    appearance_unsub: Any = ft.use_ref(None)

    def subscribe_appearance() -> None:
        if appearance is not None:
            set_dark(appearance.dark)
            appearance_unsub.current = appearance.subscribe(set_dark)

    def unsubscribe_appearance() -> None:
        if appearance_unsub.current is not None:
            appearance_unsub.current()
            appearance_unsub.current = None

    ft.use_effect(subscribe_appearance, dependencies=[appearance], cleanup=unsubscribe_appearance)
    return dark
