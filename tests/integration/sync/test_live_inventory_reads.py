"""Existing upstream reads once per metadata batch; no socket or invented RPC."""

from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
from typing import Any

import pytest

from relaydeck.connector.herdr import HerdrAdapter, HerdrError
from relaydeck.connector.link import ConnectorLink, LinkConfig
from relaydeck.contracts.schemas import validate
from tests.support.sync.scripted_transport import _ScriptedTransport
from tests.support.sync.transport_fixtures import RecordingSource


def test_metadata_batch_publishes_refreshed_inventory_before_notification(tmp_path: Path) -> None:
    async def scenario() -> None:
        source = RecordingSource()
        calls: list[str] = []
        fresh = copy.deepcopy(source._snapshot)
        agents = copy.deepcopy(source._agents)
        agents[0]["name"] = "renamed"

        class Reads(HerdrAdapter):
            async def session_snapshot(self) -> dict[str, Any]:
                calls.append("session.snapshot")
                return {"snapshot": fresh}

            async def agent_list(self) -> list[dict[str, Any]]:
                calls.append("agent.list")
                return agents

        source._adapter = Reads()
        epoch = source.snapshot_document("home-mac")["host"]["epoch"]
        event = {
            "event_id": "observed-001",
            "host_epoch": epoch,
            "type": "pane.upserted",
            "resource_id": "p1",
            "observed_at": "2026-10-04T10:00:00Z",
            "payload": {"agent_status": "working"},
        }
        batches = [[event], [], [event]]

        async def collect_live(timeout: float) -> list[dict[str, Any]]:
            return batches.pop(0)

        source.collect_live = collect_live  # type: ignore[method-assign]
        wire = _ScriptedTransport([])
        link = ConnectorLink(
            LinkConfig(
                "http://synthetic",
                "home-mac",
                "synthetic-token",
                journal_path=str(tmp_path / "connector.db"),
            ),
            source,
        )
        try:
            assert await link._push_live(wire)
            frames = [json.loads(value) for value in wire.sent]
            assert [frame["kind"] for frame in frames] == ["snapshot", "event"]
            validate("relaydeck/gateway/v1/snapshot", frames[0]["snapshot"])
            assert frames[0]["snapshot"]["host"]["epoch"] == epoch
            assert frames[0]["snapshot"]["agents"][0]["name"] == "renamed"
            assert frames[1]["event"] == event
            assert calls == ["session.snapshot", "agent.list"]
            assert await link._push_live(wire)
            assert len(wire.sent) == 2 and len(calls) == 2
            before = copy.deepcopy(source._snapshot)

            class FailedReads(Reads):
                async def session_snapshot(self) -> dict[str, Any]:
                    return {"snapshot": {"workspaces": []}}

                async def agent_list(self) -> list[dict[str, Any]]:
                    raise HerdrError("upstream_closed", "synthetic failed inventory read")

            source._adapter = FailedReads()
            with pytest.raises(HerdrError):
                await link._push_live(wire)
            assert source._snapshot == before and len(wire.sent) == 2
            assert link._source_stale  # Recovery must rebootstrap, not publish mixed reads.
        finally:
            link._journal.close()

    asyncio.run(scenario())
