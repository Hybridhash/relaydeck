"""Shared mapping-fixture test helpers (moved out of test modules)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from relaydeck.connector.herdr import BootstrapResult
from relaydeck.connector.source import HerdrSource
from relaydeck.usage.contracts import UsageSource
from tests.support.usage.store import NOW


def pane(space="space-a", *, session="session-private-marker"):
    return {
        "pane_id": "p1",
        "workspace_id": space,
        "tab_id": "t1",
        "agent_session": (
            {"agent": "kind-a", "kind": "id", "source": "reported", "value": session}
            if session
            else None
        ),
    }


class Adapter:
    def __init__(self):
        self.document = json.loads(
            Path("tests/fixtures/upstream/session_snapshot.json").read_text()
        )["result"]
        self.document["snapshot"]["panes"][0].update(pane())

    async def bootstrap(self, subscriptions):
        return BootstrapResult(self.document, [], [], None)

    async def session_snapshot(self):
        return self.document

    async def agent_list(self):
        return []


def source_fixture(tmp_path):
    now = [NOW]
    adapter = Adapter()
    source = HerdrSource(adapter, clock=lambda: now[0])
    source.configure_usage((UsageSource("h1", "kind-a", "format-a", tmp_path.resolve()),))

    async def no_sub():
        pass

    source._ensure_pane_subscriptions = no_sub
    asyncio.run(source.bootstrap())
    return source, adapter, now
