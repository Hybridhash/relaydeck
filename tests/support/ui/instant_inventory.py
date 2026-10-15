"""Inventory navigation helpers for instant-open tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from relaydeck.contracts.schemas import validate
from tests.support.ui.retained_root import _active


async def valid_inventory(_transport: Any, _host: str) -> dict[str, Any]:
    doc = json.loads(Path("tests/fixtures/gateway/snapshot.json").read_text())
    doc["tabs"][0]["id"], doc["tabs"][1]["id"] = "tab-2", "tab-1"
    doc["workspaces"][0]["active_tab_id"] = "tab-2"
    doc["panes"][0]["tab_id"], doc["panes"][1]["tab_id"] = "tab-2", "tab-1"
    doc["agents"][0]["tab_id"] = "tab-2"
    validate("relaydeck/gateway/v1/snapshot", doc)
    return doc


async def navigate(app: Any, destination: str, width: int) -> None:
    if width < 600:
        try:
            _active(app, "terminal-back")
        except KeyError:
            pass
        else:
            await app.set_props_and_fire("terminal-back", {}, "click")
    prefix = "console-nav" if width < 600 else "console-rail"
    await app.set_props_and_fire(f"{prefix}-{destination}", {}, "click")
