"""attach/lease/renew transport seam against the in-process gateway.

HttpxGatewayTransport drives the shipped attach routes (FakeBackend):
agent attach with explicit takeover, shell first-without-takeover plus
409 holder conflict, control lease issue/renew, and ticket minting.
"""

from __future__ import annotations

import asyncio

import httpx

from relaydeck.client.features.sync.transport import HttpxGatewayTransport, TransportError
from relaydeck.gateway.app import GatewayState, create_app
from relaydeck.gateway.backend import FakeBackend

NOW = 2_000_000
CAPS = frozenset(
    {
        "sessions.read",
        "terminal.view",
        "terminal.control",
        "terminal.attach",
    }
)


def _state() -> GatewayState:
    return GatewayState(
        backend=FakeBackend(now_ms=lambda: NOW),
        cursor_secret=b"y" * 32,
        now_ms=lambda: NOW,
    )


def _transport(state: GatewayState, access: str) -> tuple[HttpxGatewayTransport, httpx.AsyncClient]:
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(state)), base_url="http://gw"
    )

    async def token() -> str:
        return access

    return HttpxGatewayTransport(base_url="http://gw", token=token, client=client), client


def _session(state: GatewayState, account: str, device: str) -> str:
    access, _ = state.sessions.create(account, CAPS, frozenset({"h1"}), NOW, device)
    return access


def test_agent_attach_takeover_lease_renew_ticket() -> None:
    async def scenario() -> None:
        state = _state()
        transport, client = _transport(state, _session(state, "a", "d1"))
        try:
            attachment = await transport.create_attachment(
                "h1", "term-1", kind="agent", takeover=True
            )
            assert attachment["attachment_id"].startswith("att-")
            assert attachment["generation"] == 1
            lease = await transport.create_lease(attachment["attachment_id"], mode="control")
            assert lease["lease_id"]
            renewed = await transport.renew_lease(lease["lease_id"])
            assert renewed["lease_id"] == lease["lease_id"]
            assert renewed["expires_at"]
            ticket = await transport.terminal_ticket(lease["lease_id"])
            assert ticket["ticket"]
        finally:
            await client.aclose()

    asyncio.run(scenario())


def test_shell_attach_conflict_then_takeover() -> None:
    async def scenario() -> None:
        state = _state()
        first_t, first_c = _transport(state, _session(state, "a", "d1"))
        second_t, second_c = _transport(state, _session(state, "a", "d2"))
        try:
            first = await first_t.create_attachment("h1", "term-9", kind="shell", takeover=False)
            assert first["attachment_id"]
            try:
                await second_t.create_attachment("h1", "term-9", kind="shell", takeover=False)
            except TransportError as exc:
                assert exc.code == "lease_conflict"
                assert exc.retryable is False
            else:
                raise AssertionError("expected lease_conflict for second holder")
            taken = await second_t.create_attachment("h1", "term-9", kind="shell", takeover=True)
            assert taken["generation"] == first["generation"] + 1
        finally:
            await first_c.aclose()
            await second_c.aclose()

    asyncio.run(scenario())


def test_unknown_host_and_foreign_renew_are_not_found() -> None:
    async def scenario() -> None:
        state = _state()
        transport, client = _transport(state, _session(state, "a", "d1"))
        other, other_client = _transport(state, _session(state, "b", "d9"))
        try:
            try:
                await transport.create_attachment("nope", "term-1", kind="agent", takeover=True)
            except TransportError as exc:
                assert exc.code == "not_found"
                assert exc.retryable is False
            else:
                raise AssertionError("expected not_found for unknown host")
            attachment = await transport.create_attachment(
                "h1", "term-2", kind="agent", takeover=True
            )
            lease = await transport.create_lease(attachment["attachment_id"], mode="control")
            try:
                await other.renew_lease(lease["lease_id"])
            except TransportError as exc:
                assert exc.code == "not_found"
            else:
                raise AssertionError("expected not_found for foreign renew")
        finally:
            await client.aclose()
            await other_client.aclose()

    asyncio.run(scenario())
