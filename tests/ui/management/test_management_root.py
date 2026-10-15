"""Real Flet root + ASGI gateway management; physical/render proof pending."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from relaydeck.client.features.management.controller import ManagementController
from relaydeck.client.features.sync.transport import HttpxGatewayTransport, TransportError
from tests.support.host_harness import run
from tests.support.sync.transport_fixtures import _wait
from tests.support.ui.management import _render


def test_root_device_confirm_revoke_and_current_login_cleanup() -> None:
    async def scenario() -> None:
        (
            app,
            http,
            state,
            controller,
            public,
            fingerprint,
            host_token,
            other,
            refresh,
            logged_out,
        ) = await _render()
        try:
            await app.set_props_and_fire("console-nav-settings", {}, "click")
            await _wait(
                lambda: any(
                    row.device_id == "this-login" for row in state.sessions._by_access.values()
                )
            )
            await app.settle()
            assert app.find("managed-device-revoke-other-login").height == 48
            await app.set_props_and_fire("managed-device-revoke-other-login", {}, "click")
            assert state.sessions.lookup(other, state.now_ms()) is not None
            await app.set_props_and_fire("management-revoke-confirm", {}, "click")
            await _wait(lambda: state.sessions.lookup(other, state.now_ms()) is None)
            assert state.sessions.refresh(refresh, state.now_ms()) is None and not logged_out
            await app.settle()
            await app.set_props_and_fire("managed-device-revoke-this-login", {}, "click")
            assert "signs you out" in app.find("management-revoke-disclosure").value
            await app.set_props_and_fire("management-revoke-confirm", {}, "click")
            await _wait(lambda: bool(logged_out))
            assert not state.sessions.device_metadata("acct-1", state.now_ms())
            # Settings embeds the pane under its own "Devices and access" panel.
            assert app.find("management-bounds") is not None
        finally:
            await app.set_props_and_fire("test-unmount", {}, "click")
            await http.aclose()
            state.push.close()

    run(scenario())


def test_root_host_fingerprint_confirmation_enrollment_and_revoke_effects() -> None:
    async def scenario() -> None:
        (
            app,
            http,
            state,
            controller,
            public,
            fingerprint,
            host_token,
            other,
            refresh,
            logged_out,
        ) = await _render()
        try:
            await app.set_props_and_fire("console-nav-settings", {}, "click")
            await app.settle()
            await app.set_props_and_fire("host-home-mac", {}, "click")
            await app.settle()
            assert app.find("managed-host-fingerprint").value == fingerprint
            assert app.find("managed-host-revoke").height == 48
            await app.set_props_and_fire("enroll-host-open", {}, "click")
            for key, value in (
                ("enroll-host-label", "New home"),
                ("enroll-host-key", public),
                ("enroll-fingerprint", "SHA256:wrong"),
            ):
                await app.set_props_and_fire(key, {"value": value}, "change", value)
            await app.set_props_and_fire(
                "enroll-fingerprint-confirm", {"value": True}, "change", "true"
            )
            await app.set_props_and_fire("enroll-host-create", {}, "click")
            await app.settle()
            assert not state.enrollments._items
            assert "fingerprint" in app.find("management-error").value
            await app.set_props_and_fire(
                "enroll-fingerprint", {"value": fingerprint}, "change", fingerprint
            )
            await app.set_props_and_fire(
                "enroll-fingerprint-confirm", {"value": True}, "change", "true"
            )
            await app.set_props_and_fire("enroll-host-create", {}, "click")
            await _wait(lambda: bool(state.enrollments._items))
            await app.settle()
            assert app.find("enroll-bound-fingerprint").value == fingerprint
            assert len(app.find("enroll-one-use-code").value) in (12, 14)
            assert "does not establish" in app.find("enroll-host-explanation").value
            # Matching an already paired key cannot claim this new invitation was consumed.
            code = app.find("enroll-one-use-code").value
            await app.set_props_and_fire("management-refresh", {}, "click")
            await app.settle()
            assert app.find("enroll-one-use-code").value == code
            await app.set_props_and_fire("managed-host-revoke", {}, "click")
            assert state.hosts.lookup(host_token) is not None
            await app.set_props_and_fire("management-revoke-confirm", {}, "click")
            await _wait(lambda: state.hosts.lookup(host_token) is None)
            await app.settle()
            assert "home-mac" not in controller.state.inventory.hosts
            assert not logged_out and state.sessions.lookup(other, state.now_ms()) is not None
        finally:
            await app.set_props_and_fire("test-unmount", {}, "click")
            await http.aclose()
            state.push.close()

    run(scenario())


def test_management_disposal_expiry_and_unknown_revoke_are_fenced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        (
            app,
            http,
            state,
            controller,
            public,
            fingerprint,
            host_token,
            other,
            refresh,
            logged_out,
        ) = await _render()
        manager = ManagementController(
            HttpxGatewayTransport("http://gw", lambda: None, http),
            "acct-1",
            lambda: None,
            lambda rows: None,
            lambda: None,
        )
        try:
            # Use the root's actual authenticated transport via a token callable.
            token = next(
                access
                for access, session in state.sessions._by_access.items()
                if session.device_id == "this-login"
            )

            async def get_token() -> str:
                assert isinstance(token, str)
                return token

            manager.transport.token = get_token
            await manager.refresh()
            await manager.enroll("Home", public, fingerprint, True)
            assert manager.invitation
            manager.clock = lambda: time.time() + 3601
            manager.expire()
            assert not manager.invitation and "expired" in manager.notice
            calls: list[str] = []
            real_revoke = manager.transport.revoke_device

            async def uncertain(identity: str) -> dict[str, Any]:
                calls.append(identity)
                raise TransportError("host_offline", "synthetic lost response")

            monkeypatch.setattr(manager.transport, "revoke_device", uncertain)
            await manager.revoke("device", "other-login", True)
            await manager.revoke("device", "other-login", True)
            assert calls == ["other-login"] and manager.needs_refresh
            monkeypatch.setattr(manager.transport, "revoke_device", real_revoke)
            await manager.refresh()
            assert not manager.needs_refresh
            gate = asyncio.Event()
            real_hosts = manager.transport.hosts

            async def delayed() -> dict[str, Any]:
                await gate.wait()
                return await real_hosts()

            manager.transport.hosts = delayed
            task = asyncio.create_task(manager.refresh())
            await asyncio.sleep(0)
            manager.dispose()
            gate.set()
            await task
            assert manager.hosts == [] and manager.devices == [] and not manager.invitation
        finally:
            manager.dispose()
            await app.set_props_and_fire("test-unmount", {}, "click")
            await http.aclose()
            state.push.close()

    run(scenario())
