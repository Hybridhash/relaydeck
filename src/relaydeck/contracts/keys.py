"""Herdr API key-combo grammar (upstream-derived, not designed).

Mirrors Herdr 0.9.1 `src/config/keybinds.rs::parse_key_combo` plus the
`src/app/api_helpers.rs::normalize_api_key_alias` aliases at pinned
commit 065ef9d6. The gateway validates `key` input frames against this
grammar so bad combos fail fast; Herdr re-parses at dispatch and remains
authoritative. Exact terminal bytes are Herdr's encoder output (verified
live in); this module never invents byte mappings.
"""

from __future__ import annotations

MODIFIERS = frozenset(
    {"ctrl", "control", "shift", "alt", "option", "meta", "cmd", "command", "super", "hyper"}
)

# Lowercased single-token key names from parse_key_combo (plus " ").
NAMED_KEYS = frozenset(
    {
        "space",
        " ",
        "enter",
        "return",
        "esc",
        "escape",
        "tab",
        "backspace",
        "bs",
        "left",
        "right",
        "up",
        "down",
        "minus",
        "comma",
        "period",
        "slash",
        "backslash",
        "quote",
        "double_quote",
        "double-quote",
        "semicolon",
        "colon",
        "percent",
        "ampersand",
        "backtick",
        "plus",
    }
)

# normalize_api_key_alias: applied before combo parsing, case-sensitive.
ALIASES = {"C-c": "ctrl+c", "c-c": "ctrl+c", "+": "plus"}

MAX_KEY_LEN = 64


def normalize_combo(name: str) -> tuple[frozenset[str], str] | None:
    """Parse `mod+mod+key` into (modifiers, key). None when invalid."""
    if not isinstance(name, str) or not name or len(name) > MAX_KEY_LEN:
        return None
    text = ALIASES.get(name.strip(), name.strip())
    parts = text.split("+")
    modifiers: set[str] = set()
    key: str | None = None
    for part in parts:
        token = part.strip()
        if not token:
            return None
        lowered = token.lower()
        if lowered in MODIFIERS:
            modifiers.add(lowered)
        elif key is not None:
            return None
        else:
            key = token
    if key is None:
        return None
    lowered = key.lower()
    if lowered in NAMED_KEYS:
        return frozenset(modifiers), lowered
    if len(key) == 1:
        return frozenset(modifiers), lowered
    if lowered.startswith("f") and lowered[1:].isdigit():
        # Upstream parses u8: f0..f255 accepted, f256 rejected.
        number = int(lowered[1:])
        if 0 <= number <= 255:
            return frozenset(modifiers), f"f{number}"
        return None
    return None


def is_valid_key(name: str) -> bool:
    return normalize_combo(name) is not None
