"""Account-bound metadata paging with cancellation/generation fences."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from relaydeck.client.features.sync.transport import HttpxGatewayTransport, TransportError
from relaydeck.domain.agent_order import metadata_order, status_rank
from relaydeck.domain.models import scope_key

PAGE_SIZE = 50
MAX_ROWS = 4096
MAX_HOSTS = 128


@dataclass(frozen=True)
class InventoryFilters:
    host_id: str = ""
    project_host: str = ""
    project_id: str = ""
    status: str = ""
    query: str = ""


class InventoryPager:
    def __init__(self, transport: HttpxGatewayTransport, changed: Callable[[], None]) -> None:
        self.transport = transport
        self.changed = changed
        self.rows: dict[str, dict[str, Any]] = {}
        self.pages: dict[str, dict[str, Any]] = {}
        self.filters = InventoryFilters()
        self.hosts: dict[str, str] = {}
        self.account = ""
        self.busy = False
        self.error = ""
        self._generation = 0

    @property
    def more(self) -> bool:
        return len(self.rows) < MAX_ROWS and any(
            page.get("next_page_token") for page in self.pages.values()
        )

    @property
    def total(self) -> int:
        return sum(int(page.get("total", len(page["agents"]))) for page in self.pages.values())

    def _frontiers(self) -> dict[str, int]:
        return {
            host: status_rank(page["agents"][-1]["status"])
            for host, page in self.pages.items()
            if page.get("next_page_token") and page["agents"]
        }

    @property
    def visible_rows(self) -> tuple[dict[str, Any], ...]:
        """Withhold lower groups until every host has passed their frontier.

        Sorting loaded rows alone can put Working ahead of Blocked on an unseen
        host page. Each gateway page is status ordered; its last status bounds
        what remains unseen. Within a group display order stays deterministic.
        """
        if set(self.hosts) - self.pages.keys():
            return ()  # an unobserved host could contain a higher-priority group
        boundary = min(self._frontiers().values(), default=4)
        return tuple(row for row in self.rows.values() if status_rank(row["status"]) <= boundary)

    def invalidate(self) -> None:
        self._generation += 1
        self.busy = False

    def clear(self) -> None:
        self.invalidate()
        self.rows.clear()
        self.pages.clear()
        self.hosts.clear()
        self.account = ""
        self.error = ""

    def _current(self, generation: int) -> bool:
        return generation == self._generation

    def _check(
        self, host_id: str, doc: dict[str, Any], hosts: Mapping[str, str] | None = None
    ) -> None:
        expected = (self.hosts if hosts is None else hosts).get(host_id)
        if doc.get("host_id") != host_id or doc.get("host_epoch") != expected:
            raise TransportError("stale_epoch", "inventory host context changed", True)

    def _merge(self, host_id: str, doc: dict[str, Any]) -> None:
        self._check(host_id, doc)
        for row in doc["agents"]:
            key = scope_key(host_id, row["terminal_id"])
            self.rows[key] = {**row, "host_id": host_id, "host_epoch": doc["host_epoch"]}
        if len(self.rows) > MAX_ROWS:
            # A later host/page can contain earlier status groups. Preserve the
            # global prefix at capacity rather than whichever host arrived first.
            self.rows = dict(
                sorted(self.rows.items(), key=lambda item: (*metadata_order(item[1])[:2], item[0]))[
                    :MAX_ROWS
                ]
            )
            self.error = "Inventory limit reached. Narrow the filters."
        self.pages[host_id] = doc
        if len(self.rows) >= MAX_ROWS and any(
            page.get("next_page_token") for page in self.pages.values()
        ):
            self.error = "Inventory limit reached. Narrow the filters."

    async def refresh(
        self, account: str, hosts: Mapping[str, str], filters: InventoryFilters
    ) -> None:
        selected = {
            host: epoch
            for host, epoch in hosts.items()
            if (not filters.host_id or host == filters.host_id)
            and (not filters.project_host or host == filters.project_host)
        }
        context_changed = (self.account, self.hosts, self.filters) != (account, selected, filters)
        if account != self.account:
            self.clear()  # another account's agents are never shown, even for a moment
        self.invalidate()
        generation = self._generation
        # Other filters or hosts keep the current rows on screen until the new ones replace them.
        self.account, self.filters = account, filters
        self.busy, self.error = True, ""
        self.changed()
        if len(selected) > MAX_HOSTS:
            self.hosts = selected
            self.rows.clear()
            self.pages.clear()
            self.busy = False
            self.error = "Inventory host limit reached. Select one host."
            self.changed()
            return
        fresh: dict[str, dict[str, Any]] = {}
        errors: list[str] = []
        semaphore = asyncio.Semaphore(3)

        async def fetch(host_id: str) -> None:
            async with semaphore:
                try:
                    doc = await self.transport.agents(
                        host_id,
                        PAGE_SIZE,
                        project=filters.project_id,
                        status=filters.status,
                        query=filters.query,
                    )
                    if self._current(generation):
                        self._check(host_id, doc, selected)
                        fresh[host_id] = doc
                except TransportError as exc:
                    errors.append(exc.code)

        try:
            await asyncio.gather(*(fetch(host_id) for host_id in selected))
            if not self._current(generation):
                return
            if context_changed:
                self.rows, self.pages = {}, {}
            self.hosts = selected
            # A failed host keeps only same-context cached metadata, explicitly stale.
            for host_id in fresh:
                self.rows = {
                    key: row for key, row in self.rows.items() if row["host_id"] != host_id
                }
                self.pages.pop(host_id, None)
                self._merge(host_id, fresh[host_id])
            if errors:
                self.error = f"Inventory refresh failed ({errors[0]}). Showing cached metadata."
        finally:
            if self._current(generation):
                self.busy = False
                self.changed()

    async def load_more(self) -> None:
        if self.busy or not self.more:
            return
        generation = self._generation
        filters = self.filters
        self.busy = True
        self.error = ""
        self.changed()
        try:
            frontiers = self._frontiers()
            boundary = min(frontiers.values(), default=4)
            for host_id, page in list(self.pages.items()):
                token = page.get("next_page_token", "")
                if not token or frontiers.get(host_id) != boundary:
                    continue
                doc = await self.transport.agents(
                    host_id,
                    PAGE_SIZE,
                    token,
                    project=filters.project_id,
                    status=filters.status,
                    query=filters.query,
                )
                if not self._current(generation):
                    return
                self._merge(host_id, doc)
                if len(self.rows) >= MAX_ROWS:
                    break
        except TransportError as exc:
            if self._current(generation):
                self.error = f"Inventory changed ({exc.code}). Refresh with the same filters."
        finally:
            if self._current(generation):
                self.busy = False
                self.changed()

    async def mark_read(self, key: str) -> None:
        generation = self._generation
        row = self.rows.get(key)
        if row is None or not row.get("unread"):
            return
        try:
            await self.transport.mark_agent_read(
                row["host_id"],
                row["terminal_id"],
                row["host_epoch"],
                row["activity_id"],
            )
            current = self.rows.get(key)
            if (
                self._current(generation)
                and current is not None
                and current.get("activity_id") == row["activity_id"]
            ):
                self.rows[key] = {**current, "unread": False}
                self.changed()
        except TransportError:
            pass  # Read failure cannot erase the unread mark or authorize an input.
