"""Terminal ownership routes: publish each host's terminal epochs and release leases."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from relaydeck.gateway.app import GatewayState


async def release_lease_and_close(
    state: GatewayState, lease_id: str, account: str, device_id: str
) -> None:
    """Detach one lease (the attachment and remote process survive) and
    sever its sockets. Holder-only: unknown, foreign, or other-device
    leases are BackendError not_found (unlike attachment close, which
    is account-scoped)."""
    attachment = state.backend.release_lease(lease_id, account, device_id)
    await state.sockets.close_lease(lease_id, "detached")
    await publish_terminal_routes(state, attachment.host_id)


async def publish_terminal_routes(state: GatewayState, host_id: str) -> int:
    """Gateway-only ownership mode: observer streams cannot hold Mac sizing."""
    record = state.hosts.record_for_host(host_id)
    if record is not None:
        return await state.sockets.notify_host(
            host_id,
            json.dumps(
                {
                    "kind": "terminal.epochs",
                    "host_id": host_id,
                    "epochs": state.backend.epochs_for_host(host_id, record.account),
                }
            ),
            binding=record,
        )
    return 0


async def reconcile_terminal_routes(state: GatewayState) -> None:
    """Expiry also tears down upstream ownership with no live browser socket."""
    previous: dict[str, list[dict[str, str]]] = {}
    while True:
        for record in state.hosts.hosts():
            routes = state.backend.epochs_for_host(record.host_id, record.account)
            if previous.get(record.host_id) != routes:
                notified = await publish_terminal_routes(state, record.host_id)
                if notified >= state.sockets.host_count(record.host_id, record):
                    previous[record.host_id] = routes
        await asyncio.sleep(1)
