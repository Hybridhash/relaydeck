"""Command-journal messages and clock."""

from __future__ import annotations

from typing import Any

from relaydeck.journal import digest_for

NOW = 1_700_000_000_000


def _dispatch_msg(
    command_id: str, kind: str, target: dict[str, Any], payload: dict[str, Any]
) -> dict[str, Any]:
    return {
        "kind": "command.dispatch",
        "command_id": command_id,
        "principal": "acct-1",
        "host_id": "home-mac",
        "gateway_kind": kind,
        "target": target,
        "payload": payload,
        "digest": digest_for("home-mac", kind, target, payload),
        "generation": 0,
        "expires_ms": 0,
    }
