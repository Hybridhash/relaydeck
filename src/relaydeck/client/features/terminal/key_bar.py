"""Pure compact key-bar model (no layout, no Flet).

Single scrolling key row for the compact phone terminal: groups, order,
labels, long-press alternates, sticky Ctrl/Alt latch, and the width-class
function. The layout consumes `KEY_BAR_GROUPS`,
`KEY_BAR`, `ModifierLatch` and `width_class`; emission stays in
`keys.encode_key` / `KeyInputSession`, which this module never reimplements.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from relaydeck.client.features.terminal.keys import (
    MOD_ALT,
    MOD_CTRL,
    MODIFIERS,
    SHIFT_TAB,
)

Kind = Literal["key", "modifier", "action"]

#: Double-tap window (ms) promoting a sticky modifier from one-shot to locked.
LATCH_WINDOW_MS = 350

_COMPACT_MAX = 600
_MEDIUM_MAX = 1024


@dataclass(frozen=True)
class KeyBarItem:
    """One key-bar chip. `key`/`mods` feed `encode_key` verbatim; `None`
    key means the chip never emits bytes itself (modifiers, actions).
    `long_press` is the (key, mods) alternate for a long press."""

    id: str
    label: str
    kind: Kind
    key: str | None
    mods: frozenset[str]
    long_press: tuple[str, frozenset[str]] | None
    semantics: str


def _key(
    id: str,
    label: str,
    key: str,
    semantics: str,
    *,
    mods: frozenset[str] = frozenset(),
    long_press: tuple[str, frozenset[str]] | None = None,
) -> KeyBarItem:
    return KeyBarItem(id, label, "key", key, mods, long_press, semantics)


def _mod(id: str, label: str, modifier: str, semantics: str) -> KeyBarItem:
    return KeyBarItem(id, label, "modifier", None, frozenset({modifier}), None, semantics)


def _action(id: str, label: str, semantics: str) -> KeyBarItem:
    return KeyBarItem(id, label, "action", None, frozenset(), None, semantics)


KEY_BAR_GROUPS: tuple[tuple[KeyBarItem, ...], ...] = (
    (
        _key("key-esc", "Esc", "escape", "Escape"),
        _key("key-tab", "Tab", "tab", "Tab"),
        _key("key-shift-tab", "\u21e7Tab", SHIFT_TAB, "Shift Tab"),
    ),
    (
        _key("key-left", "\u2190", "left", "Left Arrow", long_press=("home", frozenset())),
        _key("key-up", "\u2191", "up", "Up Arrow", long_press=("page-up", frozenset())),
        _key(
            "key-down",
            "\u2193",
            "down",
            "Down Arrow",
            long_press=("page-down", frozenset()),
        ),
        _key("key-right", "\u2192", "right", "Right Arrow", long_press=("end", frozenset())),
    ),
    (
        _key(
            "key-backspace",
            "\u232b",
            "backspace",
            "Backspace",
            long_press=("w", frozenset({MOD_CTRL})),
        ),
        _key("key-enter", "\u23ce", "enter", "Enter"),
    ),
    (
        _mod("mod-ctrl", "Ctrl", MOD_CTRL, "Control"),
        _mod("mod-alt", "Alt", MOD_ALT, "Alt"),
    ),
    (
        _key("key-ctrl-c", "^C", "c", "Control C", mods=frozenset({MOD_CTRL})),
        _key("key-ctrl-d", "^D", "d", "Control D", mods=frozenset({MOD_CTRL})),
        _key("key-ctrl-z", "^Z", "z", "Control Z", mods=frozenset({MOD_CTRL})),
        _key("key-ctrl-l", "^L", "l", "Control L", mods=frozenset({MOD_CTRL})),
        _key("key-ctrl-r", "^R", "r", "Control R", mods=frozenset({MOD_CTRL})),
    ),
    (
        _key("key-slash", "/", "/", "Slash"),
        _key("key-tilde", "~", "~", "Tilde"),
        _key("key-pipe", "|", "|", "Pipe"),
        _key("key-minus", "-", "-", "Minus"),
    ),
    (
        _action("action-paste", "Paste", "Paste"),
        _action("action-select", "Select", "Select text"),
    ),
)

KEY_BAR: tuple[KeyBarItem, ...] = tuple(item for group in KEY_BAR_GROUPS for item in group)


class ModifierLatch:
    """Sticky Ctrl/Alt state machine. Pure: time enters only through the
    `now_ms` argument, so tests drive it without clocks or sleeps.

    Per modifier: "off" | "once" (one-shot, clears after next key) |
    "locked" (stays until tapped again). `tap`: off→once; once→locked when
    the previous tap of the same modifier is < 350 ms old, else off;
    locked→off. `consume` (after a non-modifier key): once→off, locked stays.
    """

    def __init__(self) -> None:
        self._state: dict[str, str] = {}
        self._tapped_at: dict[str, int] = {}

    @staticmethod
    def _check(modifier: str) -> None:
        if modifier not in MODIFIERS:
            raise ValueError(f"unknown modifier {modifier!r}")

    def state(self, modifier: str) -> str:
        self._check(modifier)
        return self._state.get(modifier, "off")

    def tap(self, modifier: str, now_ms: int) -> str:
        self._check(modifier)
        previous = self._state.get(modifier, "off")
        if previous == "once" and now_ms - self._tapped_at.get(modifier, now_ms) < LATCH_WINDOW_MS:
            current = "locked"
        elif previous == "once":
            current = "off"
        elif previous == "locked":
            current = "off"
        else:
            current = "once"
        self._state[modifier] = current
        self._tapped_at[modifier] = now_ms
        return current

    def consume(self) -> None:
        for modifier in tuple(self._state):
            if self._state[modifier] == "once":
                self._state[modifier] = "off"

    def armed(self) -> frozenset[str]:
        return frozenset(mod for mod, held in self._state.items() if held in ("once", "locked"))


def width_class(width: float) -> str:
    """Layout class by viewport width: `compact` < 600px (phones), `medium`
    600-1023 (tablets/small windows), `expanded` >= 1024 (desktop web)."""
    if width < _COMPACT_MAX:
        return "compact"
    if width < _MEDIUM_MAX:
        return "medium"
    return "expanded"


__all__ = [
    "KEY_BAR",
    "KEY_BAR_GROUPS",
    "LATCH_WINDOW_MS",
    "KeyBarItem",
    "Kind",
    "ModifierLatch",
    "width_class",
]
