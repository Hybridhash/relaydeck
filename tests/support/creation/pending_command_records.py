"""Pending command and record builders."""

from __future__ import annotations

from typing import Any

from relaydeck.client.features.composer.commands import agent_prompt_command


def command(
    identity: str = "one", host: str = "home-mac", target: str = "agent-1"
) -> dict[str, Any]:
    return agent_prompt_command(
        command_id="pending-" + identity,
        host_id=host,
        host_epoch="epoch-7",
        agent_id=target,
        text="PRIVATE_PROMPT",
    )


def record(
    body: dict[str, Any], outcome: str = "accepted", result: dict[str, Any] | None = None
) -> dict[str, Any]:
    doc = {
        "protocol": body["protocol"],
        "command_id": body["command_id"],
        "host_id": body["host_id"],
        "kind": body["kind"],
        "target": body["target"],
        "status": outcome,
    }
    if result is not None:
        doc["result"] = result
    return doc
