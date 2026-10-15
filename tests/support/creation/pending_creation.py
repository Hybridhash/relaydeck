"""Pending-creation builders."""

from __future__ import annotations

from typing import Any

from relaydeck.client.features.create.commands import agent_start_command, shell_create_command


def _creation(agent: bool, new_workspace: bool = False) -> dict[str, Any]:
    args: dict[str, Any] = {
        "command_id": "pending-creation",
        "host_id": "home-mac",
        "host_epoch": "epoch-7",
        "workspace_id": "w1",
        "new_workspace": {"label": "qa"} if new_workspace else None,
    }
    return agent_start_command(**args, kind="agent-a") if agent else shell_create_command(**args)


def _record(body: dict[str, Any], outcome: str) -> dict[str, Any]:
    agent = body["kind"] == "agent.start"
    return {
        **{k: body[k] for k in ("protocol", "command_id", "host_id", "kind", "target")},
        "status": outcome,
        "result": {
            "type": "agent_started" if agent else "tab_created",
            "agent_id" if agent else "tab_id": "created-resource",
            "workspace_id": "w2",
        },
    }
