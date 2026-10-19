"""Session name semantics and constrained project workspace launch spec."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Collection
from typing import Any, TypeGuard

from relaydeck.contracts.graphemes import grapheme_count
from relaydeck.contracts.graphemes import graphemes as graphemes  # re-exported

RESULT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["type"],
    "additionalProperties": False,
    "properties": {
        "type": {
            "enum": ["agent_started", "tab_created", "agent_prompted", "ok", "partial_creation"]
        },
        **{
            key: {"type": "string", "minLength": 1, "maxLength": 128}
            for key in ("workspace_id", "tab_id", "pane_id", "agent_id")
        },
    },
}

AGENT_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
AGENT_NAME_HELP = "lowercase letters, digits, - or _; start with a letter; up to 32"


def agent_name_error(name: str) -> str:
    return "" if AGENT_NAME.fullmatch(name) else f"Use {AGENT_NAME_HELP}."


DISPLAY_NAME_MAX = 32
DISPLAY_NAME_HELP = "up to 32 characters"
_ZWJ = "\u200d"


def display_name_clean(text: str) -> str:
    """Trim and collapse runs of whitespace to one space."""
    return " ".join(text.split())


def _is_control(char: str) -> bool:
    category = unicodedata.category(char)
    if category == "Cf":
        return char != _ZWJ and not 0xE0020 <= ord(char) <= 0xE007F
    return category in ("Cc", "Cs", "Zl", "Zp")


def display_name_error(name: str) -> str:
    """Free-form display name rule: 1-32 grapheme clusters, no control characters."""
    if any(_is_control(char) for char in name):
        return "Name can't contain control characters"
    cleaned = display_name_clean(name)
    if not cleaned:
        return "Name can't be empty"
    if grapheme_count(cleaned) > DISPLAY_NAME_MAX:
        return f"Use {DISPLAY_NAME_HELP}"
    return ""


def agent_slug(label: str) -> str:
    """Herdr agent name made from a display label; empty when nothing usable remains."""
    folded = "".join(
        char
        for char in unicodedata.normalize("NFKD", label)
        if not unicodedata.category(char).startswith("M")
    ).lower()
    mapped = "".join("-" if char in " ./" else char for char in folded)
    kept = "".join(char for char in mapped if char in AGENT_NAME_CHARS)
    start = next((i for i, char in enumerate(kept) if "a" <= char <= "z"), None)
    if start is None:
        return ""
    return kept[start : start + 32].rstrip("-_")


AGENT_NAME_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789_-")


def has_control(text: str) -> bool:
    """Foundation controlCharacters parity, plus invalid UTF-8 surrogates."""
    return any(unicodedata.category(char) in ("Cc", "Cf", "Cs") for char in text)


def default_agent_name(kind: str, taken: Collection[str]) -> str:
    if kind not in taken:
        return kind
    suffix = 2
    while f"{kind}-{suffix}" in taken:
        suffix += 1
    return f"{kind}-{suffix}"


def valid_new_workspace(spec: Any) -> TypeGuard[dict[str, str]]:
    if not isinstance(spec, dict) or set(spec) - {"directory", "label"}:
        return False
    directory, label = spec.get("directory", ""), spec.get("label", "")
    return (
        isinstance(directory, str)
        and len(directory) <= 1024
        and (not directory or directory.startswith("/"))
        and not has_control(directory)
        and isinstance(label, str)
        and len(label) <= 64
        and not has_control(label)
    )


def public_command_result(raw: Any) -> dict[str, str] | None:
    """Expose only bounded resource identities, never arbitrary upstream result content."""
    if not isinstance(raw, dict) or raw.get("type") not in (
        "agent_started",
        "tab_created",
        "agent_prompted",
        "ok",
        "partial_creation",
    ):
        return None
    out = {"type": raw["type"]}
    for key in ("workspace_id", "tab_id", "pane_id", "agent_id"):
        if key not in raw:
            continue
        value = raw[key]
        if not isinstance(value, str) or not 1 <= len(value) <= 128:
            return None
        if has_control(value):
            return None
        out[key] = value
    return out
