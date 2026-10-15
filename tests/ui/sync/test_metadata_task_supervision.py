"""A same-identity reconnect must revive a dead metadata task, not Root."""

from __future__ import annotations

import asyncio
import json
from typing import Any, cast

import flet as ft
import pytest
from flet.data_channel import _ProtocolMuxedDataChannel
from relaydeck_terminal import protocol

from relaydeck.client.app import Root
from relaydeck.client.features.inventory.routing import RouteBridge
from relaydeck.client.features.sync.controller import SyncController
from relaydeck.client.features.sync.sse import SseFrame
from relaydeck.client.features.sync.transport import HttpxGatewayTransport
from relaydeck.client.retained_sessions import RetainedSessions
from relaydeck.domain.models import ConnectionState
from tests.support.host_harness import render_component, run
from tests.support.scripted_sessions import make_scripted_sessions
from tests.support.sync.scripted_sync import _event_doc, _ScriptedTransport, _snapshot_doc
from tests.support.ui.navigation import _StubTransport
from tests.support.ui.phone_terminal import _ancestry, _shape


@pytest.mark.parametrize("recovery", ["resume", "banner", "gap_banner"])
def test_sync_task_failure_recovery_retains_terminal(recovery: str) -> None:
    async def scenario() -> None:
        release = asyncio.Event()
        parked = asyncio.Event()

        class Transport(_ScriptedTransport):
            async def snapshot(self, host_id: str) -> dict[str, Any]:
                self.snapshots += 1
                doc = _snapshot_doc()
                doc["host"].update(id=host_id, epoch="epoch-7")
                for name in ("workspaces", "tabs", "panes", "agents"):
                    for item in doc[name]:
                        item["host_id"] = host_id
                return doc

            async def stream(self, host_id: str, cursor: str) -> Any:
                yield SseFrame("stream_open", "")
                if recovery == "gap_banner" and self.snapshots <= 3:
                    if self.snapshots == 1:
                        await release.wait()
                    event = _event_doc(5, host_id=host_id, host_epoch="epoch-7")
                    yield SseFrame("metadata", json.dumps(event))
                    return
                if self.snapshots == 1:
                    await release.wait()
                    raise RuntimeError("synthetic secret must never reach state/log")
                await parked.wait()

        transport = Transport()
        controller = SyncController(transport=transport)

        async def retry_delay(delay: float) -> None:
            await asyncio.sleep(0)

        controller.sleep = retry_delay
        sessions = make_scripted_sessions()
        pool = RetainedSessions()

        @ft.component
        def Parent() -> Any:
            mounted, set_mounted = ft.use_state(True)
            foreground, set_foreground = ft.use_state(True)
            return ft.Column(
                controls=[
                    Root(
                        controller=controller,
                        transport=cast(HttpxGatewayTransport, _StubTransport()),
                        host_id="home-mac",
                        bridge=RouteBridge(
                            route="/", width=390 if recovery == "gap_banner" else 1280
                        ),
                        go=lambda route: None,
                        session_factory=sessions.factory,
                        retained_sessions=pool,
                        account="synthetic",
                        foreground=foreground,
                    )
                    if mounted
                    else ft.Text("Closed"),
                    ft.Button("Unmount", key="unmount", on_click=lambda e: set_mounted(False)),
                    ft.Button(
                        "Background", key="background", on_click=lambda e: set_foreground(False)
                    ),
                    ft.Button(
                        "Foreground", key="foreground", on_click=lambda e: set_foreground(True)
                    ),
                ]
            )

        app = await render_component(Parent)
        try:
            await app.set_props_and_fire("agent-home-mac:term-1", {}, "click")
            await app.set_props_and_fire("agent-takeover-confirm", {}, "click")
            session = sessions.factory.created[0]
            engine, terminal_controller = session.current_term, session.controller
            assert engine is not None
            app.conn.data_channel_for = lambda number: _ProtocolMuxedDataChannel(number, app.conn)
            await app.set_props_and_fire(
                "terminal-surface",
                {},
                "data_channel_open",
                {"channel_name": "terminal", "channel_id": 51},
            )
            binding = session._viewer_binding
            engine_shape = _shape(_ancestry(app, engine))
            session._handle_bytes(b"synthetic retained output\r\n")
            output = bytes(session.output_log)
            frames = list(app.conn.data_frames)
            socket = sessions.streams.opened[0][2]
            sent = list(socket.sent)
            store = session.store
            session.on_draft_change("kept draft 🐾")
            release.set()
            await app.settle()
            assert controller.state.connection.state is ConnectionState.DISCONNECTED
            assert controller.state.connection.detail == (
                "gap_stalled" if recovery == "gap_banner" else "sync_task_failed"
            )
            assert controller.state.stale
            if recovery == "gap_banner":
                assert transport.snapshots == 3 and controller.last_run_exit == "completed"
                controller.resume_ui()
                controller.resume_ui()
                await app.settle()
                assert transport.snapshots == 3  # no automatic retry on ordinary resume
                # Actual Root effect cleanup/resubscription also stays stopped.
                await app.set_props_and_fire("background", {}, "click")
                await app.set_props_and_fire("foreground", {}, "click")
                assert transport.snapshots == 3
                assert controller.state.connection.detail == "gap_stalled"
                # Platform background now drops the terminal stream/control.
                # Foreground restores local actions, not the closed PTY reader.
                assert socket.closed and session.lease is None
                assert session.controller.key_generation() is None
                assert socket.sent[: len(sent)] == sent
                assert all(
                    kind == "text" and json.loads(data) == {"kind": "bye"}
                    for kind, data in socket.sent[len(sent) :]
                )
                sent = list(socket.sent)
            else:
                assert controller.last_run_exit == "error:RuntimeError"
            if recovery in ("banner", "gap_banner"):
                if recovery == "gap_banner":
                    action = app.find("terminal-connection-retry")
                    assert action.content.value == "Retry"
                    assert (
                        "stopped after repeated gaps" in app.find("terminal-connection-dot").tooltip
                    )
                else:
                    action = app.find("banner-action")
                    assert action.content == "Retry"
                # Dispatch two actual Flet clicks before settling/rerender.
                # A still-visible Retry must not create two metadata tasks.
                await app.session.dispatch_event(action._i, "click", "")
                await app.session.dispatch_event(action._i, "click", "")
            else:
                controller.resume_ui()
                controller.resume_ui()  # rapid duplicate reconnect is idempotent
            await app.settle()
            assert transport.snapshots == (4 if recovery == "gap_banner" else 2)
            assert controller.state.connection.state is ConnectionState.LIVE
            assert len(controller._listeners) == 1 and len(controller._resume_listeners) == 1
            assert session.current_term is engine and session.controller is terminal_controller
            assert session._viewer_binding == binding and engine.viewer_generation == 1
            assert _shape(_ancestry(app, engine)) == engine_shape
            assert bytes(session.output_log) == output
            assert session.store is store and len(sessions.factory.created) == 1
            assert session.store.draft == "kept draft 🐾" and len(sessions.ops.attaches) == 1
            assert socket.sent == sent and sessions.submitted == [] and sessions.prompted == []
            assert app.conn.data_frames[: len(frames)] == frames
            assert all(
                payload[0] not in (protocol.RESET, protocol.WRITE)
                for _, payload in app.conn.data_frames[len(frames) :]
            )
        finally:
            await app.set_props_and_fire("unmount", {}, "click")
        assert controller._listeners == [] and controller._resume_listeners == []

    run(scenario())
