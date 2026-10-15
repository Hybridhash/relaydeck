"""Actual Root lifecycle and quiet native/viewer output regressions."""

import asyncio
import struct
from dataclasses import replace
from typing import Any

import pytest
from flet.data_channel import _ProtocolMuxedDataChannel
from relaydeck_terminal import protocol

from relaydeck.client.features.inventory.routing import RouteBridge
from relaydeck.client.features.shell.ops import ShellOpError
from relaydeck.gateway import terminal_wire
from tests.support.host_harness import run
from tests.support.sync.transport_fixtures import _wait
from tests.support.ui.instant_inventory import navigate, valid_inventory
from tests.support.ui.instant_open import jobs
from tests.support.ui.navigation import _StubTransport
from tests.support.ui.retained_root import _active, _open, _render


@pytest.mark.parametrize("width", [390, 1280])
@pytest.mark.parametrize("previously_selected", [False, True])
def test_backgrounded_shell_navigation_only_obtains_view(
    width: int, previously_selected: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_StubTransport, "snapshot", valid_inventory)

    async def scenario() -> None:
        app, _controller, scripted, pool, _now = await _render(
            bridge=RouteBridge(width=width, height=844)
        )
        try:
            await navigate(app, "terminals", width)
            await app.set_props_and_fire("shell-home-mac:tab-1", {}, "click")
            await _wait(lambda: pool.foreground.session.phase == "live")
            entry = pool.foreground
            shell, engine = entry.session, entry.session.current_term
            greeting = b"retained shell greeting"
            socket = next(
                sock for lease, _, sock in scripted.streams.opened if lease == shell.lease.lease_id
            )
            socket.queue(("bytes", terminal_wire.encode_output(shell.lease.epoch, 0, greeting)))
            await _wait(lambda: shell.term_state.applied_offset == len(greeting))
            if not previously_selected:
                await navigate(app, "console", width)
                await _open(app, "home-mac:term-1")
                await jobs(pool)
                assert shell.lease.mode == "view"
                pool.foreground.session.on_draft_change("retained agent draft")
            await app.set_props_and_fire("test-background", {}, "click")
            await jobs(pool)
            assert shell.paused and shell.lease is None
            await app.set_props_and_fire("test-foreground", {}, "click")
            await jobs(pool)
            # Previously selected foreground recovery itself never opens a lease.
            assert shell.lease is None
            before = len(scripted.ops.attaches), len(scripted.ops.leases)
            await navigate(app, "console", width)
            await navigate(app, "terminals", width)
            await app.set_props_and_fire("shell-home-mac:tab-1", {}, "click")
            await _wait(lambda: shell.lease is not None or entry.stopped)
            await jobs(pool)
            await app.settle()
            assert not entry.stopped and shell.lease.mode == "view"
            assert shell.current_term is engine and not shell.paused
            assert shell.controller._client.epoch == shell.lease.epoch
            assert shell.controller._client.resume_offset() == len(greeting)
            assert bytes(shell.output_log) == greeting
            assert len(scripted.ops.attaches) == before[0]
            assert all(doc["mode"] == "view" for doc in scripted.ops.leases[before[1] :])
            assert shell.controller.key_generation() is None
            assert not await shell.controller.request_resize(40, 20)
            assert _active(app, "shell-takeover") is not None
            # Navigation emitted only stream control, no PTY input/resize.
            assert not any(
                kind == "bytes" for _, _, sock in scripted.streams.opened for kind, _ in sock.sent
            )
            if not previously_selected:
                agent = pool.get("home-mac", "agent", "home-mac:term-1").session
                assert agent.store.draft == "retained agent draft"
        finally:
            await app.set_props_and_fire("test-unmount", {}, "click")

    run(scenario())


