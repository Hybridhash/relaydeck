"""Bounded metadata inventory/read marks; never reads terminal output.

The gateway owns this projection. Activity timestamps are observed metadata
times, not guessed agent start times. A bootstrap alone has unknown activity.
Read markers belong to an account + host + epoch + resource, not a device.
"""

from __future__ import annotations

import hashlib
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from relaydeck.contracts.canonical import canonical_json
from relaydeck.domain.agent_order import metadata_order
from relaydeck.gateway.backend import BackendError
from relaydeck.gateway.cursors import CursorError, decode_cursor, encode_cursor

STATUSES = frozenset({"idle", "working", "blocked", "done", "unknown"})
MAX_HOSTS = 128
MAX_AGENTS = 4096
MAX_READ_MARKS = 16384
_MEANINGFUL = ("status", "name", "title", "state_change_seq", "interactive_ready")
_AGENT_FIELDS = (
    "terminal_id",
    "workspace_id",
    "tab_id",
    "pane_id",
    "name",
    "agent",
    "title",
    "status",
    "focused",
    "revision",
    "state_change_seq",
    "interactive_ready",
)


def _hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def _observed_ms(event: dict[str, Any]) -> int:
    try:
        return max(
            0,
            int(
                datetime.fromisoformat(event["observed_at"].replace("Z", "+00:00")).timestamp()
                * 1000
            ),
        )
    except (KeyError, TypeError, ValueError, OverflowError):
        return 0


@dataclass
class _Host:
    epoch: str
    activities: dict[str, tuple[str, int]] = field(default_factory=dict)
    sequence: int = 0
    facts: dict[str, tuple[Any, ...]] = field(default_factory=dict)


