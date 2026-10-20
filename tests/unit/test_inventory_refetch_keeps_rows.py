"""A refetch for other filters keeps the shown rows until the new ones replace them."""

from __future__ import annotations

import asyncio
from typing import Any

from relaydeck.client.features.inventory.paging import InventoryFilters, InventoryPager
from tests.support.host_harness import run


class _Transport:
    def __init__(self) -> None:
        self.gate: asyncio.Event | None = None

    async def agents(self, host_id: str, limit: int, token: str = "", **filters: Any) -> Any:
        if self.gate is not None:
            await self.gate.wait()
        names = ["b", "d"] if filters.get("project") else ["a", "b", "c", "d"]
        return {
            "host_id": host_id,
            "host_epoch": "e1",
            "total": len(names),
            "next_page_token": None,
            "agents": [
                {"terminal_id": f"t-{name}", "name": name, "status": "done"} for name in names
            ],
        }


def _shown(pager: InventoryPager) -> list[str]:
    return sorted(row["terminal_id"] for row in pager.visible_rows)


def test_rows_stay_until_the_refetch_replaces_them() -> None:
    async def scenario() -> None:
        transport = _Transport()
        pager = InventoryPager(transport, lambda: None)
        await pager.refresh("acct", {"h1": "e1"}, InventoryFilters())
        assert _shown(pager) == ["t-a", "t-b", "t-c", "t-d"]
        transport.gate = asyncio.Event()
        task = asyncio.create_task(
            pager.refresh("acct", {"h1": "e1"}, InventoryFilters(project_id="w1"))
        )
        await asyncio.sleep(0)
        assert pager.busy and _shown(pager) == ["t-a", "t-b", "t-c", "t-d"]  # never blank
        transport.gate.set()
        await task
        assert _shown(pager) == ["t-b", "t-d"]

    run(scenario())


def test_another_account_never_sees_the_previous_rows() -> None:
    async def scenario() -> None:
        transport = _Transport()
        pager = InventoryPager(transport, lambda: None)
        await pager.refresh("acct", {"h1": "e1"}, InventoryFilters())
        transport.gate = asyncio.Event()
        task = asyncio.create_task(pager.refresh("other", {"h1": "e1"}, InventoryFilters()))
        await asyncio.sleep(0)
        assert _shown(pager) == []
        transport.gate.set()
        await task
        assert pager.account == "other" and len(_shown(pager)) == 4

    run(scenario())
