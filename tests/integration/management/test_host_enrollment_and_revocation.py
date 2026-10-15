"""Durable, account-bound enrollment and serving host revocation."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx

from relaydeck.client.features.sync.transport import HttpxGatewayTransport
from relaydeck.connector.pubkey import fingerprint_of_raw, generate, openssh_to_raw
from relaydeck.gateway.app import create_app
from relaydeck.gateway.hosts import HostStore
from tests.security.helpers import ALL_CAPS, NOW, make_state
from tests.support.companion.enrollment import _redeem_body
from tests.support.gateway.fake_socket import _FakeSocket


def test_owned_host_revoke_closes_access_persists_and_preserves_other_host(tmp_path: Path) -> None:
    async def scenario() -> None:
        state, backend = make_state(hosts=("home-mac", "other-mac"))
        state.hosts = HostStore(path=tmp_path / "hosts.json")
        _, public = generate()
        fingerprint = fingerprint_of_raw(openssh_to_raw(public))
        host_token = state.hosts.enroll("home-mac", "Home", "acct-1", public, fingerprint)
        foreign_token = state.hosts.enroll("other-mac", "Other", "acct-2")
        owner, refresh = state.sessions.create("acct-1", ALL_CAPS, frozenset({"home-mac"}), NOW)
        foreign, _ = state.sessions.create("acct-2", ALL_CAPS, frozenset({"other-mac"}), NOW)
        tunnel, foreign_tunnel = _FakeSocket(), _FakeSocket()
        state.sockets.register_host("home-mac", tunnel)
        state.sockets.register_host("other-mac", foreign_tunnel)
        key, signal = state.metadata.register("acct-1", "owner", "home-mac")

        async def token() -> str:
            return owner

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(state)), base_url="http://gw"
        ) as http:
            adapter = HttpxGatewayTransport("http://gw", token, http)
            page = await adapter.hosts()
            assert page["hosts"][0]["fingerprint"] == fingerprint
            response = await http.delete(
                "/v1/hosts/home-mac", headers={"Authorization": f"Bearer {foreign}"}
            )
            assert response.status_code == 404 and not signal.event.is_set()
            assert (await adapter.revoke_host("home-mac"))["ok"]
            assert (await adapter.hosts())["hosts"] == []
            assert signal.event.is_set() and signal.code == "not_found"
            assert tunnel.closes and not foreign_tunnel.closes
            assert state.hosts.lookup(host_token) is None
            assert state.hosts.lookup(foreign_token) is not None
            response = await http.get(
                "/v1/hosts/home-mac/snapshot", headers={"Authorization": f"Bearer {owner}"}
            )
            assert response.status_code == 404
            # Refresh and serving restart cannot resurrect a configured stale host grant.
            pair = state.sessions.refresh(refresh, NOW)
            assert pair is not None
            state.hosts = HostStore(path=tmp_path / "hosts.json")
            response = await http.get("/v1/hosts", headers={"Authorization": f"Bearer {pair[0]}"})
            assert response.json()["hosts"] == []
        state.metadata.unregister(key)
        state.push.close()

    asyncio.run(scenario())


def test_enrollment_rejects_foreign_active_and_revoked_ids_and_grants_new_owned_host() -> None:
    async def scenario() -> None:
        state, backend = make_state()
        seed, public = generate()
        foreign_token = state.hosts.enroll("foreign-mac", account="acct-2")
        owner, _ = state.sessions.create("acct-1", ALL_CAPS, frozenset(), NOW)

        async def token() -> str:
            return owner

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(state)), base_url="http://gw"
        ) as http:
            adapter = HttpxGatewayTransport("http://gw", token, http)
            invitation = await adapter.create_enrollment("Synthetic Home", public)
            assert (await adapter.enrollment_status(invitation["id"]))["status"] == "pending"

            async def redeem(host_id: str) -> httpx.Response:
                return await http.post(
                    f"/v1/enrollments/{invitation['id']}/redeem",
                    json=_redeem_body(seed, public, invitation["id"], invitation["code"], host_id),
                )

            assert (await redeem("foreign-mac")).status_code == 404
            assert state.hosts.lookup(foreign_token) is not None
            state.hosts.revoke("foreign-mac")
            assert (await redeem("foreign-mac")).status_code == 404
            response = await redeem("new-mac")
            assert response.status_code == 200
            assert (await adapter.enrollment_status(invitation["id"]))["status"] == "claimed"
            host = (await adapter.hosts())["hosts"][0]
            assert host["host_id"] == "new-mac" and not host["reachable"]
            assert host["fingerprint"] == invitation["fingerprint"]
            assert (await redeem("another-mac")).status_code == 404
            assert (
                "new-mac"
                in (await http.get("/v1/me", headers={"Authorization": f"Bearer {owner}"})).json()[
                    "hosts"
                ]
            )
        state.push.close()

    asyncio.run(scenario())


def test_enrollment_status_is_redacted_owned_and_reenrollment_closes_previous_access() -> None:
    async def scenario() -> None:
        state, backend = make_state()
        owner, _ = state.sessions.create("acct-1", ALL_CAPS, frozenset({"home-mac"}), NOW)
        foreign, _ = state.sessions.create("acct-2", ALL_CAPS, frozenset(), NOW)
        old_token = state.hosts.enroll("home-mac", account="acct-1")
        tunnel, terminal = _FakeSocket(), _FakeSocket()
        state.sockets.register_host("home-mac", tunnel)
        attachment, _ = backend.create_attachment(
            "home-mac", "term-1", "shell", "acct-1", "old-device", takeover=False
        )
        lease = backend.acquire_control(attachment.attachment_id, "acct-1", "old-device")
        state.sockets.register("acct-1", lease.lease_id, terminal, "old-device")
        seed, public = generate()

        async def token() -> str:
            return owner

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(state)), base_url="http://gw"
        ) as http:
            adapter = HttpxGatewayTransport("http://gw", token, http)
            invitation = await adapter.create_enrollment("Home", public)
            path = f"/v1/enrollments/{invitation['id']}"
            response = await http.get(path, headers={"Authorization": f"Bearer {foreign}"})
            assert response.status_code == 404
            response = await http.get(path)
            assert response.status_code == 401
            status = await adapter.enrollment_status(invitation["id"])
            assert set(status) == {"id", "status"} and status["status"] == "pending"
            response = await http.post(
                path + "/redeem",
                json=_redeem_body(seed, public, invitation["id"], invitation["code"], "home-mac"),
            )
            assert response.status_code == 200
            assert state.hosts.lookup(old_token) is None
            assert tunnel.closes and terminal.closes
            assert backend.open_lease(lease.lease_id) is None
            assert (await adapter.enrollment_status(invitation["id"]))["status"] == "claimed"
            second = await adapter.create_enrollment("Expired Home", public)
            state.now_ms = lambda: NOW + 600_001
            assert (await adapter.enrollment_status(second["id"]))["status"] == "expired"
        state.push.close()

    asyncio.run(scenario())
