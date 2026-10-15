"""Owned device credentials, redacted lists and quiet-stream revocation."""

from __future__ import annotations

import asyncio
from collections.abc import MutableMapping
from typing import Any

import httpx
import pytest

from relaydeck.client.features.sync.transport import HttpxGatewayTransport, TransportError
from relaydeck.gateway.app import create_app
from tests.security.helpers import ALL_CAPS, NOW, make_state
from tests.support.gateway.fake_socket import _FakeSocket


def test_owned_device_list_revoke_credentials_refresh_push_and_terminal_connections() -> None:
    async def scenario() -> None:
        state, backend = make_state()
        own, _ = state.sessions.create("acct-1", ALL_CAPS, frozenset({"home-mac"}), NOW, "d1")
        target, refresh = state.sessions.create(
            "acct-1", ALL_CAPS, frozenset({"home-mac"}), NOW, "d2"
        )
        foreign, foreign_refresh = state.sessions.create(
            "acct-2", ALL_CAPS, frozenset({"home-mac"}), NOW, "d2"
        )
        state.push.register_device("acct-1", "d2", "synthetic-delivery-token", "android", NOW)
        state.push.register_device("acct-2", "d2", "foreign-delivery-token", "android", NOW)
        attachment, _ = backend.create_attachment(
            "home-mac", "term-1", "shell", "acct-1", "d2", takeover=False
        )
        lease = backend.acquire_control(attachment.attachment_id, "acct-1", "d2")
        socket, foreign_socket = _FakeSocket(), _FakeSocket()
        state.sockets.register("acct-1", lease.lease_id, socket, "d2")
        state.sockets.register("acct-2", "foreign-lease", foreign_socket, "d2")

        async def token() -> str:
            return own

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(state)), base_url="http://gw"
        ) as http:
            client = HttpxGatewayTransport("http://gw", token, http)
            page = await client.devices()
            assert page["current_device_id"] == "d1"
            assert {row["device_id"] for row in page["devices"]} == {"d1", "d2"}
            assert page["devices"][0]["current"]
            target_row = next(row for row in page["devices"] if row["device_id"] == "d2")
            assert target_row["platform"] == "android" and target_row["auth_sessions"] == 1
            assert all(
                secret not in str(page)
                for secret in (
                    own,
                    target,
                    refresh,
                    foreign,
                    "synthetic-delivery-token",
                    "foreign-delivery-token",
                )
            )
            assert (await client.revoke_device("d2"))["ok"]
            assert (
                state.sessions.lookup(target, NOW) is None
                and state.sessions.refresh(refresh, NOW) is None
            )
            assert not state.push.device_active("acct-1", "d2")
            assert socket.closes and socket.closes[-1][1] == "unauthenticated"
            assert foreign_socket.closes == [] and state.sessions.lookup(foreign, NOW) is not None
            assert state.push.device_active("acct-2", "d2")
            assert state.sessions.refresh(foreign_refresh, NOW) is not None
            assert (await client.devices())["devices"][0]["device_id"] == "d1"
            with pytest.raises(TransportError) as missing:
                await client.revoke_device("unknown")
            assert missing.value.code == "not_found"
            assert (await client.revoke_device("d1"))["ok"]
            with pytest.raises(TransportError) as logged_out:
                await client.devices()
            assert logged_out.value.code == "unauthenticated"
        state.push.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["device", "host"])
