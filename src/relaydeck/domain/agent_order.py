"""Console ordering shared by metadata pages and presentation selectors."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

CONSOLE_STATUSES = ("blocked", "working", "done", "idle", "unknown")


def status_rank(status: str) -> int:
    return CONSOLE_STATUSES.index(status) if status in CONSOLE_STATUSES else 4


def metadata_order(row: Mapping[str, Any]) -> tuple[int, str, str]:
    title = row.get("name") or row.get("title") or row["terminal_id"]
    return status_rank(row["status"]), title.casefold(), row["terminal_id"]
