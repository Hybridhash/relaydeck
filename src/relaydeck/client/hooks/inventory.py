"""Inventory hook of `Root`.

Plain functions called from `Root` at the position the inline code used to occupy, so hook
order and the control tree are unchanged. Each takes only what it reads (keyword-only) and
returns the names the rest of `Root` still uses.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable
from typing import Any

import flet as ft

from relaydeck.client.features.inventory.paging import InventoryFilters, InventoryPager
from relaydeck.client.features.sync.controller import SyncController
from relaydeck.client.features.sync.transport import HttpxGatewayTransport

# design tokens (minimal first theme, centrally configurable).
from relaydeck.domain.models import ConnectionState


def use_inventory(
    *,
    account: str,
    app_state: Any,
    controller: SyncController,
    foreground: bool,
    transport: HttpxGatewayTransport,
) -> tuple[Any, ...]:
    """Paged agent inventory: pager, supervised tasks, refresh/cancel/release effects."""
    inventory_tick, set_inventory_tick = ft.use_state(0)

    inventory_account: Any = ft.use_ref(account)

    inventory_account.current = account

    inventory_alive: Any = ft.use_ref(True)

    inventory_filters, set_inventory_filters = ft.use_state(InventoryFilters())

    pager_box: Any = ft.use_ref(None)

    inventory_tasks: Any = ft.use_ref(set())

    if pager_box.current is None and callable(getattr(transport, "agents", None)):
        pager_box.current = InventoryPager(
            transport, lambda: set_inventory_tick(time.monotonic_ns())
        )

    pager: InventoryPager | None = pager_box.current

    inventory_hosts = {
        host.id: host.epoch
        for host in app_state.inventory.hosts.values()
        if not app_state.revoked
        and (app_state.granted_hosts is None or host.id in app_state.granted_hosts)
    }

    def inventory_task(coro: Awaitable[Any]) -> None:
        task = asyncio.ensure_future(coro)
        inventory_tasks.current.add(task)

        def finished(done: asyncio.Future[Any]) -> None:
            inventory_tasks.current.discard(done)
            if (
                not done.cancelled()
                and done.exception() is not None
                and inventory_alive.current
                and inventory_account.current == account
                and pager_box.current is not None
                and pager_box.current.filters == inventory_filters
            ):
                pager_box.current.error = "Inventory request failed. Refresh to retry."
                pager_box.current.changed()

        task.add_done_callback(finished)

    def cancel_inventory() -> None:
        if pager is not None:
            pager.invalidate()
        for task in tuple(inventory_tasks.current):
            task.cancel()
        inventory_tasks.current.clear()

    def refresh_inventory() -> None:
        if (
            not foreground
            or pager is None
            or not inventory_alive.current
            or inventory_account.current != account
        ):
            return
        if controller.state.revoked:
            pager.clear()
            set_inventory_tick(time.monotonic_ns())
            return
        inventory_task(pager.refresh(account, inventory_hosts, inventory_filters))

    ft.use_effect(
        refresh_inventory,
        dependencies=[
            account,
            foreground,
            tuple(sorted(inventory_hosts.items())),
            inventory_filters,
            app_state.connection.last_sequence,
            app_state.connection.state == ConnectionState.LIVE,
            app_state.revoked,
        ],
        cleanup=cancel_inventory,
    )

    def release_inventory() -> None:
        inventory_alive.current = False
        cancel_inventory()
        if pager is not None:
            pager.clear()

    def hold_inventory() -> None:
        inventory_alive.current = True

    ft.use_effect(hold_inventory, dependencies=[], cleanup=release_inventory)

    def more_inventory() -> None:
        if pager is not None and inventory_alive.current and inventory_account.current == account:
            inventory_task(pager.load_more())

    return (
        inventory_account,
        inventory_alive,
        inventory_filters,
        inventory_hosts,
        inventory_task,
        inventory_tick,
        more_inventory,
        pager,
        refresh_inventory,
        release_inventory,
        set_inventory_filters,
    )
