"""Snapshot/agent-lease metadata continuity without listening sockets."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx

from relaydeck.client.features.sync.controller import SyncController
from relaydeck.client.features.sync.transport import HttpxGatewayTransport
from relaydeck.connector.fixture import fixture_event
from relaydeck.domain.models import ConnectionState
from relaydeck.gateway.app import create_app
from relaydeck.gateway.cursors import decode_cursor
from tests.security.helpers import SECRET, auth, mint_token
from tests.support.sync.transport_fixtures import _state


def test_agent_direct_attach_preserves_nonzero_snapshot_boundary(tmp_path: Path) -> None:
    async def scenario() -> None:
        state, backend, source = _state(tmp_path)
        for number in (1, 2, 3):
            event = fixture_event("agent.upserted", f"before-{number}", resource_id="term-1")
            event["host_epoch"] = source.epoch
            assert "ok" in backend.ingest_event("home-mac", event, state.now_ms(), SECRET)
        assert backend.ingest_snapshot(
            "home-mac", source.snapshot_document("home-mac"), state.now_ms(), SECRET
        ) == {"ok": True}
        token = mint_token(state)
        app = create_app(state)
        sequences: list[int] = []

        # ASGITransport buffers a response. End this test's real follower
        # after delivery; replay/queue, handler, parser and client stay production.
        follow = backend.follow

        async def finite(host: str, after: int, heartbeat: float = 15.0) -> AsyncIterator[Any]:
            stream = follow(host, after, heartbeat)
            try:
                async for event in stream:
                    if event is not None:
                        sequences.append(event["sequence"])
                    yield event
                    if event is not None and event["sequence"] >= 4:
                        return
            finally:
                await stream.aclose()

        backend.follow = finite  # type: ignore[method-assign]
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://synthetic",
            headers=auth(token),
        ) as http:

            async def access() -> str:
                return token

            class AttachAfterSnapshot(HttpxGatewayTransport):
                async def snapshot(self, host_id: str) -> dict[str, Any]:
                    raw = await super().snapshot(host_id)
                    assert decode_cursor(SECRET, raw["cursor"])["sequence"] == 3
                    attached = await http.post(
                        "/v1/hosts/home-mac/terminals/term-1/attachments",
                        json={"kind": "agent", "takeover": True},
                    )
                    assert attached.status_code == 201, attached.text
                    attachment = attached.json()
                    lease = await http.post(
                        f"/v1/terminal-attachments/{attachment['attachment_id']}/leases",
                        json={"mode": "control"},
                    )
                    assert lease.status_code == 201, lease.text
                    routes = {
                        route["terminal_id"]: route
                        for route in backend.epochs_for_host("home-mac", "acct-1")
                    }
                    assert source.note_epochs(routes) == {}
                    assert len(source.created) == 1
                    # Attachment/epoch bookkeeping consumes no metadata sequence.
                    assert backend.events_after("home-mac", 3) == []
                    event = fixture_event("agent.upserted", "after-attach", resource_id="term-1")
                    event["host_epoch"] = source.epoch
                    stored = backend.ingest_event("home-mac", event, state.now_ms(), SECRET)
                    assert stored["ok"]["sequence"] == 4
                    return raw

            transport = AttachAfterSnapshot("http://synthetic", access, http)
            controller = SyncController(transport)
            try:
                await controller._sync_once(
                    "home-mac", lambda: controller.state.connection.last_sequence == 4
                )
                assert sequences == [4]
                assert controller.state.connection.last_sequence == 4
                assert controller.state.connection.state is ConnectionState.LIVE
                assert not controller.state.stale and not controller.state.needs_resync
            finally:
                await source.close()
                state.push.close()
                backend.close()

    asyncio.run(scenario())