@pytest.mark.parametrize("failure", ["changed_epoch", "revoked"])
def test_paused_reopen_revalidates_identity_and_inventory_before_view(
    failure: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_StubTransport, "snapshot", valid_inventory)

    async def scenario() -> None:
        app, controller, scripted, pool, _now = await _render()
        entered, finish = asyncio.Event(), asyncio.Event()
        try:
            await app.set_props_and_fire("console-rail-terminals", {}, "click")
            await app.set_props_and_fire("shell-home-mac:tab-1", {}, "click")
            await _wait(lambda: pool.foreground.session.phase == "live")
            entry = pool.foreground
            await app.set_props_and_fire("console-rail-console", {}, "click")
            await _open(app, "home-mac:term-1")
            await jobs(pool)
            await app.set_props_and_fire("test-background", {}, "click")
            await jobs(pool)
            await app.set_props_and_fire("test-foreground", {}, "click")
            await jobs(pool)
            before = len(scripted.ops.attaches), len(scripted.ops.leases)

            async def delayed(_transport: Any, host: str) -> dict[str, Any]:
                doc = await valid_inventory(_transport, host)
                entered.set()
                await finish.wait()
                if failure == "changed_epoch":
                    doc["host"]["epoch"] = "changed-host-epoch"
                return doc

            monkeypatch.setattr(_StubTransport, "snapshot", delayed)
            await app.set_props_and_fire("console-rail-terminals", {}, "click")
            await app.set_props_and_fire("shell-home-mac:tab-1", {}, "click")
            await _wait(entered.is_set)
            # Includes a warmed agent leaving; never count it as shell recovery.
            await jobs(pool)
            before = len(scripted.ops.attaches), len(scripted.ops.leases)
            if failure == "revoked":
                controller.push(replace(controller.state, revoked=True))
            finish.set()
            await _wait(lambda: entry.stopped)
            await jobs(pool)
            assert (len(scripted.ops.attaches), len(scripted.ops.leases)) == before
            assert entry.session.lease is None and not scripted.ops.closes
        finally:
            finish.set()
            await app.set_props_and_fire("test-unmount", {}, "click")

    run(scenario())


@pytest.mark.parametrize("failure", ["not_found", "unauthenticated"])
def test_paused_attachment_failure_never_falls_back_to_control(
    failure: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_StubTransport, "snapshot", valid_inventory)

    async def scenario() -> None:
        app, _controller, scripted, pool, _now = await _render()
        try:
            await app.set_props_and_fire("console-rail-terminals", {}, "click")
            await app.set_props_and_fire("shell-home-mac:tab-1", {}, "click")
            await _wait(lambda: pool.foreground.session.phase == "live")
            entry = pool.foreground
            await app.set_props_and_fire("console-rail-console", {}, "click")
            await _open(app, "home-mac:term-1")
            await jobs(pool)
            await app.set_props_and_fire("test-background", {}, "click")
            await jobs(pool)
            await app.set_props_and_fire("test-foreground", {}, "click")
            await jobs(pool)
            before = len(scripted.ops.attaches), len(scripted.ops.leases)
            calls: list[str] = []
            acquire = scripted.ops.acquire_lease

            async def refused(_attachment: str, mode: str) -> Any:
                if _attachment != entry.session.attachment.attachment_id:
                    return await acquire(_attachment, mode)
                calls.append(mode)
                raise ShellOpError(failure, "Synthetic attachment unavailable")

            monkeypatch.setattr(scripted.ops, "acquire_lease", refused)
            await app.set_props_and_fire("console-rail-terminals", {}, "click")
            await app.set_props_and_fire("shell-home-mac:tab-1", {}, "click")
            await _wait(lambda: bool(calls))
            await jobs(pool)
            assert calls == ["view"]
            assert len(scripted.ops.attaches) == before[0]
            assert all(doc["mode"] == "view" for doc in scripted.ops.leases[before[1] :])
            assert entry.session.lease is None and entry.session.phase == "disconnected"
            assert entry.session.cached_screen == "" and not scripted.ops.closes
        finally:
            await app.set_props_and_fire("test-unmount", {}, "click")

    run(scenario())