def test_revoke_quiet_metadata_stream_wakes_immediately_and_cleans_subscription(kind: str) -> None:
    async def scenario() -> None:
        state, backend = make_state()
        owner, _ = state.sessions.create("acct-1", ALL_CAPS, frozenset({"home-mac"}), NOW, "owner")
        target, _ = state.sessions.create(
            "acct-1", ALL_CAPS, frozenset({"home-mac"}), NOW, "target"
        )
        state.hosts.enroll("home-mac", account="acct-1")
        app = create_app(state)
        messages: list[dict[str, Any]] = []
        started = asyncio.Event()
        received: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        received.put_nowait({"type": "http.request", "body": b"", "more_body": False})

        async def receive() -> dict[str, Any]:
            return await received.get()

        async def send(message: MutableMapping[str, Any]) -> None:
            messages.append(dict(message))
            if message["type"] == "http.response.start":
                started.set()

        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/v1/events",
            "raw_path": b"/v1/events",
            "query_string": b"host_id=home-mac",
            "headers": [(b"authorization", f"Bearer {target}".encode()), (b"host", b"gw")],
            "client": ("127.0.0.1", 1),
            "server": ("gw", 80),
            "root_path": "",
        }
        task = asyncio.create_task(app(scope, receive, send))
        try:
            await asyncio.wait_for(started.wait(), 1)
            async with asyncio.timeout(1):
                while not state.metadata._held:
                    await asyncio.sleep(0)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://gw"
            ) as http:
                response = await http.delete(
                    "/v1/devices/target" if kind == "device" else "/v1/hosts/home-mac",
                    headers={"Authorization": f"Bearer {owner}"},
                )
                assert response.status_code == 200
            await asyncio.wait_for(task, 1)  # Does not wait the heartbeat interval.
            body = b"".join(message.get("body", b"") for message in messages)
            expected = b'"code": "unauthenticated"' if kind == "device" else b'"code": "not_found"'
            assert expected in body and b"event: metadata" not in body
            assert not state.metadata._held
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            state.push.close()

    asyncio.run(scenario())


def test_subscription_failure_still_severs_revoked_device_access(monkeypatch: Any) -> None:
    async def scenario() -> None:
        state, backend = make_state()
        owner, _ = state.sessions.create("acct-1", ALL_CAPS, frozenset({"home-mac"}), NOW, "owner")
        target, refresh = state.sessions.create(
            "acct-1", ALL_CAPS, frozenset({"home-mac"}), NOW, "target"
        )
        socket = _FakeSocket()
        state.sockets.register("acct-1", "target-lease", socket, "target")
        key, signal = state.metadata.register("acct-1", "target", "home-mac")

        def fail(*args: Any) -> bool:
            raise RuntimeError("synthetic subscription write failure")

        monkeypatch.setattr(state.push, "revoke_device", fail)
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=create_app(state)), base_url="http://gw"
            ) as http:
                with pytest.raises(RuntimeError, match="synthetic subscription write failure"):
                    await http.delete(
                        "/v1/devices/target", headers={"Authorization": f"Bearer {owner}"}
                    )
            assert state.sessions.lookup(target, NOW) is None
            assert state.sessions.refresh(refresh, NOW) is None
            assert signal.event.is_set()
            assert socket.closes[-1][1] == "unauthenticated"
        finally:
            state.metadata.unregister(key)
            state.push.close()

    asyncio.run(scenario())


def test_device_list_expired_login_notification_installation_and_row_bound() -> None:
    async def scenario() -> None:
        state, backend = make_state()
        owner, _ = state.sessions.create("acct-1", ALL_CAPS, frozenset({"home-mac"}), NOW, "owner")
        state.sessions.create(
            "acct-1", ALL_CAPS, frozenset({"home-mac"}), NOW - 1_000_000, "expired-access"
        )
        state.push.register_device(
            "acct-1", "notification-install", "synthetic-notification-only", "android", NOW
        )
        for index in range(202):
            state.sessions.create(
                "acct-1", ALL_CAPS, frozenset({"home-mac"}), NOW, f"extra-{index:03}"
            )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(state)), base_url="http://gw"
        ) as http:
            page = (
                await http.get("/v1/devices", headers={"Authorization": f"Bearer {owner}"})
            ).json()
            assert len(page["devices"]) == 200 and page["truncated"]
            assert page["devices"][0]["device_id"] == "owner"
            assert (
                next(row for row in page["devices"] if row["device_id"] == "expired-access")[
                    "status"
                ]
                == "refresh_only"
            )
            for device in tuple(state.sessions.device_metadata("acct-1", NOW)):
                if device.startswith("extra-"):
                    state.sessions.revoke_device("acct-1", device)
            page = (
                await http.get("/v1/devices", headers={"Authorization": f"Bearer {owner}"})
            ).json()
            row = next(row for row in page["devices"] if row["device_id"] == "notification-install")
            assert (
                row["status"] == "notifications_only"
                and row["auth_sessions"] == 0
                and not row["current"]
            )
        state.push.close()

    asyncio.run(scenario())
