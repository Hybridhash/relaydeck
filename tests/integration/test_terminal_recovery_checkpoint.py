"""Real pinned Flet dispatch; no socket/native build proof."""

from __future__ import annotations

import asyncio
import logging
import struct
from typing import Any

import flet as ft
import httpx
import pytest
from flet.data_channel import _ProtocolMuxedDataChannel
from relaydeck_terminal import protocol
from relaydeck_terminal.terminal import RelayDeckTerminal

from relaydeck.client import serve
from relaydeck.client.features.shell.ops import HttpxShellGatewayOps
from relaydeck.client.features.sync.transport import TransportError
from relaydeck.client.features.terminal.models import TerminalViewState
from relaydeck.client.features.terminal.screen import status_line
from relaydeck.client.retained_sessions import RetainedSessions
from relaydeck.client.sessions import SessionFactory
from relaydeck.domain.models import ConnectionState
from relaydeck.gateway.app import create_app
from tests.support.fakes import FakeClipboard
from tests.support.gateway.flet_gateway import _gateway_state
from tests.support.host_harness import render_component, run
from tests.support.scripted_sessions import make_scripted_sessions
from tests.support.terminal.sessions import _agent_session, _session, _SocketFactory
from tests.support.ui.navigation import _state, _StubController, _StubTransport


async def viewer() -> tuple[Any, RelayDeckTerminal]:
    @ft.component
    def View() -> Any:
        term = ft.use_ref(None)
        if term.current is None:
            term.current = RelayDeckTerminal(key="probe-viewer")
        return term.current

    app = await render_component(View)
    channels: dict[int, Any] = {}

    def get_channel(number: int) -> Any:
        if number not in channels:
            channels[number] = _ProtocolMuxedDataChannel(number, app.conn)
        return channels[number]

    # Actual pinned mux implementation; frame transport remains in-process.
    app.conn.data_channel_for = get_channel
    return app, app.find("probe-viewer")


async def channel(app: Any, value: int) -> None:
    await app.set_props_and_fire(
        "probe-viewer",
        {},
        "data_channel_open",
        {"channel_name": "terminal", "channel_id": value},
    )


def test_actual_channel_dispatch_retained_reopened_and_stale_frames() -> None:
    async def scenario() -> None:
        app, term = await viewer()
        await channel(app, 11)
        assert term.viewer_generation == 1
        term.write(b"synthetic output")
        old = term._channel
        assert old is not None
        callbacks: list[int] = []
        term.add_channel_listener(lambda: callbacks.append(term.viewer_generation))
        await channel(app, 11)
        assert callbacks == [] and term.viewer_generation == 1
        # Queue an old-channel input callback, then reopen before it executes.
        received: list[bytes] = []
        term.add_input_listener(lambda origin, data: received.append(data))
        snapshot = asyncio.create_task(term.get_snapshot_text())
        selection = asyncio.create_task(term.get_selection_text())
        await asyncio.sleep(0)
        old._deliver(protocol.encode_input(b"old", origin=protocol.ORIGIN_TEXT))
        await app.session.dispatch_event(
            term._i,
            "data_channel_open",
            {
                "channel_name": "terminal",
                "channel_id": 12,
            },
        )
        await app.settle()
        assert callbacks == [2] and received == []
        assert term._sent_bytes == term._acked_bytes == 0
        assert not term.input_enabled
        for pending in (snapshot, selection):
            with pytest.raises(RuntimeError, match="viewer reopened"):
                await pending
        fresh_frames = [payload for number, payload in app.conn.data_frames if number == 12]
        assert fresh_frames and all(
            payload[0] == protocol.INPUT_ENABLED for payload in fresh_frames
        )

    run(scenario())


