"""Creation command builders. Pure mapping from
validated drafts to gateway command envelopes."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from relaydeck.contracts.launch import default_agent_name
from relaydeck.contracts.schemas import GATEWAY_PROTOCOL


def _envelope(
    *,
    command_id: str,
    host_id: str,
    host_epoch: str,
    kind: str,
    target: dict[str, str],
    payload: dict[str, Any],
    now: datetime | None = None,
    ttl_seconds: int = 120,
) -> dict[str, Any]:
    at = now or datetime.now(UTC)
    return {
        "protocol": GATEWAY_PROTOCOL,
        "command_id": command_id,
        "host_id": host_id,
        "host_epoch": host_epoch,
        "kind": kind,
        "target": target,
        "payload": payload,
        "expires_at": (at + timedelta(seconds=ttl_seconds)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def agent_start_command(
    *,
    command_id: str,
    host_id: str,
    host_epoch: str,
    workspace_id: str,
    kind: str,
    name: str = "",
    argv: list[str] | None = None,
    now: datetime | None = None,
    existing_names: frozenset[str] = frozenset(),
    new_workspace: dict[str, str] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "kind": kind,
        "name": name.strip() or default_agent_name(kind, existing_names),
    }
    if argv:
        payload["argv"] = list(argv)
    if new_workspace is not None:
        payload["new_workspace"] = dict(new_workspace)
    return _envelope(
        command_id=command_id,
        host_id=host_id,
        host_epoch=host_epoch,
        kind="agent.start",
        target={"workspace_mode": "new"}
        if new_workspace is not None
        else {"workspace_id": workspace_id},
        payload=payload,
        now=now,
    )


def shell_create_command(
    *,
    command_id: str,
    host_id: str,
    host_epoch: str,
    workspace_id: str,
    label: str = "",
    now: datetime | None = None,
    new_workspace: dict[str, str] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    if label:
        payload["label"] = label
    if new_workspace is not None:
        payload["new_workspace"] = dict(new_workspace)
    return _envelope(
        command_id=command_id,
        host_id=host_id,
        host_epoch=host_epoch,
        kind="shell.create",
        target={"workspace_mode": "new"}
        if new_workspace is not None
        else {"workspace_id": workspace_id},
        payload=payload,
        now=now,
    )


def session_close_command(
    *,
    command_id: str,
    host_id: str,
    host_epoch: str,
    terminal_id: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Close the Herdr pane for a shell terminal. Payload is empty."""
    return _envelope(
        command_id=command_id,
        host_id=host_id,
        host_epoch=host_epoch,
        kind="session.close",
        target={"terminal_id": terminal_id},
        payload={},
        now=now,
    )