def snapshot_bridge(app: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    channel = _ProtocolMuxedDataChannel(83, app.conn)
    app.conn.data_channel_for = lambda _: channel
    original = app.conn.send_data_channel_frame

    def reply(number: int, packet: bytes) -> None:
        original(number, packet)
        if packet[0] == protocol.REQUEST_SNAPSHOT:
            request = struct.unpack_from(">I", packet, 1)[0]
            text = b"old screen before switching"
            channel._deliver(protocol.encode_snapshot_chunk(request, len(text), 0, text))

    monkeypatch.setattr(app.conn, "send_data_channel_frame", reply)
    return channel


@pytest.mark.parametrize("width", [390, 1280])
@pytest.mark.parametrize("kind", ["agent", "shell"])
def test_hidden_output_then_quiet_reopen_never_installs_older_preview(
    kind: str, width: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        app, _controller, scripted, pool, _now = await _render(
            bridge=RouteBridge(width=width, height=844)
        )
        try:
            if kind == "agent":
                await _open(app, "home-mac:term-1")
            else:
                await navigate(app, "terminals", width)
                await app.set_props_and_fire("shell-home-mac:tab-1", {}, "click")
                await _wait(lambda: pool.foreground.session.phase == "live")
            entry = pool.foreground
            session, engine = entry.session, entry.session.current_term
            snapshot_bridge(app, monkeypatch)
            await app.set_props_and_fire(
                "terminal-surface",
                {},
                "data_channel_open",
                {"channel_name": "terminal", "channel_id": 83},
            )
            await navigate(app, "console", width)
            await _open(app, "home-mac:term-2" if kind == "agent" else "home-mac:term-1")
            await jobs(pool)
            assert len(pool.screens) == 1 and session.cached_screen == ""
            sock = next(
                s for lease, _, s in scripted.streams.opened if lease == session.lease.lease_id
            )
            newer = b"newer output received while hidden"
            sock.queue(("bytes", terminal_wire.encode_output(session.lease.epoch, 0, newer)))
            await _wait(lambda: session.term_state.applied_offset == len(newer))
            before = len(bytes(session.output_log))
            await navigate(app, "terminals" if kind == "shell" else "console", width)
            await app.set_props_and_fire(
                "shell-home-mac:tab-1" if kind == "shell" else "agent-home-mac:term-1", {}, "click"
            )
            await jobs(pool)
            await app.settle()
            assert session.current_term is engine and session.lease.mode == "view"
            assert session.cached_screen == ""
            assert not app.find("terminal-cached-screen").visible
            assert len(bytes(session.output_log)) == before  # No subsequent byte needed.
        finally:
            await app.set_props_and_fire("test-unmount", {}, "click")

    run(scenario())


@pytest.mark.parametrize("width", [390, 1280])
def test_recreated_viewer_empty_live_frame_clears_preview_without_cursor_change(
    width: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        app, _controller, scripted, pool, _now = await _render(
            bridge=RouteBridge(width=width, height=844)
        )
        try:
            await _open(app, "home-mac:term-1")
            entry = pool.foreground
            snapshot_bridge(app, monkeypatch)
            await app.set_props_and_fire(
                "terminal-surface",
                {},
                "data_channel_open",
                {"channel_name": "terminal", "channel_id": 83},
            )
            await _open(app, "home-mac:term-2")
            await jobs(pool)
            await pool.discard(entry)
            await app.set_props_and_fire("agent-home-mac:term-1", {}, "click")
            await app.settle()
            session = pool.foreground.session
            assert session.cached_screen and session.term_state.applied_offset == 0
            socket = next(
                s for lease, _, s in scripted.streams.opened if lease == session.lease.lease_id
            )
            # An epoch-checked empty frame proves stream liveness, not native readiness.
            socket.queue(("bytes", terminal_wire.encode_output(session.lease.epoch, 0, b"")))
            await _wait(lambda: session.term_state.status == "live")
            await app.settle()
            assert session.cached_screen  # preserve the preview until the fresh engine opens
            fresh = _ProtocolMuxedDataChannel(84, app.conn)
            app.conn.data_channel_for = lambda _: fresh
            await app.session.dispatch_event(
                session.current_term._i,
                "data_channel_open",
                {"channel_name": "terminal", "channel_id": 84},
            )
            await app.settle()
            assert session.term_state.applied_offset == 0 and session.cached_screen == ""
            assert not app.find("terminal-cached-screen").visible
        finally:
            await app.set_props_and_fire("test-unmount", {}, "click")

    run(scenario())
