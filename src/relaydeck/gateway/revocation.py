"""Revoke an account or a host and sever its live sockets and leases now."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from relaydeck.offload import settled

if TYPE_CHECKING:
    from relaydeck.gateway.app import GatewayState
    from relaydeck.gateway.identity import Session


async def revoke_account_and_close(state: GatewayState, account: str) -> int:
    """Revoke every session, invalidate every lease, and sever every live
    terminal socket now (logout/revoke kills writers)."""
    try:
        state.metrics.count_sign_in("revoked", state.sessions.revoke_account(account))
    finally:
        # The authorized scope survives failed durable I/O. Pending denial
        # blocks new credentials; existing writers must also stop now.
        state.metadata.revoke(account=account)
        state.backend.revoke_leases_account(account)
        state.inventory.drop_account(account)
        closed = await state.sockets.close_account(account, "unauthenticated")
    return closed


async def revoke_host_and_close(state: GatewayState, host_id: str) -> tuple[bool, int, int]:
    """Revoke a host token and sever its tunnels and leases now."""
    dead_sessions: tuple[Session, ...] = ()
    try:
        dead_sessions = state.sessions.revoke_paired_host(host_id)
        state.metrics.count_sign_in("revoked", len(dead_sessions))
        revoked = state.hosts.revoke(host_id)
    finally:
        state.metadata.revoke(host_id=host_id)
        # Fence leases before awaiting socket shutdown, even when there is
        # no durable result from which to enumerate paired devices.
        dead = state.backend.revoke_leases_host(host_id)
        for session in dead_sessions:
            state.metadata.revoke(account=session.account, device_id=session.device_id)
        state.inventory.drop_host(host_id)
        state.notifications.drop_host(host_id)

        async def finish_revocation() -> tuple[int, int]:
            for session in dead_sessions:
                await state.sockets.close_device(
                    session.account, session.device_id, "unauthenticated"
                )
            tunnels = await state.sockets.close_host(host_id, "unauthenticated")
            leases = 0
            for lease_id in dead:
                leases += await state.sockets.close_lease(lease_id, "lease_expired")
            return tunnels, leases

        tunnels, leases = await settled(asyncio.create_task(finish_revocation()))
    return revoked, tunnels, leases
