"""Agent prompt command envelope. Pure mapping from a
validated composer draft to the gateway command envelope.

The gateway operation name `agent.prompt` mirrors the verified upstream
method, but gateway fields and target shape are RelayDeck design; the
adapter translates into the exact upstream parameter schema. Live
dispatch of the recorded intent belongs to the gateway adapter.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from relaydeck.contracts.schemas import GATEWAY_PROTOCOL


def agent_prompt_command(
    *,
    command_id: str,
    host_id: str,
    host_epoch: str,
    agent_id: str,
    text: str,
    now: datetime | None = None,
    ttl_seconds: int = 120,
    expected_revision: int = 0,
) -> dict[str, Any]:
    at = now or datetime.now(UTC)
    return {
        "protocol": GATEWAY_PROTOCOL,
        "command_id": command_id,
        "host_id": host_id,
        "host_epoch": host_epoch,
        "kind": "agent.prompt",
        "expected_revision": expected_revision,
        "target": {"agent_id": agent_id},
        "payload": {"text": text},
        "expires_at": (at + timedelta(seconds=ttl_seconds)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
