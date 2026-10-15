"""Preferences hook of `Root`.

Plain functions called from `Root` at the position the inline code used to occupy, so hook
order and the control tree are unchanged. Each takes only what it reads (keyword-only) and
returns the names the rest of `Root` still uses.
"""

from __future__ import annotations

import asyncio
from typing import Any

import flet as ft

from relaydeck.client.features.settings import prefs as prefs_store
from relaydeck.client.features.settings.prefs import TerminalPrefs

# design tokens (minimal first theme, centrally configurable).


def use_preferences(*, preferences: Any) -> tuple[Any, ...]:
    """Device-local terminal preferences: loaded once, saved through the platform store."""
    # Device-local terminal preferences shown in Settings. Loaded once;
    # every change is saved through the platform key/value store. The
    # store is optional: without one the values live for this session.
    prefs, set_prefs_state = ft.use_state(TerminalPrefs())

    prefs_saved, set_prefs_saved = ft.use_state(True)

    prefs_box: Any = ft.use_ref(None)

    prefs_ref: Any = ft.use_ref(TerminalPrefs())

    prefs_edited: Any = ft.use_ref(False)

    prefs_save_lock: Any = ft.use_ref(lambda: asyncio.Lock())

    prefs_ref.current = prefs

    def load_prefs() -> None:
        if prefs_box.current is None:
            try:
                prefs_box.current = preferences or ft.SharedPreferences()
            except Exception:  # noqa: BLE001 - no page services (tests)
                prefs_box.current = False
        store = prefs_box.current
        if not store:
            set_prefs_saved(False)
            return

        async def load() -> None:
            loaded = await prefs_store.load(store.get)
            if (
                not prefs_edited.current
                and loaded != prefs_ref.current
                and prefs_ref.current == TerminalPrefs()
            ):
                set_prefs_state(loaded)

        asyncio.create_task(load())

    ft.use_effect(load_prefs, dependencies=[])

    def set_prefs(value: TerminalPrefs) -> None:
        prefs_edited.current = True
        set_prefs_state(value)
        prefs_ref.current = value
        store = prefs_box.current
        if not store:
            return

        async def persist() -> None:
            async with prefs_save_lock.current:
                if value != prefs_ref.current:
                    return
                saved = await prefs_store.save(store.set, value)
                if value == prefs_ref.current:
                    set_prefs_saved(saved)

        asyncio.create_task(persist())

    return (prefs, prefs_ref, prefs_saved, set_prefs)
