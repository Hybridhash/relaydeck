"""Device-local terminal and inventory preferences shown in Settings.

Flet-free. Only rows the app can honour are modelled; the design's
terminal theme, per-event notification toggles, SSH key and update check
have no backing yet and are deliberately absent (no dead controls).

Persistence goes through an injected async key/value store (Flet
`SharedPreferences` in the app, a dict in tests). A missing, corrupt or
unavailable store falls back to defaults; preferences never block the
UI and never hold secrets.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, replace
from typing import Any

from relaydeck.client.features.terminal.input_modes import (
    WORKFLOW_AGENT_COMPOSER,
    WORKFLOW_AGENT_DIRECT,
)

PREFS_KEY = "relaydeck.terminal_prefs.v1"

FONT_MIN = 9.0
FONT_MAX = 22.0
FONT_STEP = 1.0
FONT_DEFAULT_WEB = 13.0
FONT_DEFAULT_PHONE = 12.0

AGENT_WORKFLOWS = (WORKFLOW_AGENT_COMPOSER, WORKFLOW_AGENT_DIRECT)


@dataclass(frozen=True)
class TerminalPrefs:
    """`font_size` None means the platform default (13 px web, 12 px phone)."""

    font_size: float | None = None
    agents_open_in: str = WORKFLOW_AGENT_COMPOSER
    confirm_takeover: bool = True
    # Opening an agent takes control automatically (an explicit product decision).
    # Another device holding control still gets the explicit conflict prompt.
    auto_takeover: bool = True
    show_key_row: bool = True
    # a light tap on Send and Copy (native apps; not iPhone/iPad browsers).
    haptics: bool = True
    space_key: str = ""

    def effective_font_size(self, phone: bool) -> float:
        if self.font_size is not None:
            return self.font_size
        return FONT_DEFAULT_PHONE if phone else FONT_DEFAULT_WEB

    def step_font(self, delta: float, phone: bool) -> TerminalPrefs:
        size = min(FONT_MAX, max(FONT_MIN, self.effective_font_size(phone) + delta))
        return replace(self, font_size=size)


def clean(raw: Any) -> TerminalPrefs:
    """Validate a stored document field by field; bad fields use defaults."""
    if not isinstance(raw, dict):
        return TerminalPrefs()
    prefs = TerminalPrefs()
    size = raw.get("font_size")
    if isinstance(size, int | float) and not isinstance(size, bool):
        prefs = replace(prefs, font_size=min(FONT_MAX, max(FONT_MIN, float(size))))
    workflow = raw.get("agents_open_in")
    if workflow in AGENT_WORKFLOWS:
        prefs = replace(prefs, agents_open_in=workflow)
    confirm = raw.get("confirm_takeover")
    if isinstance(confirm, bool):
        prefs = replace(prefs, confirm_takeover=confirm)
    auto = raw.get("auto_takeover")
    if isinstance(auto, bool):
        prefs = replace(prefs, auto_takeover=auto)
    show_keys = raw.get("show_key_row")
    if isinstance(show_keys, bool):
        prefs = replace(prefs, show_key_row=show_keys)
    haptic = raw.get("haptics")
    if isinstance(haptic, bool):
        prefs = replace(prefs, haptics=haptic)
    space = raw.get("space_key")
    if (
        isinstance(space, str)
        and len(space) <= 512
        and (space == "" or (":" in space and space.strip() == space))
        and all(ord(char) >= 32 and ord(char) != 127 for char in space)
    ):
        prefs = replace(prefs, space_key=space)
    return prefs


def encode(prefs: TerminalPrefs) -> str:
    return json.dumps(asdict(prefs), sort_keys=True)


async def load(get: Callable[[str], Awaitable[Any]]) -> TerminalPrefs:
    try:
        stored = await get(PREFS_KEY)
    except Exception:  # noqa: BLE001 - storage unavailable: defaults
        return TerminalPrefs()
    if not isinstance(stored, str):
        return TerminalPrefs()
    try:
        return clean(json.loads(stored))
    except ValueError:
        return TerminalPrefs()


async def save(set_: Callable[[str, str], Awaitable[Any]], prefs: TerminalPrefs) -> bool:
    try:
        await set_(PREFS_KEY, encode(prefs))
    except Exception:  # noqa: BLE001 - storage unavailable: keep in memory
        return False
    return True


__all__ = [
    "AGENT_WORKFLOWS",
    "FONT_DEFAULT_PHONE",
    "FONT_DEFAULT_WEB",
    "FONT_MAX",
    "FONT_MIN",
    "FONT_STEP",
    "PREFS_KEY",
    "TerminalPrefs",
    "clean",
    "encode",
    "load",
    "save",
]
