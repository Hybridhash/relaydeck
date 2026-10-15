"""Explicit synthetic snapshot pages for existing UI transport fixtures."""

from __future__ import annotations

from typing import Any

from relaydeck.contracts.schemas import validate
from relaydeck.gateway.inventory import InventoryView


def snapshot_page(
    snapshot: dict[str, Any], limit: int, token: str, project: str, status: str, query: str
) -> dict[str, Any]:
    doc = InventoryView().page(
        account="synthetic",
        host_id=snapshot["host"]["id"],
        snapshot=snapshot,
        events=[],
        secret=b"synthetic-inventory-fixture-only",
        limit=limit,
        token=token,
        project=project,
        status=status,
        query=query,
    )
    validate("relaydeck/gateway/v1/agents-page", doc)
    return doc
