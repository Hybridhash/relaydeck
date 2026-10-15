"""Metadata pages/read marks through actual HTTP and shared typed adapter."""

from __future__ import annotations

import asyncio
import copy
from typing import Any

import httpx
import pytest

from relaydeck.client.features.sync.transport import HttpxGatewayTransport, TransportError
from relaydeck.contracts.schemas import validate
from relaydeck.gateway.app import create_app
from tests.security.helpers import NOW, SECRET, make_state, mint_token
from tests.support.sync.inventory_metadata import _metadata


def test_metadata_activity_and_read_marks_are_account_host_epoch_scoped() -> None:
    async def scenario() -> None:
        state, backend = make_state()
        twin = copy.deepcopy(backend.snapshots["home-mac"])
        twin["host"]["id"] = "other-mac"
        for kind in ("agents", "workspaces", "tabs", "panes"):
            for item in twin[kind]:
                item["host_id"] = "other-mac"
        backend.snapshots["other-mac"] = twin
        tokens = {
            account: mint_token(state, account=account, hosts=frozenset(backend.snapshots))
            for account in ("acct-1", "acct-2")
        }
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(state)), base_url="http://gw"
        ) as http:

            async def token_one() -> str:
                return tokens["acct-1"]

            async def token_two() -> str:
                return tokens["acct-2"]

            one = HttpxGatewayTransport("http://gw", token_one, http)
            two = HttpxGatewayTransport("http://gw", token_two, http)
            row = (await one.agents("home-mac"))["agents"][0]
            assert row["last_activity_ms"] == 0 and not row["unread"] and row["activity_id"] == ""
            stored = _metadata(backend)
            assert stored.get("ok")
            row = (await one.agents("home-mac"))["agents"][0]
            assert row["status"] == "working" and row["unread"]
            assert row["last_activity_ms"] == 1791108000000
            assert (await two.agents("home-mac"))["agents"][0]["unread"]
            assert not (await one.agents("other-mac"))["agents"][0]["unread"]
            record = await one.mark_agent_read(
                "home-mac", row["terminal_id"], "epoch-7", row["activity_id"]
            )
            validate("relaydeck/gateway/v1/agent-read", record)
            assert not (await one.agents("home-mac"))["agents"][0]["unread"]
            assert (await two.agents("home-mac"))["agents"][0]["unread"]
            _metadata(backend)  # Exact event identity deduped, no new unread event.
            assert not (await one.agents("home-mac"))["agents"][0]["unread"]
            _metadata(backend, event_id="ev-activity-002")
            with pytest.raises(TransportError) as exc:
                await one.mark_agent_read(
                    "home-mac", row["terminal_id"], "epoch-7", row["activity_id"]
                )
            assert exc.value.code == "stale_revision"
            assert (await one.agents("home-mac"))["agents"][0]["unread"]
            backend.snapshots["home-mac"]["host"]["epoch"] = "epoch-8"
            reset = (await one.agents("home-mac"))["agents"][0]
            assert not reset["unread"] and reset["last_activity_ms"] == 0
            with pytest.raises(TransportError) as exc:
                await one.mark_agent_read(
                    "home-mac", row["terminal_id"], "epoch-7", row["activity_id"]
                )
            assert exc.value.code == "stale_epoch"
        state.push.close()

    asyncio.run(scenario())