class InventoryView:
    def __init__(self) -> None:
        self._hosts: dict[str, _Host] = {}
        self._read: OrderedDict[tuple[str, str, str, str], str] = OrderedDict()

    def drop_host(self, host_id: str) -> None:
        self._hosts.pop(host_id, None)
        for key in [key for key in self._read if key[1] == host_id]:
            del self._read[key]

    def drop_account(self, account: str) -> None:
        for key in [key for key in self._read if key[0] == account]:
            del self._read[key]

    def rows(
        self,
        account: str,
        host_id: str,
        snapshot: dict[str, Any],
        events: list[dict[str, Any]],
        secret: bytes,
    ) -> list[dict[str, Any]]:
        host = snapshot.get("host", {})
        if host.get("id") != host_id:
            raise BackendError("upstream_incompatible", "inventory host mismatch")
        epoch = str(host.get("epoch", ""))
        held = self._hosts.get(host_id)
        if held is None or held.epoch != epoch:
            self.drop_host(host_id)
            if len(self._hosts) >= MAX_HOSTS:
                raise BackendError("rate_limited", "inventory host capacity reached")
            held = _Host(epoch)
            self._hosts[host_id] = held
        raw_agents = snapshot.get("agents", [])
        if len(raw_agents) > MAX_AGENTS:
            raise BackendError("too_large", "inventory agent capacity reached")
        agents = {str(raw["terminal_id"]): dict(raw) for raw in raw_agents}
        workspaces = {str(raw["id"]): dict(raw) for raw in snapshot.get("workspaces", [])}
        snapshot_seq = 0
        try:
            cursor = decode_cursor(secret, snapshot["cursor"])
            if cursor.get("host_id") == host_id and type(cursor.get("sequence")) is int:
                snapshot_seq = max(0, cursor["sequence"])
        except CursorError:
            pass  # Explicit fixtures can carry unsigned cursors; production ingest stamps them.
        # Fold retained metadata each time so fresh snapshots and old event
        # delivery converge; activity assignment itself is sequence-idempotent.
        for event in sorted(events, key=lambda item: item.get("sequence", 0)):
            if event.get("host_id") != host_id or event.get("host_epoch") != epoch:
                continue
            seq = int(event.get("sequence", 0))
            kind, resource = event.get("type"), str(event.get("resource_id", ""))
            payload = event.get("payload", {})
            if not isinstance(payload, dict):
                continue
            targets: list[str] = []
            if kind == "agent.upserted" and resource in agents:
                row = agents[resource]
                revision = int(event.get("resource_revision", 0))
                if revision >= int(row.get("revision", 0)):
                    before = held.facts.get(resource, tuple(row.get(key) for key in _MEANINGFUL))
                    after = tuple(payload.get(key, row.get(key)) for key in _MEANINGFUL)
                    targets = [resource] if before != after else []
                    if seq > snapshot_seq:
                        row.update({key: payload[key] for key in _AGENT_FIELDS if key in payload})
                        row["revision"] = revision
            elif kind in ("pane.upserted", "tab.upserted"):
                for agent_id, row in agents.items():
                    if row.get("pane_id" if kind == "pane.upserted" else "tab_id") == resource:
                        explicit_status = (
                            kind == "pane.upserted" and payload.get("agent_status") in STATUSES
                        )
                        if explicit_status and seq > snapshot_seq:
                            row["status"] = payload["agent_status"]
                        metadata_before = held.facts.get(agent_id)
                        after = tuple(row.get(key) for key in _MEANINGFUL)
                        if explicit_status or (
                            metadata_before is not None and metadata_before != after
                        ):
                            targets.append(agent_id)
            elif kind == "workspace.upserted" and resource in workspaces:
                if "label" in payload and seq > snapshot_seq:
                    workspaces[resource]["label"] = payload["label"]
            elif kind == "resource.removed" and seq > snapshot_seq:
                for agent_id, row in list(agents.items()):
                    if resource in (
                        agent_id,
                        row.get("pane_id"),
                        row.get("tab_id"),
                        row.get("workspace_id"),
                    ):
                        del agents[agent_id]
            if seq > held.sequence:
                for agent_id in targets:
                    held.activities[agent_id] = (
                        _hash([epoch, event.get("event_id"), agent_id]),
                        _observed_ms(event),
                    )
                held.sequence = max(held.sequence, seq)
        held.activities = {key: value for key, value in held.activities.items() if key in agents}
        held.facts = {
            key: tuple(row.get(field) for field in _MEANINGFUL) for key, row in agents.items()
        }
        out: list[dict[str, Any]] = []
        for agent_id, raw in agents.items():
            activity_id, at = held.activities.get(agent_id, ("", 0))
            workspace = workspaces.get(str(raw.get("workspace_id", "")), {})
            out.append(
                {
                    "terminal_id": agent_id,
                    "name": raw.get("name", ""),
                    "agent": raw.get("agent", ""),
                    "title": raw.get("title", ""),
                    "status": raw.get("status", "unknown"),
                    "focused": bool(raw.get("focused", False)),
                    "revision": int(raw.get("revision", 0)),
                    "workspace_id": raw.get("workspace_id", ""),
                    "project": workspace.get("label", ""),
                    "tab_id": raw.get("tab_id", ""),
                    "pane_id": raw.get("pane_id", ""),
                    "last_activity_ms": at,
                    "activity_id": activity_id,
                    "unread": bool(
                        activity_id
                        and self._read.get((account, host_id, epoch, agent_id)) != activity_id
                    ),
                }
            )
        out.sort(key=metadata_order)
        return out

    def page(
        self,
        *,
        account: str,
        host_id: str,
        snapshot: dict[str, Any],
        events: list[dict[str, Any]],
        secret: bytes,
        limit: int,
        token: str = "",
        project: str = "",
        status: str = "",
        query: str = "",
    ) -> dict[str, Any]:
        if type(limit) is not int or not 1 <= limit <= 200 or len(token) > 512:
            raise BackendError("forbidden", "invalid inventory page bound")
        rows = self.rows(account, host_id, snapshot, events, secret)
        epoch = str(snapshot["host"]["epoch"])
        if len(project) > 128 or len(query) > 256 or (status and status not in STATUSES):
            raise BackendError("forbidden", "invalid inventory filter")
        rows = [
            row
            for row in rows
            if (not project or row["workspace_id"] == project)
            and (not status or row["status"] == status)
            and (
                not query
                or any(
                    query.casefold() in row[key].casefold()
                    for key in ("name", "agent", "title", "project")
                )
            )
        ]
        scope = _hash(["console/1", account, host_id, epoch, project, status, query, limit])
        # Read state does not change ordering; reading a row must not
        # invalidate an otherwise coherent next-page cursor.
        version = _hash(
            [{key: value for key, value in row.items() if key != "unread"} for row in rows]
        )
        offset = 0
        if token:
            try:
                cursor = decode_cursor(secret, token)
            except CursorError:
                raise BackendError("stale_epoch", "page token invalid", True) from None
            offset = cursor.get("offset", -1)
            if (
                cursor.get("scope") != scope
                or cursor.get("view") != version
                or type(offset) is not int
                or not 0 <= offset <= len(rows)
            ):
                raise BackendError("stale_epoch", "page context changed; refresh inventory", True)
        page = rows[offset : offset + limit]
        out: dict[str, Any] = {
            "host_id": host_id,
            "host_epoch": epoch,
            "agents": page,
            "total": len(rows),
        }
        if offset + len(page) < len(rows):
            out["next_page_token"] = encode_cursor(
                secret, {"scope": scope, "view": version, "offset": offset + len(page)}
            )
        return out

    def mark_read(
        self,
        *,
        account: str,
        host_id: str,
        epoch: str,
        agent_id: str,
        activity_id: str,
        snapshot: dict[str, Any],
        events: list[dict[str, Any]],
        secret: bytes,
    ) -> dict[str, Any]:
        if snapshot["host"]["epoch"] != epoch:
            raise BackendError("stale_epoch", "host changed", True)
        row = next(
            (
                row
                for row in self.rows(account, host_id, snapshot, events, secret)
                if row["terminal_id"] == agent_id
            ),
            None,
        )
        if row is None:
            raise BackendError("not_found", "agent unavailable")
        if row["activity_id"] != activity_id:
            raise BackendError(
                "stale_revision", "activity changed; refresh before marking read", True
            )
        key = (account, host_id, epoch, agent_id)
        self._read[key] = activity_id
        self._read.move_to_end(key)
        while len(self._read) > MAX_READ_MARKS:
            self._read.popitem(last=False)
        return {
            "host_id": host_id,
            "host_epoch": epoch,
            "agent_id": agent_id,
            "activity_id": activity_id,
            "unread": False,
        }
