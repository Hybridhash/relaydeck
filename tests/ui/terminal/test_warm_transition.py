"""Barrier-driven takeover/recovery arbitration over the retained renderer."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from typing import Any, cast

import pytest
from flet.controls.context import _context_page

from relaydeck.client.features.inventory.routing import RouteBridge
from relaydeck.client.features.settings.prefs import PREFS_KEY
from relaydeck.client.features.shell.ops import (
    AttachmentInfo,
    LeaseInfo,
    ShellGatewayOps,
    ShellOpError,
)
from relaydeck.client.features.sync.transport import TransportError
from relaydeck.client.features.terminal.agent_session import AgentSession
from relaydeck.client.features.terminal.ui_resume import terminal_transition
from relaydeck.contracts.wire import terminal as terminal_wire
from tests.support.host_harness import run
from tests.support.scripted_sessions import ScriptedTerminalSocket
from tests.support.terminal.resume import _MuxConn, _report
from tests.support.terminal.retained_transport import capture, mounted, requests, resource, until
from tests.support.ui.instant_open import jobs
from tests.support.ui.packaged import Preferences
from tests.support.ui.retained_root import _open, _render


@pytest.mark.parametrize("automatic", [False, True])
@pytest.mark.parametrize("cancel_original", [False, True])
def test_cancelled_takeover_waiter_cannot_retire_another_callers_flight(
    automatic: bool, cancel_original: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        scripted, session, entry, resources, app, term, _ = await watching()
        entered, release, joined = asyncio.Event(), asyncio.Event(), asyncio.Event()
        original = cast(ShellGatewayOps, scripted.ops).acquire_lease
        attaches = len(scripted.ops.attaches)

        async def acquire(attachment_id: str, mode: str, **kwargs: Any) -> LeaseInfo:
            lease = await original(attachment_id, mode, **kwargs)
            if mode == "control":
                entered.set()
                await release.wait()
            return lease

        async def join() -> str:
            joined.set()
            return await cast(AgentSession, session).confirm_takeover()

        monkeypatch.setattr(scripted.ops, "acquire_lease", acquire)
        session.request_takeover()
        first = asyncio.create_task(
            session.auto_take_control() if automatic else session.confirm_takeover()
        )
        second = None
        try:
            await entered.wait()
            token = terminal_transition(session).control
            assert token is not None
            second = asyncio.create_task(join())
            await joined.wait()
            cancelled, survivor = (first, second) if cancel_original else (second, first)
            cancelled.cancel()
            with pytest.raises(asyncio.CancelledError):
                await cancelled
            assert terminal_transition(session).control is token
            assert session.takeover_busy and not survivor.done()
            release.set()
            assert await survivor == "live"
            assert session.lease.mode == "control"
            assert session.lease.lease_id not in scripted.ops.releases
            assert len(scripted.ops.attaches) == attaches + 1
            assert terminal_transition(session).control is None
            await until(lambda: not session.takeover_busy)
            assert session.current_term is term
            assert session.store.draft == "keep this draft"
            no_input(scripted)
        finally:
            release.set()
            await asyncio.gather(
                first, *([second] if second is not None else []), return_exceptions=True
            )
            await resources.close()

    run(scenario())


async def watching() -> tuple[Any, ...]:
    scripted, session, entry, resources, app, term, channel = await mounted("agent")
    _context_page.set(app.page)
    entry.task.cancel()
    await asyncio.gather(entry.task, return_exceptions=True)
    entry.supervise(session)
    resources.retained.release()
    await jobs(resources.retained)
    assert session.lease.mode == "view"
    await resources.retained.acquire("mac", "agent", "key", "e1", lambda: session)
    return scripted, session, entry, resources, app, term, channel


def no_input(scripted: Any) -> None:
    assert scripted.submitted == []
    assert not any(
        kind == "bytes" for _, _, sock in scripted.streams.opened for kind, _ in sock.sent
    )


@pytest.mark.parametrize("automatic", [False, True])
def test_pending_probe_cannot_remount_control_acquisition(
    automatic: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        scripted, session, entry, resources, app, term, _ = await watching()
        conn = _MuxConn()
        app.session.attach_connection(conn)
        entered, release = asyncio.Event(), asyncio.Event()
        original = scripted.ops.acquire_lease
        acquired = []

        async def acquire(attachment_id: str, mode: str, **kwargs: Any) -> Any:
            lease = await original(attachment_id, mode, **kwargs)
            if mode == "control":
                acquired.append(lease)
                entered.set()
                await release.wait()
            return lease

        monkeypatch.setattr(scripted.ops, "acquire_lease", acquire)
        monkeypatch.setattr("relaydeck.client.resources.OUTPUT_ACK_DEADLINE", 0)
        try:
            resources.resume_terminals(
                transport=object(), current=lambda: True, closed=asyncio.Event()
            )
            await until(lambda: requests(conn))
            request = requests(conn)[0][1]
            lifecycle = session.interaction_generation
            session.request_takeover()
            take = asyncio.create_task(
                session.auto_take_control() if automatic else session.confirm_takeover()
            )
            await entered.wait()
            assert terminal_transition(session).control is not None
            assert not resources.ui_resumes._active
            assert not await resources.retained.remount_ui(entry, 1, lambda: True)
            term.write(b"unacknowledged")
            resources._ui_output[id(entry)] = (term, 1, term._acked_bytes, 0.0, 1)
            conn.channels[7].deliver(_report(request, instance=999))
            await app.settle()
            assert resources._ui_output == {}
            assert session.current_term is term
            assert session.interaction_generation == lifecycle
            assert entry.ui_remount_generation == -1 and not entry.ui_recovering
            assert session.controller.key_generation() is None
            release.set()
            assert await take == "live"
            assert session.lease is acquired[0]
            assert acquired[0].lease_id not in scripted.ops.releases
            await until(lambda: len(requests(conn)) == 2)
            conn.channels[7].deliver(_report(request, instance=999))
            await app.settle()
            assert session.lease is acquired[0] and not session.controller.writer_revoked
            assert session.controller.key_generation() is None
            conn.channels[7].deliver(
                _report(requests(conn)[-1][1], instance=123, resets=term._reset_count)
            )
            await until(lambda: session.terminal_live)
            assert len(requests(conn)) == 2
            assert terminal_transition(session).control is None
            assert session.store.draft == "keep this draft"
            no_input(scripted)
        finally:
            release.set()
            await resources.close()

    run(scenario())


def test_claim_before_foreground_preserves_verified_quiet_viewer() -> None:
    async def scenario() -> None:
        scripted, session, entry, resources, app, term, _ = await watching()
        conn = _MuxConn()
        app.session.attach_connection(conn)
        transport = object()
        try:
            resources.resume_terminals(
                transport=transport, current=lambda: True, closed=asyncio.Event()
            )
            await until(lambda: requests(conn))
            conn.channels[7].deliver(_report(requests(conn)[-1][1], instance=123))
            await until(lambda: term.transport_verified)
            resources.retained.release()
            await jobs(resources.retained)
            before = len(requests(conn)), term._reset_count
            tokens = []

            def claim(active: Any) -> None:
                tokens.append(active.begin_control_transition())

            await resources.retained.acquire(
                "mac", "agent", "key", "e1", lambda: session, before_foreground=claim
            )
            assert terminal_transition(session).control is tokens[0]
            assert len(requests(conn)) == before[0]
            assert await session.auto_take_control() == "live"
            session.end_control_transition(tokens[0])
            await until(lambda: len(requests(conn)) == before[0] + 1)
            controller = session.controller
            attempt = resources.ui_resumes._active[id(term)]
            assert attempt.controller is controller
            assert attempt.stream_epoch == session.lease.epoch
            assert attempt.stream_epoch and controller.state.status == "live"
            assert controller.emulator == "preserved"
            assert term._reset_count == before[1]
            conn.channels[7].deliver(_report(requests(conn)[-1][1], instance=123))
            await until(lambda: session.terminal_live)
            await app.settle()
            assert len(requests(conn)) == before[0] + 1
            assert session.current_term is term
            assert terminal_transition(session).control is None
            scripted.streams.opened[-1][2].queue(
                ("bytes", terminal_wire.encode_output(session.lease.epoch, 0, b"")),
                ("bytes", terminal_wire.encode_output(session.lease.epoch, 0, b"x")),
            )
            await until(lambda: controller.state.applied_offset == 1)
            assert len(requests(conn)) == before[0] + 1
            no_input(scripted)
        finally:
            await resources.close()

    run(scenario())


@pytest.mark.parametrize("failed", [False, True])
def test_takeover_waits_for_owned_remount_and_ignores_its_late_failure(
    failed: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        scripted, session, entry, resources, app, term, _ = await watching()
        conn = _MuxConn()
        app.session.attach_connection(conn)
        entered, release = asyncio.Event(), asyncio.Event()
        original = cast(ShellGatewayOps, scripted.ops).terminal_ticket
        old_lease = session.lease

        async def ticket(lease_id: str) -> str:
            if lease_id == old_lease.lease_id:
                entered.set()
                await release.wait()
                if failed:
                    raise ShellOpError("lease_expired", "synthetic old recovery failure")
            return await original(lease_id)

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
            session.request_takeover()
            attaches = len(scripted.ops.attaches)
            take = asyncio.create_task(session.confirm_takeover())
            await until(lambda: terminal_transition(session).control is not None)
            assert entry.ui_recovering and not take.done()
            assert len(scripted.ops.attaches) == attaches
            release.set()
            assert await take == "live"
            winner, lease = session.controller, session.lease
            assert not entry.ui_recovering
            assert terminal_transition(session).recovery is None
            assert terminal_transition(session).control is None
            assert lease.mode == "control" and lease.lease_id not in scripted.ops.releases
            await app.settle()
            assert session.controller is winner and session.lease is lease
            assert not winner.writer_revoked and session.phase == "live"
            assert session.current_term is fresh
            no_input(scripted)
        finally:
            release.set()
            await resources.close()

    run(scenario())


@pytest.mark.parametrize("stage", ["ticket", "socket"])
def test_remount_deadline_releases_waiting_takeover_and_fences_failed_viewer(
    stage: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        scripted, session, entry, resources, app, term, _ = await watching()
        conn = _MuxConn()
        app.session.attach_connection(conn)
        entered, dependency_release, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()
        attach_entered, attach_release = asyncio.Event(), asyncio.Event()
        controller, lease = session.controller, session.lease
        original_timeout = asyncio.timeout
        deadline = asyncio.Timeout(None)
        original_ticket = cast(ShellGatewayOps, scripted.ops).terminal_ticket
        original_connect = ScriptedTerminalSocket.connect
        original_attach = cast(ShellGatewayOps, scripted.ops).attach

        def timeout(delay: float | None) -> asyncio.Timeout:
            return deadline if delay == 123.0 else original_timeout(delay)

        async def block() -> None:
            entered.set()
            try:
                await dependency_release.wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

        async def ticket(lease_id: str) -> str:
            if lease_id == lease.lease_id:
                await block()
            return await original_ticket(lease_id)

        async def connect(value: ScriptedTerminalSocket) -> None:
            await original_connect(value)
            if any(
                lease_id == lease.lease_id and sock is value
                for lease_id, _, sock in scripted.streams.opened
            ):
                await block()

        async def attach(
            host_id: str, target: str, kind: str, takeover: bool, cols: int, rows: int
        ) -> AttachmentInfo:
            attach_entered.set()
            await attach_release.wait()
            return await original_attach(host_id, target, kind, takeover, cols, rows)

        monkeypatch.setattr(asyncio, "timeout", timeout)
        if stage == "ticket":
            monkeypatch.setattr(scripted.ops, "terminal_ticket", ticket)
        else:
            monkeypatch.setattr(ScriptedTerminalSocket, "connect", connect)
        monkeypatch.setattr(scripted.ops, "attach", attach)
        remount = asyncio.create_task(
            resources.retained.remount_ui(entry, 1, lambda: True, timeout=123.0)
        )
        take = None
        try:
            await until(
                lambda: session.current_term is not None and session.current_term is not term
            )
            fresh = session.current_term
            await capture(app, fresh, conn, 8, instance=456)
            await until(entered.is_set)
            session.request_takeover()
            take = asyncio.create_task(session.confirm_takeover())
            await until(lambda: terminal_transition(session).control is not None)
            assert entry.ui_recovering and not take.done()
            assert not attach_entered.is_set()
            deadline.reschedule(asyncio.get_running_loop().time())
            assert not await remount
            await until(attach_entered.is_set)
            assert cancelled.is_set() and deadline.expired()
            assert terminal_transition(session).recovery is None
            assert not entry.ui_recovering
            assert session.phase == "disconnected" and session.notice
            assert session.controller is None and session.input_token is None
            assert controller.key_generation() is None and not fresh.transport_verified
            attach_release.set()
            assert await take == "live"
            assert session.lease.mode == "control"
            assert session.lease.lease_id not in scripted.ops.releases
            assert terminal_transition(session).control is None
            assert session.current_term is fresh
            assert session.store.draft == "keep this draft"
            no_input(scripted)
        finally:
            dependency_release.set()
            attach_release.set()
            if not remount.done():
                remount.cancel()
            if take is not None and not take.done():
                session.cancel_takeover()
                take.cancel()
            await asyncio.gather(
                remount, *([take] if take is not None else []), return_exceptions=True
            )
            await resources.close()

    run(scenario())


@pytest.mark.parametrize("stage", ["attach", "acquire", "ticket", "socket"])
@pytest.mark.parametrize(
    "invalidation", ["leave", "revoke", "pause", "dispose", "expiry", "replace", "cancel"]
)
def test_held_takeover_cannot_cross_lifecycle_or_lease_loss(
    stage: str, invalidation: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        scripted, session, entry, resources, app, term, _ = await watching()
        entered, release = asyncio.Event(), asyncio.Event()
        result: list[object] = []
        foreign_lease = None
        original_attach = scripted.ops.attach
        original_acquire = scripted.ops.acquire_lease
        original_ticket = cast(ShellGatewayOps, scripted.ops).terminal_ticket
        original_socket = session._socket_factory

        async def hold[T](value: T) -> T:
            result.append(value)
            entered.set()
            await release.wait()
            return value

        if stage == "attach":

            async def attach(*args: Any, **kwargs: Any) -> Any:
                return await hold(await original_attach(*args, **kwargs))

            monkeypatch.setattr(scripted.ops, "attach", attach)
        elif stage == "acquire":

            async def acquire(attachment_id: str, mode: str, **kwargs: Any) -> Any:
                value = await original_acquire(attachment_id, mode, **kwargs)
                return await hold(value) if mode == "control" else value

            monkeypatch.setattr(scripted.ops, "acquire_lease", acquire)
        elif stage == "ticket":

            async def ticket(lease_id: str) -> str:
                return await hold(await original_ticket(lease_id))

            monkeypatch.setattr(scripted.ops, "terminal_ticket", ticket)
        else:

            def socket(ticket: str) -> Any:
                value = original_socket(ticket)
                connect = value.connect

                async def connecting() -> None:
                    await connect()
                    await hold(value)

                value.connect = connecting
                return value

            monkeypatch.setattr(session, "_socket_factory", socket)
        session.request_takeover()
        take = asyncio.create_task(session.confirm_takeover())
        try:
            await entered.wait()
            assert session.controller.key_generation() is None
            own_lease = cast(LeaseInfo, result[0]) if stage == "acquire" else session.lease
            if invalidation == "leave":
                resources.retained.release()
            elif invalidation == "revoke":
                resources.retained.invalidate("mac")
                session._on_auth_revoked()
            elif invalidation == "pause":
                await session.pause()
            elif invalidation == "dispose":
                await resources.retained.discard(entry)
            elif invalidation == "cancel":
                session.cancel_takeover()
                take.cancel()
                await asyncio.gather(take, return_exceptions=True)
            else:
                if invalidation == "replace":
                    device = scripted.ops.device_id
                    scripted.ops.device_id = "other-device"
                    try:
                        other = await original_attach("mac", session.target, "agent", True, 80, 24)
                        foreign_lease = await original_acquire(other.attachment_id, "control")
                    finally:
                        scripted.ops.device_id = device
                session._lease_owner._hooks.on_terminal("lease_expired")
            assert terminal_transition(session).control is None
            release.set()
            if invalidation == "cancel":
                await until(lambda: not session.takeover_busy)
            else:
                await take
            if stage != "attach":
                assert own_lease.lease_id in scripted.ops.releases
            if foreign_lease is not None:
                assert foreign_lease.lease_id not in scripted.ops.releases
                assert foreign_lease.lease_id in scripted.ops._live_leases
            assert session.current_term is term or invalidation == "dispose"
            assert not session.controller or session.controller.key_generation() is None
            no_input(scripted)
        finally:
            release.set()
            await resources.close()

    run(scenario())


@pytest.mark.parametrize(
    "outcome", ["conflict", "unknown", "attach", "acquire", "ticket", "socket", "cancel"]
)
def test_auto_takeover_releases_intent_on_all_outcomes(
    outcome: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        scripted, session, entry, resources, app, term, _ = await watching()
        entered, release = asyncio.Event(), asyncio.Event()
        original_holder = scripted.ops.control_holder

        async def holder(host: str, terminal: str) -> Any:
            if outcome == "unknown":
                raise ShellOpError("unsupported_capability", "synthetic unknown")
            if outcome == "conflict":
                return {"held": True, "mine": False, "holder": {"device_id": "other"}}
            if outcome == "cancel":
                entered.set()
                await release.wait()
            return await original_holder(host, terminal)

        async def failure(*args: Any, **kwargs: Any) -> Any:
            raise ShellOpError("host_offline", "synthetic failure")

        monkeypatch.setattr(scripted.ops, "control_holder", holder)
        if outcome in ("attach", "acquire", "ticket"):
            monkeypatch.setattr(
                scripted.ops,
                {"attach": "attach", "acquire": "acquire_lease", "ticket": "terminal_ticket"}[
                    outcome
                ],
                failure,
            )
        elif outcome == "socket":
            factory = session._socket_factory

            def socket(ticket: str) -> Any:
                value = factory(ticket)

                async def connect() -> None:
                    raise TransportError("host_offline", "synthetic socket refusal", False)

                value.connect = connect
                return value

            monkeypatch.setattr(session, "_socket_factory", socket)
        take = asyncio.create_task(session.auto_take_control())
        try:
            if outcome == "cancel":
                await entered.wait()
                take.cancel()
                await asyncio.gather(take, return_exceptions=True)
            else:
                await take
            assert terminal_transition(session).control is None
            assert not session.takeover_busy and not entry.ui_recovering
            conn = _MuxConn()
            app.session.attach_connection(conn)
            resources.resume_terminals(
                transport=object(), current=lambda: True, closed=asyncio.Event()
            )
            await until(lambda: requests(conn))
            assert resources.ui_resumes._active[id(term)].controller is session.controller
            no_input(scripted)
        finally:
            release.set()
            await resources.close()

    run(scenario())


@pytest.mark.parametrize("automatic", [False, True])
@pytest.mark.auto_takeover
def test_root_warm_open_claims_intent_only_when_automatic_control_is_enabled(
    automatic: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        prefs = Preferences({PREFS_KEY: json.dumps({"auto_takeover": automatic})})
        app, _, scripted, pool, _ = await _render(
            preferences=prefs, bridge=RouteBridge(width=390, height=844)
        )
        resources = resource(pool)
        entered, release = asyncio.Event(), asyncio.Event()
        try:
            if automatic:
                await app.set_props_and_fire("agent-home-mac:term-1", {}, "click")
                await until(lambda: pool.foreground is not None and pool.foreground.session.lease)
            else:
                await _open(app, "home-mac:term-1")
            entry = pool.foreground
            session, term = entry.session, entry.session.current_term
            conn = _MuxConn()
            app.session.attach_connection(conn)
            await capture(app, term, conn, 7)
            resources.resume_terminals(
                transport=object(), current=lambda: True, closed=asyncio.Event()
            )
            await until(lambda: requests(conn))
            conn.channels[7].deliver(_report(requests(conn)[-1][1], instance=123))
            await until(lambda: term.transport_verified)
            await app.set_props_and_fire("terminal-back", {}, "click")
            await jobs(pool)
            assert session.lease.mode == "view"
            events = []
            session.subscribe(
                lambda _: events.append((session.foreground, terminal_transition(session).control))
            )
            original = scripted.ops.acquire_lease

            async def acquire(attachment_id: str, mode: str, **kwargs: Any) -> Any:
                value = await original(attachment_id, mode, **kwargs)
                if mode == "control":
                    entered.set()
                    await release.wait()
                return value

            monkeypatch.setattr(scripted.ops, "acquire_lease", acquire)
            probes, attaches = len(requests(conn)), len(scripted.ops.attaches)
            await app.set_props_and_fire("agent-home-mac:term-1", {}, "click")
            if automatic:
                await asyncio.wait_for(entered.wait(), 2)
                assert all(token is not None for foreground, token in events if foreground)
                assert len(requests(conn)) == probes
                release.set()
                await until(lambda: terminal_transition(session).control is None)
                assert session.lease.mode == "control"
                assert len(scripted.ops.attaches) == attaches + 1
            else:
                assert not entered.is_set()
                assert session.lease.mode == "view"
                assert len(scripted.ops.attaches) == attaches
                assert terminal_transition(session).control is None
            await until(lambda: len(requests(conn)) == probes + 1)
            assert pool.foreground is entry and session.current_term is term
            no_input(scripted)
        finally:
            release.set()
            await resources.close()

    run(scenario())


@pytest.mark.parametrize("boundary", ["epoch", "reset", "native"])
def test_settled_probe_keeps_real_stream_and_viewer_boundaries_fenced(boundary: str) -> None:
    async def scenario() -> None:
        scripted, session, entry, resources, app, term, _ = await watching()
        conn = _MuxConn()
        app.session.attach_connection(conn)
        try:
            token = session.begin_control_transition()
            resources.resume_terminals(
                transport=object(), current=lambda: True, closed=asyncio.Event()
            )
            assert await session.auto_take_control() == "live"
            session.end_control_transition(token)
            await until(lambda: requests(conn))
            request = requests(conn)[-1][1]
            controller = session.controller
            if boundary == "epoch":
                controller._set(replace(controller.state, epoch="different"))
            elif boundary == "reset":
                term.reset()
                session._notify()
            else:
                term._native_instance = 999
                session._notify()
            conn.channels[7].deliver(_report(request, instance=123))
            await app.settle()
            assert controller.key_generation() is None
            assert not term.transport_verified
            assert session.current_term is term
            no_input(scripted)
        finally:
            await resources.close()

    run(scenario())


def test_old_holder_query_finishing_cannot_release_pending_explicit_intent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        scripted, session, entry, resources, app, term, _ = await watching()
        holder_entered, holder_release = asyncio.Event(), asyncio.Event()
        acquired, control_release = asyncio.Event(), asyncio.Event()
        original = scripted.ops.acquire_lease

        async def holder(*args: Any) -> Any:
            holder_entered.set()
            await holder_release.wait()
            raise ShellOpError("unsupported_capability", "synthetic unknown holder")

        async def acquire(attachment: str, mode: str, **kwargs: Any) -> Any:
            lease = await original(attachment, mode, **kwargs)
            if mode == "control":
                acquired.set()
                await control_release.wait()
            return lease

        monkeypatch.setattr(scripted.ops, "control_holder", holder)
        monkeypatch.setattr(scripted.ops, "acquire_lease", acquire)
        automatic = asyncio.create_task(session.auto_take_control())
        try:
            await holder_entered.wait()
            session.request_takeover()
            explicit = asyncio.create_task(session.confirm_takeover())
            await acquired.wait()
            token = terminal_transition(session).control
            holder_release.set()
            await automatic
            assert terminal_transition(session).control is token and token is not None
            assert not await resources.retained.remount_ui(entry, 1, lambda: True)
            control_release.set()
            assert await explicit == "live"
            assert terminal_transition(session).control is None
            assert session.lease.mode == "control"
            assert session.lease.lease_id not in scripted.ops.releases
            no_input(scripted)
        finally:
            holder_release.set()
            control_release.set()
            await resources.close()

    run(scenario())


def test_paused_reacquisition_keeps_reserved_intent_until_validated_resume() -> None:
    async def scenario() -> None:
        scripted, session, entry, resources, app, term, _ = await watching()
        tokens = []
        try:
            resources.retained.release()
            await jobs(resources.retained)
            await session.pause()

            def reserve(active: Any) -> None:
                tokens.append(active.begin_control_transition())

            await resources.retained.acquire(
                "mac", "agent", "key", "e1", lambda: session, before_foreground=reserve
            )
            assert terminal_transition(session).control is tokens[0]
            assert session.paused and session.lease is None
            assert await resources.retained.resume_watch(entry)
            assert terminal_transition(session).control is tokens[0]
            assert await session.auto_take_control() == "live"
            session.end_control_transition(tokens[0])
            assert terminal_transition(session).control is None
            assert session.current_term is term
            assert session.store.draft == "keep this draft"
            no_input(scripted)
        finally:
            await resources.close()

    run(scenario())


def test_only_current_renewal_failure_retires_takeover_intent() -> None:
    async def scenario() -> None:
        scripted, session, entry, resources, app, term, _ = await watching()
        try:
            retired = session._renewal
            assert retired is not None
            token = session.begin_control_transition()
            session.start_lease_renewal(3600)
            current = session._renewal
            assert current is not None and current is not retired
            retired._fail_terminal("lease_expired")
            assert terminal_transition(session).control is token
            assert not session.controller.writer_revoked
            current._fail_terminal("lease_expired")
            assert terminal_transition(session).control is None
            assert session.controller.writer_revoked
            assert session.current_term is term
            assert session.store.draft == "keep this draft"
            no_input(scripted)
        finally:
            await resources.close()

    run(scenario())
