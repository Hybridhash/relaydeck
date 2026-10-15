"""Session name semantics and constrained project workspace launch spec."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Collection
from typing import Any, TypeGuard

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