@pytest.mark.parametrize("rebound", [False, True])
def test_same_channel_native_replacement_retires_old_delivery_and_resume(
    rebound: bool, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(logging.getLogger("relaydeck.client.typing_split"), "propagate", True)
    caplog.set_level(logging.INFO, logger="relaydeck.client.typing_split")

    async def scenario() -> None:
        app, term = await viewer()
        await channel(app, 31)
        retained = term._channel
        assert isinstance(retained, _ProtocolMuxedDataChannel)
        retained._deliver(protocol.encode_resume_report(0, 111111, 0, 0))
        await app.settle()
        term.reset()
        term.reset()
        if rebound:
            assert term.rebind_transport()
        old_delivery = retained._handler
        callbacks: list[int] = []

        def reopened() -> None:
            callbacks.append(term.viewer_generation)
            # The production RendererBinding listener fences again before recovery.
            term.fence_fresh_viewer()

        term.add_channel_listener(reopened)
        received: list[bytes] = []
        modes: list[bool] = []
        term.add_input_listener(lambda origin, data: received.append(data))
        term.add_mode_listener(modes.append)
        term.write(b"old screen")
        snapshot = asyncio.create_task(term.get_snapshot_text())
        selection = asyncio.create_task(term.get_selection_text())
        await asyncio.sleep(0)
        resume = asyncio.create_task(term.rebind_resumed())
        await asyncio.sleep(0)
        if rebound:
            old_delivery = retained._handler
        term.write(b"old held output")
        # Both callbacks capture A's generation before either executes; B's hello
        # must retire the queued A input, mode and accounting effects on this channel.
        assert old_delivery is not None
        old_delivery(protocol.encode_resume_report(0, 222222, 0, 0))
        old_delivery(
            protocol.encode_input(b"stale", origin=protocol.ORIGIN_TEXT)
            + protocol.encode_ack(9)
            + bytes([protocol.MODE_REPORT, 1])
        )
        await app.settle()
        assert callbacks == [2]
        assert term._channel is retained and not retained._closed
        assert term._native_instance == 222222 and term.viewer_generation == 2
        assert term._reset_count == 0, "reset accounting belongs to the fresh engine"
        assert received == [] and modes == []
        assert term._sent_bytes == term._acked_bytes == term._pending_bytes == 0
        assert term._journal_bytes == term._resume_held_bytes == 0
        assert term.resume_unverified and not term.input_enabled
        assert (await resume).reason == "superseded"
        for pending in (snapshot, selection):
            with pytest.raises(RuntimeError, match="viewer reopened"):
                await pending
        retained._deliver(protocol.encode_resume_report(0, 222222, 0, 0))
        retained._deliver(bytes([protocol.MODE_REPORT, 1]))
        await app.settle()
        assert callbacks == [2] and modes == [True]
        replacement_logs = [
            record.getMessage()
            for record in caplog.records
            if record.getMessage().startswith("term_native_replaced")
        ]
        assert replacement_logs == [
            f"term_native_replaced term={id(term) % 100000} prev=11111 new=22222"
        ]
        # Only explicit reset releases the new viewer's effect fence. Its new
        # delivery remains live; old callbacks cannot affect it even after reset.
        term.reset()
        old_delivery(protocol.encode_input(b"stale again", origin=protocol.ORIGIN_TEXT))
        retained._deliver(protocol.encode_input(b"fresh", origin=protocol.ORIGIN_TEXT))
        await app.settle()
        assert received == [b"fresh"] and not term.resume_unverified
        # Recovery of B must also establish reset-count continuity; A's reset
        # count cannot poison every later verified resume on the fresh engine.
        resumed = asyncio.create_task(term.rebind_resumed())
        await asyncio.sleep(0)
        retained._deliver(protocol.encode_resume_report(term._resume_next_id, 222222, 1, 0))
        assert (await resumed).status == "resumed"

    run(scenario())


@pytest.mark.parametrize("kind", ["shell", "agent"])
def test_same_channel_native_replacement_uses_existing_explicit_screen_recovery(kind: str) -> None:
    async def scenario() -> None:
        session: Any
        if kind == "shell":
            session, ops, factory = _session()
            await session.open("term-1")
        else:
            session, ops, factory, submitted = _agent_session()
            session.open()
            await session.confirm_takeover()
            await session.type_text("exact retained draft")
        app, term = await viewer()
        session.bind_view(term)
        await channel(app, 32)
        retained = term._channel
        assert isinstance(retained, _ProtocolMuxedDataChannel)
        retained._deliver(protocol.encode_resume_report(0, 111111, 0, 0))
        await app.settle()
        old = session.controller
        assert old is not None
        old._client.epoch = "e1"
        old._handle_output(0, b"old screen")
        sent_before = len(factory.sockets[0].sent_bytes)
        attaches_before = len(ops.attaches)
        retained._deliver(protocol.encode_resume_report(0, 222222, 0, 0))
        await app.settle()
        assert session.controller is None and old.writer_revoked
        assert session.phase == "disconnected" and term.resume_unverified
        assert term._native_instance == 222222
        assert len(ops.attaches) == attaches_before
        assert len(factory.sockets[0].sent_bytes) == sent_before
        frame_start = len(app.conn.data_frames)
        if kind == "shell":
            await session.reconnect()
        else:
            session.open()
            await session.confirm_takeover()
        current = session.controller
        assert current is not None and current is not old
        current._client.epoch = "e1"
        expected = b"reconstructed greeting\r\nnext line"
        current._handle_output(0, expected)
        writes = [
            payload[5:]
            for number, payload in app.conn.data_frames[frame_start:]
            if number == 32 and payload[0] == protocol.WRITE
        ]
        assert writes == [expected] and term._channel is retained
        assert term.viewer_generation == 2 and not term.resume_unverified
        # Explicit ShellSession.reconnect checks ownership before its requested
        # control attach; the hello itself must never make either request.
        assert len(ops.attaches) == attaches_before + (2 if kind == "shell" else 1)
        if kind == "agent":
            assert session.store.draft == "exact retained draft" and submitted == []
        await session.dispose()

    run(scenario())


def test_size_diagnostics_report_applied_and_fenced_without_content(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(logging.getLogger("relaydeck.client.typing_split"), "propagate", True)
    term = RelayDeckTerminal(key="private-label")
    sizes: list[tuple[int, int]] = []
    term.add_size_listener(lambda cols, rows: sizes.append((cols, rows)))
    packet = protocol.DecodedPacket(protocol.SIZE_REPORT, struct.pack(">HH", 91, 37))
    with caplog.at_level(logging.INFO, logger="relaydeck.client.typing_split"):
        term._dispatch(packet)
        term._resume_fence = True
        term._dispatch(packet)
    assert sizes == [(91, 37)]
    assert [r.getMessage() for r in caplog.records if r.getMessage().startswith("term_size")] == [
        "term_size cols=91 rows=37 applied=True",
        "term_size cols=91 rows=37 applied=False",
    ]
    assert "private-label" not in caplog.text


def test_binding_and_foreground_diagnostics_are_event_only_and_content_free(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    from relaydeck.client.features.terminal.diagnostics import diagnostics_logger
    from relaydeck.client.features.terminal.surfaces import mount_agent_screen

    monkeypatch.setattr(diagnostics_logger(), "propagate", True)

    async def scenario() -> None:
        session, _, _, _ = _agent_session()
        session.open()
        await session.confirm_takeover()
        await session.type_text("private draft must not be logged")
        with caplog.at_level(logging.INFO, logger="relaydeck.client.typing_split"):
            app = await render_component(mount_agent_screen, session)
            term = session.current_term
            token = session._view_token
            assert term is not None and token is not None
            session.set_foreground(False)
            await app.settle()
            session.set_foreground(False)
            await app.settle()
            session.set_foreground(True)
            await app.settle()
            session.unbind_view()
            events = [r.getMessage() for r in caplog.records]
            identity = (
                f"term={id(term) % 100000} session={id(session) % 100000} "
                f"gen=0 token={token % 100000}"
            )
            assert f"term_bind {identity}" in events
            assert f"term_unbind {identity}" in events
            foreground = [event for event in events if event.startswith("term_foreground")]
            assert len(foreground) == 3
            assert [event.split("foreground=")[-1] for event in foreground] == [
                "True",
                "False",
                "True",
            ]
            assert "private draft must not be logged" not in caplog.text
        await session.dispose()

    run(scenario())


@pytest.mark.parametrize("kind", ["shell", "agent"])
def test_fresh_viewer_fences_writer_cursor_and_preserves_retained_draft(kind: str) -> None:
    async def scenario() -> None:
        if kind == "shell":
            session, ops, factory = _session()
            await session.open("term-1")
        else:
            session, ops, factory, submitted = _agent_session()
            session.open()
            await session.confirm_takeover()
            await session.type_text("unsent draft")
        app, term = await viewer()
        session.bind_view(term)
        await channel(app, 21)
        controller = session.controller
        assert controller is not None
        controller._client.epoch = "e1"
        controller._handle_output(0, b"greeting")
        assert session.term_state.applied_offset == 8
        registry = RetainedSessions()
        one = await registry.acquire("home-mac", kind, "synthetic", "e1", lambda: session)
        registry.release()
        again = await registry.acquire(
            "home-mac", kind, "synthetic", "e1", lambda: pytest.fail("recreated")
        )
        assert one is again
        # Leaving a retained session demotes its lease to VIEW asynchronously.
        # Settle that existing lifecycle before testing channel currency; it is
        # independent of native reopen and must never restore a writer here.
        if again.warm_task is not None:
            await again.warm_task
        controller = session.controller
        assert controller is not None and controller.mode == "view"
        writer_before = controller.paste_generation()
        assert writer_before is None
        await channel(app, 21)
        assert session.controller is controller and controller.paste_generation() is writer_before
        session.unbind_view()
        session.bind_view(term)
        assert session.controller is controller and controller.paste_generation() is writer_before
        sent_before = len(factory.sockets[0].sent_bytes)
        attaches_before = len(ops.attaches)
        await channel(app, 22)
        assert controller.writer_revoked and session.controller is None
        assert session.phase == "disconnected"
        assert session.term_state.epoch == "" and session.term_state.applied_offset == 0
        assert controller.resume_query() == ""
        assert controller.pending_resume is None and not controller.reattach_needed
        assert await controller.request_resize(100, 30) is False
        controller.on_viewer_overflow()
        assert not controller.reattach_needed
        controller._handle_output(8, b"late")
        assert session.term_state.applied_offset == 0
        assert len(ops.attaches) == attaches_before
        assert len(factory.sockets[0].sent_bytes) == sent_before
        with pytest.raises(TransportError, match="Explicit reattach"):
            await controller.start("unused", "e1", 8)
        if kind == "agent":
            assert session.store.draft == "unsent draft"
            assert submitted == []
        await registry.shutdown()

    run(scenario())


@pytest.mark.parametrize("kind", ["shell", "agent"])
def test_disconnect_projects_no_lease_and_fences_before_await(kind: str) -> None:
    async def scenario() -> None:
        if kind == "shell":
            session, _, _ = _session()
            await session.open("term-1")
        else:
            session, _, _, _ = _agent_session()
            session.open()
            await session.confirm_takeover()
        controller = session.controller
        assert controller is not None
        controller._client.epoch = "e1"
        controller._handle_output(0, b"hello")
        closing, finish = asyncio.Event(), asyncio.Event()
        close = controller.close

        async def held_close() -> None:
            closing.set()
            await finish.wait()
            await close()

        controller.close = held_close
        pending = asyncio.create_task(session.disconnect())
        await closing.wait()
        assert controller.paste_generation() is None
        assert session.term_state.status == "disconnected"
        assert await session.press_key("a") is False
        finish.set()
        await pending
        assert session.term_state.status == "disconnected"
        assert session.term_state.lease_mode == "none"
        assert "live" not in status_line(session.term_state)
        assert "no lease" in status_line(session.term_state)
        controller._handle_output(5, b"late")
        assert session.term_state.status == "disconnected"
        assert controller.paste_generation() is None

    run(scenario())


def test_replacing_python_viewer_cannot_inherit_native_history() -> None:
    async def scenario() -> None:
        session, _, _ = _session()
        await session.open("term-1")
        old_app, old_term = await viewer()
        session.bind_view(old_term)
        await channel(old_app, 41)
        controller = session.controller
        assert controller is not None
        controller._client.epoch = "e1"
        controller._handle_output(0, b"hello")
        session.unbind_view()
        new_app, new_term = await viewer()
        session.bind_view(new_term)
        assert session.controller is controller and not controller.writer_revoked
        await channel(new_app, 42)
        assert session.controller is None and controller.writer_revoked
        assert session.term_state.applied_offset == 0
        assert new_app.conn.data_frames
        assert all(
            payload[0] == protocol.INPUT_ENABLED for number, payload in new_app.conn.data_frames
        )
        await session.dispose()

    run(scenario())


def test_diagnostics_are_opt_in_and_reset_reasons_are_bounded(
    monkeypatch: Any, caplog: Any
) -> None:
    term = RelayDeckTerminal()
    sent: list[bytes] = []
    term._test_mark_ready(sent)
    monkeypatch.delenv("RELAYDECK_TERMINAL_DIAGNOSTICS", raising=False)
    term.reset(reason="secret terminal label")
    assert "terminal_lifecycle" not in caplog.text
    monkeypatch.setenv("RELAYDECK_TERMINAL_DIAGNOSTICS", "1")
    term.reset(reason="secret terminal label")
    assert "terminal_lifecycle reset" in caplog.text
    assert "secret terminal label" not in caplog.text
    assert "'reason': 3" in caplog.text


def test_disconnected_screen_cannot_enable_native_input() -> None:
    async def scenario() -> None:
        from relaydeck.client.features.terminal.screen import TerminalScreen

        app = await render_component(
            TerminalScreen,
            state=TerminalViewState(status="disconnected", lease_mode="none"),
            input_enabled=True,
            on_jump_to_bottom=lambda: None,
            on_size_report=lambda c, r: None,
            on_input=lambda origin, data: None,
        )
        assert app.find("terminal-surface").input_enabled is False

    run(scenario())


@pytest.mark.parametrize("kind", ["shell", "agent"])
def test_late_old_reader_cannot_disconnect_replacement_and_retention_supervises_it(
    kind: str,
) -> None:
    async def scenario() -> None:
        if kind == "shell":
            session, _, _ = _session()
            await session.open("term-1")
        else:
            session, _, _, _ = _agent_session()
            session.open()
            await session.confirm_takeover()
        controller = session.controller
        assert controller is not None
        entered, ended = asyncio.Event(), asyncio.Event()

        async def old_reader() -> None:
            entered.set()
            await ended.wait()
            raise TransportError("host_offline", "old reader ended")

        controller.run = old_reader
        registry = RetainedSessions()
        entry = await registry.acquire("home-mac", kind, "synthetic", "e1", lambda: session)
        await entered.wait()
        session._viewer_reopened()
        if kind == "shell":
            await session.reconnect()
        else:
            session.open()
            await session.confirm_takeover()
        current = session.controller
        assert current is not None and current is not controller
        active = asyncio.Event()
        read = current.run

        async def new_reader() -> None:
            active.set()
            await read()

        current.run = new_reader
        ended.set()
        await asyncio.wait_for(active.wait(), timeout=1)
        assert session.phase == "live" and session.controller is current
        assert current.paste_generation() is not None
        assert entry.task is not None and not entry.task.done()
        await registry.shutdown()

    run(scenario())


def test_synthetic_login_issued_identity_self_reconnect_and_foreign_owner(monkeypatch: Any) -> None:
    async def scenario() -> None:
        state = _gateway_state()
        monkeypatch.setattr(serve.time, "time", lambda: 1000)
        monkeypatch.setitem(serve._DEV_LOGIN_TOKEN, "value", "synthetic")
        monkeypatch.setitem(serve._DEV_HOSTS, "value", frozenset({"home-mac"}))
        app, store = serve.build_web_app(
            gateway_state=state,
            gateway_app=create_app(state),
            flet_main=lambda p: None,
            public_origin="http://127.0.0.1:8125",
            cookie_secret=b"s" * 32,
            dev_login=True,
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=store.public_origin
        ) as http:
            response = await http.get("/v1/auth/dev-login", params={"token": "synthetic"})
            assert response.status_code == 302
            sid = store.verify_cookie(http.cookies.get("rd_session"))
            identity = store.identity(sid)
            assert sid and identity
            access, _ = store.take_session(sid)
            issued = state.sessions.lookup(access, state.now_ms())
            assert issued and issued.device_id == identity.device_id != "web-dev"
            factory = _SocketFactory()

            async def token() -> str:
                return access

            async def submit(doc: dict[str, Any]) -> dict[str, Any]:
                pytest.fail("Unexpected mutation")

            resources_factory = SessionFactory(
                ops=HttpxShellGatewayOps(base_url=store.public_origin, token=token, client=http),
                open_stream=lambda lease, ticket: factory(ticket),
                submit_command=submit,
                prompt_text=None,
                renew_lease=None,
                clipboard=FakeClipboard(),
                device_id=identity.device_id,
            )
            shell = resources_factory.make_shell(host_id="home-mac")
            assert await shell.open("term-shell-1") == "live"
            await shell.disconnect()
            assert await shell.reconnect() == "live"
            await shell.disconnect()
            foreign_access, _ = state.sessions.create(
                "dev", issued.capabilities, issued.hosts, state.now_ms(), device_id="foreign"
            )

            async def foreign_token() -> str:
                return foreign_access

            foreign_ops = HttpxShellGatewayOps(
                base_url=store.public_origin, token=foreign_token, client=http
            )
            await foreign_ops.attach("home-mac", "term-shell-1", "shell", True)
            assert await shell.reconnect() == "conflict"
            assert state.sessions.lookup(foreign_access, state.now_ms()) is not None
            await shell.dispose()

    run(scenario())


def test_revoked_root_explains_recovery_without_restoring_inventory() -> None:
    async def scenario() -> None:
        from relaydeck.domain import reducers

        calls: list[str] = []
        state = reducers.revoke(_state())
        from relaydeck.client.app import Root
        from relaydeck.client.features.inventory.routing import RouteBridge

        controller = _StubController()
        app = await render_component(
            Root,
            controller=controller,
            transport=_StubTransport(),
            host_id="home-mac",
            bridge=RouteBridge(route="/agents", width=1280),
            go=lambda route: None,
            session_factory=make_scripted_sessions().factory,
            on_session_recovery=lambda: calls.append("check"),
        )
        controller.push(state)
        await app.settle()
        assert "pairing" in app.find("connection-recovery-explanation").value
        await app.set_props_and_fire("connection-recovery-action", {}, "click")
        assert calls == ["check"]
        assert controller.state.revoked and controller.state.inventory.hosts == {}
        assert controller.state.connection.state == ConnectionState.ACCESS_REVOKED

    run(scenario())
