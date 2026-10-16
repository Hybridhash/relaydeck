"""Authenticated transport replacement across retained/background lifetimes.

Actual Flet dispatch/control reconciliation; synthetic native reports and PTY.
These checks do not claim iPhone paint/keyboard proof.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any

import pytest
from relaydeck_terminal import protocol
from relaydeck_terminal.terminal import TerminalOverflowError

from relaydeck.client.features.inventory.routing import RouteBridge
from relaydeck.client.features.sync.transport import TransportError
from relaydeck.client.features.terminal.foreground_recovery import ForegroundRecovery
from relaydeck.gateway import terminal_wire
from tests.support.host_harness import run
from tests.support.terminal.resume import _MuxConn, _open_term, _report, _write_payloads
from tests.support.terminal.retained_transport import capture, mounted, requests, resource, until
from tests.support.ui.instant_inventory import navigate, valid_inventory
from tests.support.ui.instant_open import jobs
from tests.support.ui.navigation import _StubTransport
from tests.support.ui.retained_root import _open, _render


@pytest.mark.parametrize("kind", ["agent", "shell"])
def test_actual_root_hidden_background_reconnect_then_cached_card_reacquisition(
    kind: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_StubTransport, "snapshot", valid_inventory)

    async def unavailable(*args: Any, **kwargs: Any) -> Any:
        raise TransportError("usage_disabled", "synthetic unavailable", False)

    monkeypatch.setattr(_StubTransport, "usage", unavailable, raising=False)

    async def scenario() -> None:
        app, _metadata, scripted, pool, _clock = await _render(
            bridge=RouteBridge(width=390, height=844)
        )
        resources = resource(pool)
        try:
            if kind == "agent":
                await _open(app, "home-mac:term-1")
                entry = pool.foreground
                entry.session.on_draft_change("exact unsent\ndraft")
            else:
                await navigate(app, "terminals", 390)
                await app.set_props_and_fire("shell-home-mac:tab-1", {}, "click")
                await until(
                    lambda: pool.foreground is not None and pool.foreground.session.phase == "live"
                )
                entry = pool.foreground
            session, term = entry.session, entry.session.current_term
            old_conn = _MuxConn()
            app.session.attach_connection(old_conn)
            old = await capture(app, term, old_conn, 7)
            socket = next(
                sock
                for lease, _, sock in scripted.streams.opened
                if lease == session.lease.lease_id
            )
            socket.queue(
                ("bytes", terminal_wire.encode_output(session.lease.epoch, 0, b"old screen"))
            )
            await until(lambda: term._sent_bytes == 10)
            old.deliver(protocol.encode_ack(10))
            await app.settle()
            await app.set_props_and_fire(
                "terminal-back" if kind == "agent" else "shell-back", {}, "click"
            )
            await jobs(pool)
            await app.set_props_and_fire("test-background", {}, "click")
            await jobs(pool)
            assert session.paused and session.lease is None
            replacement = _MuxConn()
            app.session.attach_connection(replacement)
            resources.resume_terminals(
                transport=object(), current=lambda: True, closed=asyncio.Event()
            )
            assert resources.ui_transport_generation == 1
            assert old.closed and term._channel is replacement.channels[7]
            assert not term.transport_verified and not session.terminal_live
            assert not requests(replacement)  # Paused/controller-less is deferred, not forgotten.
            await app.set_props_and_fire("test-foreground", {}, "click")
            await jobs(pool)
            card = "agent-home-mac:term-1" if kind == "agent" else "shell-home-mac:tab-1"
            await app.set_props_and_fire(card, {}, "click")
            await until(lambda: requests(replacement))
            number, request = requests(replacement)[-1]
            replacement.channels[number].deliver(
                _report(request, instance=123, applied=10, resets=term._reset_count)
            )
            await until(lambda: term.transport_verified)
            assert pool.foreground is entry and session.current_term is term
            assert session.controller.key_generation() is None  # Reopen is VIEW only.
            assert session.lease.mode == "view"
            assert len(scripted.ops.attaches) == 1
            assert not any(
                kind == "bytes" for _, _, sock in scripted.streams.opened for kind, _ in sock.sent
            )
            assert not any(packet[0] == protocol.WRITE for _, packet in replacement.data_frames)
            if kind == "agent":
                assert session.store.draft == "exact unsent\ndraft"
        finally:
            await resources.close()

    run(scenario())


def test_stale_binding_is_not_ready_and_rejects_output_without_counting_or_dispatching() -> None:
    async def scenario() -> None:
        app, term, conn, old = await _open_term()
        received: list[bytes] = []
        term.add_input_listener(lambda _, data: received.append(data))
        queued = old.handler
        term.write(b"delivered")
        before = term._sent_bytes, term._journal_bytes, len(conn.data_frames)
        app.session.attach_connection(_MuxConn())
        assert not term.channel_ready and not term.transport_verified
        with pytest.raises(TerminalOverflowError, match="transport is stale"):
            term.write(b"must not disappear")
        queued(protocol.encode_input(b"stale input", protocol.ORIGIN_TEXT))
        await app.settle()
        assert received == []
        assert (term._sent_bytes, term._journal_bytes, len(conn.data_frames)) == before
        term.rebind_transport()
        assert term.channel_ready and term.resume_unverified

    run(scenario())


@pytest.mark.parametrize("kind", ["agent", "shell"])
@pytest.mark.parametrize("failure", ["timeout", "instance"])
def test_probe_failure_replaces_only_native_control_and_recovers_authoritative_baseline(
    kind: str,
    failure: str,
) -> None:
    async def scenario() -> None:
        scripted, session, entry, resources, app, term, old = await mounted(kind)
        original_lease, controller = session.lease, session.controller
        controller.emulator = "preserved"
        term.write(b"stale glyph fragments")
        replacement = _MuxConn()
        app.session.attach_connection(replacement)
        parent = app.find("terminal-engine-slot")._i
        try:
            resources.resume_terminals(
                transport=object(), current=lambda: True, closed=asyncio.Event()
            )
            await until(lambda: requests(replacement))
            if failure == "instance":
                replacement.channels[7].deliver(_report(requests(replacement)[0][1], instance=999))
            await until(
                lambda: session.current_term is not None and session.current_term is not term
            )
            fresh = session.current_term
            assert fresh.key == "terminal-surface-mount-1"
            assert app.find("terminal-engine-slot")._i == parent
            await capture(app, fresh, replacement, 8, instance=456)
            await until(lambda: len(scripted.streams.opened) == 2)
            assert session.lease is original_lease and session.controller is controller
            assert controller._client.resume_offset() == 0 and not controller._client.epoch
            # The reopen names its position: the gateway holds the lease for this reader only.
            assert scripted.streams.resumes == ["", "epoch=&offset=0"]
            assert len(scripted.ops.attaches) == len(scripted.ops.leases) == 1
            assert fresh._sent_bytes == 0 and fresh._native_instance == 456
            assert term._remount is None and not fresh.transport_verified
            assert not term.channel_ready
            replay = scripted.streams.opened[-1][2]
            replay.queue(
                ("bytes", terminal_wire.encode_output(original_lease.epoch, 0, b"fresh baseline"))
            )
            await until(lambda: fresh._sent_bytes == 14)
            await until(lambda: requests(replacement)[-1] == (8, fresh._resume_next_id))
            request = requests(replacement)[-1][1]
            replacement.channels[8].deliver(_report(request, instance=456, applied=14))
            await until(lambda: fresh.transport_verified)
            # Post-frame VIEW_REPORT and viewport are recorded separately from write ACK.
            replacement.channels[8].deliver(
                protocol.encode_view_report(
                    protocol.ViewState(alt=False, at_bottom=True, mouse=False)
                )
            )
            replacement.channels[8].deliver(protocol.encode_size_report(40, 20))
            await app.settle()
            assert fresh._view_report_count == 1 and fresh._last_view_size == (40, 20)
            writes = [(n, p) for n, p in replacement.data_frames if p[0] == protocol.WRITE]
            assert _write_payloads(writes) == [b"fresh baseline"]
            assert session.terminal_live
            if kind == "agent":
                assert session.store.draft == "keep this draft"
            assert not any(
                kind == "bytes" for _, _, sock in scripted.streams.opened for kind, _ in sock.sent
            )
            assert not await resources.retained.remount_ui(entry, 1, lambda: True)
        finally:
            await resources.close()

    run(scenario())


@pytest.mark.parametrize("transition", ["leave", "background", "transport"])
def test_obsolete_probe_cannot_enable_input_or_remount_a_replacement(transition: str) -> None:
    async def scenario() -> None:
        scripted, session, entry, resources, app, term, old = await mounted("agent")
        replacement = _MuxConn()
        app.session.attach_connection(replacement)
        try:
            resources.resume_terminals(
                transport=object(), current=lambda: True, closed=asyncio.Event()
            )
            await until(lambda: requests(replacement))
            request = requests(replacement)[0][1]
            if transition == "leave":
                resources.retained.release()
            elif transition == "background":
                resources.retained.set_device_foreground(False)
            else:
                app.session.attach_connection(_MuxConn())
                resources.resume_terminals(
                    transport=object(), current=lambda: True, closed=asyncio.Event()
                )
            replacement.channels[7].deliver(_report(request, instance=123))
            await app.settle()
            assert not any(p[0] == protocol.RESUME_VERIFIED for _, p in replacement.data_frames)
            assert session.current_term is term and entry.ui_remount_generation == -1
            assert not term.transport_verified
            assert scripted.submitted == []
        finally:
            await resources.close()

    run(scenario())


@pytest.mark.parametrize("kind", ["agent", "shell"])
def test_visible_output_deadline_probes_but_quiet_hidden_and_acknowledged_views_do_not(
    kind: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("relaydeck.client.resources.OUTPUT_ACK_DEADLINE", 0.04)

    async def scenario() -> None:
        scripted, session, entry, resources, app, term, old = await mounted(kind)
        conn = _MuxConn()
        app.session.attach_connection(conn)
        try:
            resources.resume_terminals(
                transport=object(), current=lambda: True, closed=asyncio.Event()
            )
            await until(lambda: requests(conn))
            conn.channels[7].deliver(_report(requests(conn)[-1][1], instance=123))
            await until(lambda: term.transport_verified)
            await asyncio.sleep(0.3)
            assert len(requests(conn)) == 1  # quiet is healthy
            term.write(b"outstanding")
            await until(lambda: len(requests(conn)) == 2)
            assert not session.terminal_live and session.controller.key_generation() is None
            conn.channels[7].deliver(_report(requests(conn)[-1][1], instance=123, applied=11))
            await until(lambda: term.transport_verified)
            await asyncio.sleep(0.3)
            assert len(requests(conn)) == 2 and entry.ui_remount_generation == -1
            resources.retained.device_foreground = False
            term.write(b"hidden")
            await asyncio.sleep(0.3)
            assert len(requests(conn)) == 2 and entry.ui_remount_generation == -1
            assert len(scripted.ops.attaches) == len(scripted.ops.leases) == 1
        finally:
            await resources.close()

    run(scenario())


@pytest.mark.parametrize("kind", ["agent", "shell"])
def test_nonprogressing_output_gets_one_bounded_remount_not_endless_live_probes(
    kind: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("relaydeck.client.resources.OUTPUT_ACK_DEADLINE", 0.04)
    # Isolates the bounded remount; the reconnect that follows has its own tests.
    monkeypatch.setattr(ForegroundRecovery, "note", lambda self, entry: None)

    async def scenario() -> None:
        scripted, session, entry, resources, app, term, old = await mounted(kind)
        conn = _MuxConn()
        app.session.attach_connection(conn)
        try:
            resources.resume_terminals(
                transport=object(), current=lambda: True, closed=asyncio.Event()
            )
            await until(lambda: requests(conn))
            conn.channels[7].deliver(_report(requests(conn)[-1][1], instance=123))
            await until(lambda: term.transport_verified)
            term.write(b"no frame progress")
            await until(lambda: len(requests(conn)) == 2)
            conn.channels[7].deliver(_report(requests(conn)[-1][1], instance=123, applied=0))
            await until(lambda: entry.ui_remount_generation == 1)
            await until(
                lambda: session.current_term is not None and session.current_term is not term
            )
            # The fresh native mount never opens: bounded fallback becomes explicit recovery.
            await until(lambda: session.controller is None)
            assert len(requests(conn)) == 2 and not session.terminal_live
            assert not await resources.retained.remount_ui(entry, 1, lambda: True)
            assert len(scripted.ops.attaches) == len(scripted.ops.leases) == 1
        finally:
            await resources.close()

    run(scenario())


@pytest.mark.parametrize("kind", ["agent", "shell"])
def test_controller_readiness_reprobes_same_epoch_before_enabling_writer(kind: str) -> None:
    async def scenario() -> None:
        scripted, session, entry, resources, app, term, old = await mounted(kind)
        controller = session.controller
        controller._set(replace(controller.state, status="reconnecting"))
        conn = _MuxConn()
        app.session.attach_connection(conn)
        try:
            resources.resume_terminals(
                transport=object(), current=lambda: True, closed=asyncio.Event()
            )
            await until(lambda: requests(conn))
            conn.channels[7].deliver(_report(requests(conn)[-1][1], instance=123))
            await until(lambda: term.transport_verified)
            assert controller.ui_input_fenced and not session.terminal_live
            controller._set(replace(controller.state, status="live"))  # unchanged stream epoch
            await until(lambda: len(requests(conn)) == 2)
            assert controller.key_generation() is None
            conn.channels[7].deliver(_report(requests(conn)[-1][1], instance=123))
            await until(lambda: not controller.ui_input_fenced)
            assert session.terminal_live and controller.key_generation() is not None
        finally:
            await resources.close()

    run(scenario())


@pytest.mark.parametrize("kind", ["agent", "shell"])
def test_delayed_remount_recovery_cannot_revoke_a_takeover_successor(
    kind: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        scripted, session, entry, resources, app, term, old = await mounted(kind)
        conn = _MuxConn()
        app.session.attach_connection(conn)
        entered, release = asyncio.Event(), asyncio.Event()
        original_ticket = scripted.ops.terminal_ticket
        count = 0

        async def ticket(lease_id: str) -> str:
            nonlocal count
            count += 1
            if count == 1:
                entered.set()
                await release.wait()
            return await original_ticket(lease_id)

        monkeypatch.setattr(scripted.ops, "terminal_ticket", ticket)
        try:
            resources.resume_terminals(
                transport=object(), current=lambda: True, closed=asyncio.Event()
            )
            await until(lambda: requests(conn))
            conn.channels[7].deliver(_report(requests(conn)[0][1], instance=999))
            await until(
                lambda: session.current_term is not None and session.current_term is not term
            )
            fresh = session.current_term
            await capture(app, fresh, conn, 8, instance=456)
            await entered.wait()
            if kind == "agent":
                session.request_takeover()
                await session.confirm_takeover()
            else:
                await session.take_over()
            winner, winner_lease = session.controller, session.lease
            opened = len(scripted.streams.opened)
            release.set()
            await until(lambda: not entry.ui_recovering)
            assert session.controller is winner and session.lease is winner_lease
            assert not winner.writer_revoked and session.phase == "live"
            assert len(scripted.streams.opened) == opened  # stale ticket cannot close/reopen winner
            assert session.current_term is fresh and scripted.submitted == []
        finally:
            release.set()
            await resources.close()

    run(scenario())
