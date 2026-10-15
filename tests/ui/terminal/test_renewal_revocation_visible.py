"""Renewal: revocation is visible.

Token loss and past-expiry renewal failures must surface through the
mounted shell screen via real Flet dispatch: the tools dock disables
(no live writer) and the session notice renders verbatim. No real
credentials: scripted renew callables raise the exact `_token` triple.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any

from relaydeck.client.adapters.fake import FakeClipboard
from relaydeck.client.features.shell import ScriptedShellGatewayOps, ShellSession
from relaydeck.client.features.shell.ops import LeaseInfo
from relaydeck.client.features.shell.screen import SHELL_NOTICE_KEY, mount_shell_screen
from relaydeck.client.features.sync.transport import TransportError
from relaydeck.client.features.terminal.keys import key_button_key
from relaydeck.client.features.terminal.renewal import utc_stamp_in
from relaydeck.client.features.terminal.screen import SURFACE_KEY
from tests.support.host_harness import render_component
from tests.support.terminal.dispatch import _SocketFactory, run_clean


def _shell_session(
    ops: ScriptedShellGatewayOps,
    factory: _SocketFactory,
    renew: Any,
) -> ShellSession:
    return ShellSession(
        ops=ops,
        host_id="home-mac",
        device_id=ops.device_id,
        socket_factory=factory,
        clipboard=FakeClipboard(),
        renew_lease=renew,
    )


async def _wait_for(predicate: Any, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("timed out waiting for condition")
        await asyncio.sleep(0.01)


def test_dispatched_token_loss_disables_dock_and_shows_notice() -> None:
    async def scenario() -> None:
        renewals: list[str] = []

        async def renew(lease_id: str) -> dict[str, Any]:
            renewals.append(lease_id)
            raise TransportError("unauthenticated", "no access token", False)

        ops = ScriptedShellGatewayOps()
        session = _shell_session(ops, _SocketFactory(), renew)
        assert await session.open("term-1") == "live"
        session.set_keyboard("keys")
        app = await render_component(mount_shell_screen, session, lambda: None, lambda: None)
        assert app.find(key_button_key("enter")).disabled is False
        session.start_lease_renewal(interval_s=0.005)
        await _wait_for(lambda: session.keys_enabled is False)
        await app.settle()
        # Visible through the SAME mount: dock disabled, notice rendered.
        assert app.find(key_button_key("enter")).disabled is True
        assert "sign in" in app.find(SHELL_NOTICE_KEY).value
        assert app.find(SURFACE_KEY) is not None
        assert len(renewals) == 1
        await session.stop_lease_renewal()

    run_clean(scenario())


def test_dispatched_expiry_kills_writer_with_notice() -> None:
    async def scenario() -> None:
        class _ShortLeaseOps(ScriptedShellGatewayOps):
            async def acquire_lease(self, attachment_id: str, mode: str) -> LeaseInfo:
                lease = await super().acquire_lease(attachment_id, mode)
                return replace(lease, expires_at=utc_stamp_in(0.15))

        renewals: list[str] = []

        async def renew(lease_id: str) -> dict[str, Any]:
            renewals.append(lease_id)
            raise TransportError("host_offline", "tunnel down", True)

        ops = _ShortLeaseOps()
        session = _shell_session(ops, _SocketFactory(), renew)
        assert await session.open("term-1") == "live"
        session.set_keyboard("keys")
        app = await render_component(mount_shell_screen, session, lambda: None, lambda: None)
        session.start_lease_renewal(interval_s=0.005)
        await _wait_for(lambda: session.keys_enabled is False)
        await app.settle()
        assert app.find(key_button_key("enter")).disabled is True
        assert "expired" in app.find(SHELL_NOTICE_KEY).value
        count = len(renewals)
        await asyncio.sleep(0.04)
        assert len(renewals) == count  # no calls at/after expiry
        await session.stop_lease_renewal()

    run_clean(scenario())
