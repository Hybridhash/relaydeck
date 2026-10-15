"""Inventory metadata documents."""

from __future__ import annotations

from typing import Any

from tests.security.helpers import NOW, SECRET


def _metadata(backend: Any, host_id: str = "home-mac", event_id: str = "ev-activity-001") -> Any:
    agent = backend.snapshots[host_id]["agents"][0]
    return backend.ingest_event(
        host_id,
        {
            "event_id": event_id,
            "host_epoch": "epoch-7",
            "type": "pane.upserted",
            "resource_id": agent["pane_id"],
            "observed_at": "2026-10-04T10:00:00Z",
            "payload": {"agent_status": "working"},
        },
        NOW,
        SECRET,
    )
