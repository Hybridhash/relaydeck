"""Authorization policy decision point.

Pure functions: capabilities, per-kind payload validation, rate limits,
plaintext trust. No I/O, no clock access except injected timestamps.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from relaydeck.contracts.catalog import AGENT_KIND
from relaydeck.contracts.launch import (
    agent_name_error,
    display_name_error,
    has_control,
    valid_new_workspace,
)

COMMAND_CAPABILITY = {
    "agent.prompt": "agents.prompt",
    "agent.start": "agents.start",
    "agent.rename": "agents.start",
    "shell.create": "shells.create",
    "session.close": "terminal.control",
}

# target shapes: allowed keys per kind and the required subset.
# Unknown target keys are rejected; missing required keys are rejected.
TARGET_SHAPES = {
    "agent.prompt": (("agent_id",), ("agent_id",)),
    "agent.start": (("workspace_id",), ("workspace_id",)),
    "agent.rename": (("agent_id",), ("agent_id",)),
    "shell.create": (("workspace_id",), ("workspace_id",)),
    "session.close": (("terminal_id",), ("terminal_id",)),
}

MAX_PROMPT_CHARS = 16_000
MAX_ARGV_ITEMS = 32
MAX_ARGV_ITEM_CHARS = 512
MAX_COMMANDS_PER_MINUTE = 20
MAX_INPUT_BYTES_PER_SECOND = 100 * 1024


def _check_target(kind: str, target: dict[str, Any]) -> str | None:
    if kind in ("agent.start", "shell.create") and target == {"workspace_mode": "new"}:
        return None
    allowed, required = TARGET_SHAPES[kind]
    if set(target) - set(allowed):
        return "forbidden"
    for key in required:
        value = target.get(key, "")
        if not isinstance(value, str) or not value or len(value) > 128:
            return "forbidden"
    for key in set(target) - set(required):
        value = target[key]
        if not isinstance(value, str) or not value or len(value) > 128:
            return "forbidden"
    return None


def authorize_command(
    capabilities: frozenset[str],
    allowed_hosts: frozenset[str],
    kind: str,
    host_id: str,
    target: dict[str, Any],
    payload: dict[str, Any],
) -> str | None:
    """Return None when allowed, else an error code."""
    need = COMMAND_CAPABILITY.get(kind)
    if need is None:
        return "unsupported_capability"
    if need not in capabilities:
        return "unsupported_capability"
    if host_id not in allowed_hosts:
        return "not_found"
    if not isinstance(target, dict):
        return "forbidden"
    denied = _check_target(kind, target)
    if denied:
        return denied
    new_workspace = target == {"workspace_mode": "new"}
    if new_workspace and not valid_new_workspace(payload.get("new_workspace")):
        return "forbidden"
    if kind == "agent.prompt":
        if set(payload) != {"text"}:
            return "forbidden"
        text = payload.get("text", "")
        if not isinstance(text, str) or not text.strip():
            return "forbidden"
        if len(text) > MAX_PROMPT_CHARS:
            return "too_large"
    elif kind == "agent.rename":
        name = payload.get("name")
        if set(payload) != {"name"} or not isinstance(name, str) or display_name_error(name):
            return "forbidden"
    elif kind == "agent.start":
        required = {"kind", "name"} | ({"new_workspace"} if new_workspace else set())
        if not required <= set(payload) or set(payload) - required - {"argv"}:
            return "forbidden"
        # shape only; the connector's verify_agent_kind is the allowlist.
        agent_kind = payload.get("kind")
        if not isinstance(agent_kind, str) or AGENT_KIND.fullmatch(agent_kind) is None:
            return "forbidden"
        name = payload.get("name", "")
        if not isinstance(name, str) or agent_name_error(name):
            return "forbidden"
        argv = payload.get("argv", [])
        if not isinstance(argv, list) or len(argv) > MAX_ARGV_ITEMS:
            return "forbidden"
        for item in argv:
            if not isinstance(item, str) or len(item) > MAX_ARGV_ITEM_CHARS or has_control(item):
                return "forbidden"
    elif kind == "shell.create":
        # Workspace selects via target.workspace_id; payload carries only the label.
        if set(payload) - {"label"} - ({"new_workspace"} if new_workspace else set()):
            return "forbidden"
        label = payload.get("label", "")
        if label is not None and (
            not isinstance(label, str) or len(label) > 64 or has_control(label)
        ):
            return "forbidden"
    elif kind == "session.close":
        if payload:
            return "forbidden"
    return None


@dataclass
class RateLimiter:
    """Token bucket per key. Pure apart from injected now_ms."""

    capacity: float
    refill_per_ms: float
    _buckets: dict[str, tuple[float, int]] = field(default_factory=dict)

    def check(self, key: str, now_ms: int, cost: float = 1.0) -> bool:
        tokens, updated = self._buckets.get(key, (self.capacity, now_ms))
        tokens = min(self.capacity, tokens + (now_ms - updated) * self.refill_per_ms)
        if tokens < cost:
            self._buckets[key] = (tokens, now_ms)
            return False
        self._buckets[key] = (tokens - cost, now_ms)
        return True


def command_limiter() -> RateLimiter:
    return RateLimiter(
        capacity=MAX_COMMANDS_PER_MINUTE,
        refill_per_ms=MAX_COMMANDS_PER_MINUTE / 60_000,
    )


def input_limiter() -> RateLimiter:
    return RateLimiter(
        capacity=MAX_INPUT_BYTES_PER_SECOND,
        refill_per_ms=MAX_INPUT_BYTES_PER_SECOND / 1000,
    )
