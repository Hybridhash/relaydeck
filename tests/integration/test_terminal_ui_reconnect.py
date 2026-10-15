"""Actual guarded Flet ASGI reconnect -> production hook -> resource -> native probe.

The native report and PTY are synthetic; this is not renderer/device proof.
"""

from __future__ import annotations

import asyncio
import struct
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import flet as ft
import pytest
from relaydeck_terminal import protocol
from relaydeck_terminal.terminal import RelayDeckTerminal

from relaydeck.client.resources import AppResources
from relaydeck.client.retained_sessions import RetainedSessions
from relaydeck.client.serve import WebSessionStore, build_web_app, make_relaydeck_main
from relaydeck.gateway.app import GatewayState, create_app
from relaydeck.gateway.backend import FakeBackend
from tests.support.gateway.cookie_socket import CookieSocket, until
from tests.support.scripted_sessions import make_scripted_sessions


@pytest.mark.parametrize("kind", ["agent", "shell"])
@pytest.mark.parametrize("break_parent", [False, True])
def test_verified_r1a_reuse_triggers_bound_native_probe_without_new_root_or_ownership(
    kind: str, break_parent: bool, monkeypatch: Any
) -> None:
    async def scenario() -> None:
        scripted = make_scripted_sessions()
        if kind == "agent":
            domain = scripted.factory.make_agent(
                host_id="mac", host_epoch="e1", agent_id="a", target="a"
            )
            domain.open()
            await domain.confirm_takeover()
            domain.on_draft_change("retained exact\ndraft")
        else:
            domain = scripted.factory.make_shell(host_id="mac")
            await domain.open("shell")
        controller = domain.controller
        lease = domain.lease
        controller._set(replace(controller.state, status="live"))
        store = WebSessionStore("http://test", b"s" * 32)
        sid = store.create_session("synthetic", "device", "access", "refresh", 2_000_000)
        state = GatewayState(backend=FakeBackend(), cursor_secret=b"x" * 32)
        gateway = create_app(state)
        resources: list[AppResources] = []
        native: list[RelayDeckTerminal] = []

        async def noop() -> None:
            pass

        pool = RetainedSessions()
        entry = await pool.acquire("mac", kind, "mac:a", "e1", lambda: domain)

        def run_app(page: Any, **kwargs: Any) -> AppResources:
            @ft.component
            def Surface() -> Any:
                term = ft.use_memo(lambda: RelayDeckTerminal(key="native"), [])

                def bind() -> None:
                    native.append(term)
                    domain.bind_view(term)

                ft.use_effect(bind, [])
                return term

            # Only retained-session resume is exercised; other services are inert test doubles.
            dependencies: dict[str, Any] = {
                "props": {},
                "client": None,
                "owns_client": False,
                "retained": pool,
                "bridge": None,
                "controller": SimpleNamespace(resume_ui=lambda: None),
                "transport": None,
                "commands": SimpleNamespace(close=noop),
            }
            resource = AppResources(**dependencies)
            resources.append(resource)
            page.render(Surface)
            return resource

        monkeypatch.setattr("relaydeck.client.app.run_app", run_app)
        main = make_relaydeck_main(
            store=store,
            gateway_state=state,
            gateway_app=gateway,
            base_url="http://test",
            host_id="mac",
        )
        app, _ = build_web_app(
            gateway_state=state,
            gateway_app=gateway,
            flet_main=main,
            public_origin="http://test",
            cookie_secret=store.cookie_secret,
            store=store,
        )
        sockets: list[CookieSocket] = []
        async with app.router.lifespan_context(app):
            try:
                cookie = "rd_session=" + store.sign_sid(sid)
                old = await CookieSocket(app, cookie).open()
                sockets.append(old)
                fid = await old.register()
                assert isinstance(fid, str)
                await until(lambda: native)
                term = native[0]
                flet_session = term.page.session
                original_connection = flet_session.connection
                await old.packet(
                    3,
                    {
                        "target": term._i,
                        "name": "data_channel_open",
                        "data": {"channel_name": "terminal", "channel_id": 42},
                    },
                )
                await until(lambda: term.channel_ready)
                prefix = b"\x01" + struct.pack("<I", 42)
                await old.send_bytes(prefix + protocol.encode_resume_report(0, 123456, 0, 0))
                await until(lambda: term._native_instance == 123456)
                # Resource selection is foreground/device/identity scoped before probing.
                for rejected in ("hidden-device", "no-entry", "closing", "identity"):
                    pool.device_foreground = rejected != "hidden-device"
                    pool.foreground = None if rejected == "no-entry" else entry
                    resources[0].closing = rejected == "closing"

                    def current(rejected: str = rejected) -> bool:
                        return rejected != "identity"

                    resources[0].resume_terminals(
                        transport=object(),
                        current=current,
                        closed=asyncio.Event(),
                    )
                    assert not resources[0].ui_resumes._active
                    # A hidden viewer is fenced too; it verifies on later acquisition.
                    assert controller.key_generation() is None
                pool.device_foreground, pool.foreground, resources[0].closing = True, entry, False
                recovery_calls: list[None] = []
                original_recovery = domain.require_ui_recovery

                def recover() -> None:
                    recovery_calls.append(None)
                    original_recovery()

                monkeypatch.setattr(domain, "require_ui_recovery", recover)
                reopened: list[None] = []
                term.add_channel_listener(lambda: reopened.append(None))
                term.write(b"lost synthetic output")
                if break_parent:
                    monkeypatch.setattr(term, "_parent", None)
                    with pytest.raises(
                        RuntimeError, match="Control must be added to the page first"
                    ):
                        _ = term.page
                await old.close()
                replacement = await CookieSocket(app, cookie, "198.51.100.20").open()
                sockets.append(replacement)
                assert await replacement.register(fid) == fid
                assert flet_session.connection is not original_connection
                if break_parent:
                    with pytest.raises(
                        RuntimeError, match="Control must be added to the page first"
                    ):
                        _ = term.page
                assert term.rebind_transport() is True
                assert term._channel is flet_session.connection.data_channel_for(42)
                while True:
                    packet = (await replacement.receive()).get("bytes", b"")
                    if packet[:5] == prefix and packet[5] == protocol.RESUME_PROBE:
                        request = struct.unpack(">I", packet[6:10])[0]
                        break
                assert controller.key_generation() is None
                await replacement.send_bytes(
                    prefix + protocol.encode_resume_report(request, 123456, 0, 0)
                )
                output: list[bytes] = []
                while True:
                    packet = (await replacement.receive()).get("bytes", b"")
                    if packet[:5] != prefix:
                        continue
                    if packet[5] == protocol.WRITE:
                        output.append(packet[10:])
                    if packet[5] == protocol.RESUME_VERIFIED:
                        break
                await until(lambda: not resources[0].ui_resumes._active)
                await replacement.send_bytes(prefix + protocol.encode_ack(term._sent_bytes))
                await until(lambda: term._acked_bytes == term._sent_bytes)
                assert output == [b"lost synthetic output"]
                assert len(resources) == len(native) == 1
                assert domain.current_term is term and domain.controller is controller
                assert domain.lease is lease and recovery_calls == reopened == []
                assert controller.key_generation() is not None
                assert term.viewer_generation == 1 and term._reset_count == 0
                assert len(scripted.ops.attaches) == len(scripted.ops.leases) == 1
                assert scripted.submitted == [] and scripted.prompted == []
                assert all(tag != "bytes" for tag, _ in scripted.streams.opened[-1][2].sent)
                if kind == "agent":
                    assert domain.composer_props()["draft"] == "retained exact\ndraft"
            finally:
                for socket in sockets:
                    await socket.close()
                for resource in resources:
                    await resource.close()

    asyncio.run(scenario())