def test_fresh_snapshot_is_not_rolled_back_by_retained_metadata() -> None:
    async def scenario() -> None:
        state, backend = make_state()
        token_value = mint_token(state)

        async def token() -> str:
            return token_value

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(state)), base_url="http://gw"
        ) as http:
            transport = HttpxGatewayTransport("http://gw", token, http)
            _metadata(backend)
            old = (await transport.agents("home-mac"))["agents"][0]
            assert old["status"] == "working"
            fresh = copy.deepcopy(backend.snapshots["home-mac"])
            fresh["agents"][0]["status"] = "done"
            fresh["agents"][0]["name"] = "renamed"
            assert backend.ingest_snapshot("home-mac", fresh, NOW, SECRET)["ok"]
            row = (await transport.agents("home-mac"))["agents"][0]
            assert row["status"] == "done" and row["name"] == "renamed"
            assert row["activity_id"] == old["activity_id"] and row["unread"]
            # A later real metadata notification can attribute a meaningful
            # fresh-snapshot change; focus-only/byte output cannot do so.
            fresh["agents"][0]["name"] = "renamed-again"
            assert backend.ingest_snapshot("home-mac", fresh, NOW, SECRET)["ok"]
            assert backend.ingest_event(
                "home-mac",
                {
                    "event_id": "rename-notice",
                    "host_epoch": "epoch-7",
                    "type": "pane.upserted",
                    "resource_id": fresh["agents"][0]["pane_id"],
                    "observed_at": "2026-10-04T10:00:01Z",
                },
                NOW,
                SECRET,
            )["ok"]
            changed = (await transport.agents("home-mac"))["agents"][0]
            assert (
                changed["name"] == "renamed-again" and changed["activity_id"] != old["activity_id"]
            )
            assert changed["last_activity_ms"] == 1791108001000
        state.push.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "change", ["account", "host", "epoch", "query", "project", "status", "limit", "revision"]
)
def test_filtered_page_cursor_cannot_cross_context_or_changed_view(change: str) -> None:
    async def scenario() -> None:
        state, backend = make_state()
        agents = backend.snapshots["home-mac"]["agents"]
        agents[0]["name"] = "demo"
        agents[0]["status"] = "blocked"
        for n in range(1, 7):
            agents.append({**agents[0], "terminal_id": f"term-{n + 1}", "name": f"demo-{n}"})
        twin = copy.deepcopy(backend.snapshots["home-mac"])
        twin["host"]["id"] = "other-mac"
        for kind in ("agents", "workspaces", "tabs", "panes"):
            for item in twin[kind]:
                item["host_id"] = "other-mac"
        backend.snapshots["other-mac"] = twin
        current = mint_token(state, hosts=frozenset(backend.snapshots))

        async def token() -> str:
            return current

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(state)), base_url="http://gw"
        ) as http:
            transport = HttpxGatewayTransport("http://gw", token, http)
            page = await transport.agents(
                "home-mac",
                limit=2,
                project=agents[0]["workspace_id"],
                query="demo",
                status="blocked",
            )
            assert page["total"] == 7 and len(page["agents"]) == 2
            validate("relaydeck/gateway/v1/agents-page", page)
            kwargs: dict[str, Any] = dict(
                limit=2,
                page_token=page["next_page_token"],
                project=agents[0]["workspace_id"],
                query="demo",
                status="blocked",
            )
            host = "home-mac"
            if change == "account":
                current = mint_token(state, account="acct-2", hosts=frozenset(backend.snapshots))
            elif change == "host":
                host = "other-mac"
            elif change == "epoch":
                backend.snapshots[host]["host"]["epoch"] = "epoch-8"
            elif change == "revision":
                agents[0]["revision"] += 1
            else:
                kwargs[change] = {
                    "query": "other",
                    "project": "other",
                    "status": "idle",
                    "limit": 3,
                }[change]
            with pytest.raises(TransportError) as exc:
                await transport.agents(host, **kwargs)
            assert exc.value.code == "stale_epoch"
        state.push.close()

    asyncio.run(scenario())


def test_paging_and_read_boundaries_refuse_unauthorized_or_malformed_requests() -> None:
    async def scenario() -> None:
        state, backend = make_state()
        token = mint_token(state)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(state)), base_url="http://gw"
        ) as http:
            headers = {"Authorization": f"Bearer {token}"}
            agent = backend.snapshots["home-mac"]["agents"][0]
            path = f"/v1/hosts/home-mac/agents/{agent['terminal_id']}/read"
            assert (
                await http.post(path, json={"host_epoch": "epoch-7", "activity_id": ""})
            ).status_code == 401
            for body in (
                {"host_epoch": "epoch-7", "activity_id": "", "account": "acct-2"},
                {"host_epoch": True, "activity_id": ""},
                {"host_epoch": "epoch-7", "activity_id": "short"},
            ):
                assert (await http.post(path, headers=headers, json=body)).status_code == 400
            assert (await http.post(path, headers=headers, content="{")).status_code == 400
            assert (await http.get("/v1/hosts/foreign/agents", headers=headers)).status_code == 404
            for query in ("status=made-up", "execute=bad", "q=" + "a" * 257):
                assert (
                    await http.get("/v1/hosts/home-mac/agents?" + query, headers=headers)
                ).status_code == 400
        state.push.close()

    asyncio.run(scenario())
