"""Session-owned recovery budgets fence the production reopen paths."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from typing import Any

import httpx
import pytest

from relaydeck.client.features.composer.domain import SUBMISSION_SENDING
from relaydeck.client.features.shell.ops import ShellOpError
from relaydeck.client.features.sync.controller import SyncController
from relaydeck.client.features.sync.transport import HttpxGatewayTransport
from relaydeck.client.features.terminal.controller import TerminalController
from relaydeck.client.features.terminal.session import recovery_state as budget_module
from relaydeck.client.features.terminal.session.recovery_state import GAP_EXHAUSTED_NOTICE
from relaydeck.client.features.terminal.surfaces.sessions import drive_recovery
from relaydeck.client.resources import AppResources
from relaydeck.domain.models import AppState, Host, Inventory
from tests.support.foreground_recovery import Harness, run_recovery
from tests.support.scripted_sessions import ScriptedTerminalSocket
from tests.support.terminal.watch_drops import until


def gap(session: Any, detail: str) -> None:
    session.controller._handle_gap(detail)
    session._classify_run_exit()


async def reopen(h: Harness, path: str) -> str:
    session = h.entry.session
    if path == "auto_take":
        return await session.auto_take_control()
    if path == "reload":
        return await session.recover_after_reload()
    task = h.recovery.request(h.entry, "loss")
    return "exhausted" if task is None else await task


def expire_waits(monkeypatch: pytest.MonkeyPatch, gate: asyncio.Event | None = None) -> list[float]:
    delays: list[float] = []
    original = asyncio.wait_for

    async def elapsed(work: Any, delay: float) -> Any:
        if getattr(getattr(work, "cr_code", None), "co_name", None) == "wait":
            delays.append(delay)
            work.close()
            if gate is not None:
                await gate.wait()
            raise TimeoutError
        return await original(work, delay)

    monkeypatch.setattr(budget_module.asyncio, "wait_for", elapsed)
    return delays


@pytest.mark.parametrize("path", ["auto_take", "reload", "foreground"])
@pytest.mark.parametrize("wake", ["return", "cap"])
def test_episode_a_reopen_paths_wait_before_gateway_writes(
    monkeypatch: pytest.MonkeyPatch, path: str, wake: str
) -> None:
    async def body(h: Harness) -> None:
        entry = await h.open()
        session = entry.session
        session.auto_take_allowed = True
        session._store.replace_draft("unsent")
        gap(session, "host_offline")
        controller = session.controller
        cap = asyncio.Event()
        delays = expire_waits(monkeypatch, cap) if wake == "cap" else []
        before = (len(h.scripted.ops.attaches), len(h.scripted.ops.tickets))
        assert await reopen(h, path) == "deferred"
        task = session._gap_retry
        assert task is not None
        assert await reopen(h, path) == "deferred"
        assert session._gap_retry is task
        assert (len(h.scripted.ops.attaches), len(h.scripted.ops.tickets)) == before
        if wake == "cap":
            cap.set()
        else:
            await until(lambda: session.gap_recovery.waiting)
            session.set_host_online(False)
            await asyncio.sleep(0)
            assert not task.done()
            session.set_host_online(True)
        await asyncio.wait_for(task, 2)
        assert session.controller is not controller
        assert session.controller.gap_recovery is controller.gap_recovery
        assert len(h.scripted.ops.attaches) == before[0] + 1
        assert len(h.scripted.ops.tickets) == before[1] + 1
        if wake == "cap":
            assert delays == pytest.approx([60], abs=0.02)
        assert session._store.draft == "unsent"
        assert h.sent_bytes() == 0 and not h.scripted.submitted and not h.scripted.prompted

    run_recovery("agent", body)


@pytest.mark.parametrize("path", ["auto_take", "reload", "foreground"])
def test_controller_replacement_cannot_refill_five_step_budget(
    monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    delays = expire_waits(monkeypatch)

    async def body(h: Harness) -> None:
        session = (await h.open()).session
        session.auto_take_allowed = True
        controllers = []
        for _ in range(5):
            controllers.append(session.controller)
            gap(session, "rate_limited")
            assert await reopen(h, path) == "deferred"
            if session._gap_retry is not None:
                await session._gap_retry
        assert len({id(c) for c in controllers}) == 5
        assert session.gap_recovery.attempt == 5
        gap(session, "rate_limited")
        controller = session.controller
        state = controller.state
        before = (len(h.scripted.ops.attaches), len(h.scripted.ops.tickets))
        assert await reopen(h, path) == "deferred"
        if session._gap_retry is not None:
            assert await session._gap_retry == "exhausted"
        assert session.notice == GAP_EXHAUSTED_NOTICE
        assert not controller._viewer_lost and not controller.writer_revoked
        assert controller.state is state
        assert (len(h.scripted.ops.attaches), len(h.scripted.ops.tickets)) == before
        assert await reopen(h, path) == "exhausted"
        assert delays == pytest.approx([1, 2, 4, 8, 16], abs=0.02)
        await session.reconnect()  # Explicit Reconnect alone replenishes the budget.
        assert session.gap_recovery.attempt == 0 and not session.gap_recovery.exhausted

    run_recovery("agent", body)


@pytest.mark.parametrize("ending", ["leave", "revoke", "controller", "cancel"])
def test_deferred_retry_rechecks_captured_identity(ending: str) -> None:
    async def body(h: Harness) -> None:
        session = (await h.open()).session
        gap(session, "host_offline")
        assert await reopen(h, "foreground") == "deferred"
        task = session._gap_retry
        await until(lambda: session.gap_recovery.waiting)
        before = (len(h.scripted.ops.attaches), len(h.scripted.ops.tickets))
        if ending == "leave":
            await session.disconnect()
        elif ending == "revoke":
            h.revoked = True
        elif ending == "controller":
            session._controller = session._spawn_controller(session.lease)
        else:
            task.cancel()
        session.set_host_online(True)
        await asyncio.gather(task, return_exceptions=True)
        assert (len(h.scripted.ops.attaches), len(h.scripted.ops.tickets)) == before
        assert not session.gap_recovery.waiting and h.sent_bytes() == 0

    run_recovery("agent", body)


def test_foreground_return_redrives_pending_gap_once() -> None:
    async def body(h: Harness) -> None:
        session = (await h.open()).session
        gap(session, "host_offline")
        assert await reopen(h, "auto_take") == "deferred"
        old = session._gap_retry
        await until(lambda: session.gap_recovery.waiting)
        session.set_foreground(False)
        await asyncio.gather(old, return_exceptions=True)
        before = len(h.scripted.ops.attaches)
        session.set_foreground(True)
        new = session._gap_retry
        assert new is not None and new is not old
        session.set_foreground(True)
        assert session._gap_retry is new
        session.set_host_online(True)
        await new
        assert len(h.scripted.ops.attaches) == before + 1

    run_recovery("agent", body)


def test_unchanged_host_heartbeat_does_not_wake_but_fresh_seen_does() -> None:
    async def body(h: Harness) -> None:
        session = (await h.open()).session
        session.auto_take_allowed = True
        async with httpx.AsyncClient(trust_env=False) as client:
            transport = HttpxGatewayTransport("https://synthetic.invalid", lambda: "", client)
            sync = SyncController(transport=transport)
            resources = AppResources({}, client, False, h.pool, None, sync, transport, None)
            resources.recovery.close()
            resources.recovery = h.recovery
            h.pool.recovery_note = lambda _: None  # Explicitly drive each step in this test.
            host = Host("mac", "Mac", reachable=True, last_seen_ms=1)
            state = replace(AppState(), inventory=Inventory(hosts={"mac": host}))
            sync._set(state)
            gap(session, "host_offline")
            await reopen(h, "reload")
            task = session._gap_retry
            await until(lambda: session.gap_recovery.waiting)
            for seen in (1, 1, 2):
                host = replace(host, last_seen_ms=seen)
                sync._set(replace(state, inventory=Inventory(hosts={"mac": host})))
                await asyncio.sleep(0)
            assert not task.done() and not session.gap_recovery.host_online
            before = len(h.scripted.ops.attaches)
            host = replace(host, last_seen_ms=int(session.gap_recovery.gap_ms) + 1)
            sync._set(replace(state, inventory=Inventory(hosts={"mac": host})))
            await asyncio.wait_for(task, 2)
            assert len(h.scripted.ops.attaches) == before + 1
            assert session.phase == "live" and session._gap_retry is None
            assert session.gap_recovery.attempt == 0 and session.gap_recovery.host_online
            assert resources._gap_host_state in sync._listeners

            class Commands:
                async def close(self) -> None:
                    pass

            resources.commands = Commands()
            await resources.close()
            assert resources._gap_host_state not in sync._listeners

    run_recovery("agent", body)


def test_reload_without_auto_takeover_arms_disclosure_without_a_retry() -> None:
    async def body(h: Harness) -> None:
        session = (await h.open()).session
        h.pool.recovery_note = lambda _: None  # The reload path alone is under test.
        gap(session, "host_offline")
        budget = session.gap_recovery
        revision = budget.revision
        before = len(h.scripted.ops.attaches)
        assert await reopen(h, "reload") == "disconnected"
        assert session._disclosure_armed and session._gap_retry is None
        assert budget.revision == revision and budget.consumed != revision
        session.set_host_online(True)
        await asyncio.sleep(0)
        assert session._gap_retry is None and len(h.scripted.ops.attaches) == before
        assert await session.confirm_takeover() == "live"
        assert len(h.scripted.ops.attaches) == before + 1 and budget.detail == ""

    run_recovery("agent", body)


def test_failed_admission_after_host_return_backs_off_instead_of_rewaking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = asyncio.Event()
    delays = expire_waits(monkeypatch, gate)

    async def body(h: Harness) -> None:
        session = (await h.open()).session
        session.auto_take_allowed = True
        gap(session, "host_offline")
        budget = session.gap_recovery
        original = h.scripted.ops.attach
        calls = 0

        async def fail_once(*args: Any, **kwargs: Any) -> Any:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise ShellOpError("rate_limited", "synthetic")
            return await original(*args, **kwargs)

        monkeypatch.setattr(h.scripted.ops, "attach", fail_once)
        assert await reopen(h, "auto_take") == "deferred"
        first = session._gap_retry
        await until(lambda: budget.waiting)
        session.set_host_online(True)
        gate.set()
        await first
        assert session.phase == "disconnected" and budget.detail == "host_offline"
        assert budget.host_online and budget.attempt == 0 and budget.consumed != budget.revision
        h.recovery.note(h.entry)
        await until(lambda: session._gap_retry is not None)
        await session._gap_retry
        assert session.phase == "live" and budget.attempt == 1 and calls == 2
        assert delays == pytest.approx([60, 1], abs=0.02)

    run_recovery("agent", body)


@pytest.mark.parametrize("missing", [True, False])
def test_host_return_wakes_session_even_without_a_controller(missing: bool) -> None:
    async def body(h: Harness) -> None:
        session = (await h.open()).session
        resources = AppResources({}, None, False, h.pool, None, None, None, None)
        resources.recovery.close()
        session._controller = None
        host = Host("mac", "Mac", reachable=False)
        state = replace(AppState(), inventory=Inventory(hosts={} if missing else {"mac": host}))
        resources._gap_host_state(state)
        session.gap_recovery.record("host_offline")
        resources._gap_host_state(
            replace(state, inventory=Inventory(hosts={"mac": replace(host, reachable=True)}))
        )
        assert session.gap_recovery.host_online

    run_recovery("agent", body)


@pytest.mark.parametrize("after", [5.0, 31.0])
def test_first_live_output_clears_cause_and_only_stable_output_refills(
    monkeypatch: pytest.MonkeyPatch, after: float
) -> None:
    async def body(h: Harness) -> None:
        session = (await h.open()).session
        budget = session.gap_recovery
        budget.attempt = 4
        budget.foreground_attempts = 2
        session._recovery_cause = "transport"
        now = [1000.0]
        monkeypatch.setattr(budget_module.time, "monotonic", lambda: now[0])
        session.controller._handle_output(0, b"one")
        assert session.recovery_cause is None
        now[0] += after
        session.controller._handle_output(3, b"two")
        assert budget.attempt == (0 if after >= 30 else 4)
        assert budget.foreground_attempts == (0 if after >= 30 else 2)
        budget.record("rate_limited")
        now[0] += 31
        session.controller._handle_output(6, b"three")
        assert budget.stable_since == now[0]

    run_recovery("agent", body)


def test_stale_revision_window_survives_controller_replacement() -> None:
    async def body(h: Harness) -> None:
        session = (await h.open()).session
        gap(session, "stale_revision")
        await reopen(h, "auto_take")
        await session._gap_retry
        replacement = session.controller
        gap(session, "stale_revision")
        await reopen(h, "auto_take")
        if session._gap_retry is not None:
            assert await session._gap_retry == "exhausted"
        assert session.controller is replacement and not replacement._viewer_lost
        session.request_takeover()
        await session.confirm_takeover()
        assert not session.gap_recovery.exhausted

    run_recovery("agent", body)


@pytest.mark.parametrize("step", [0, 1, 2])
def test_reader_handoff_never_mints_after_long_delay(
    monkeypatch: pytest.MonkeyPatch, step: int
) -> None:
    expire_waits(monkeypatch)

    async def scenario() -> None:
        socket = ScriptedTerminalSocket()
        controller = TerminalController(lambda _: socket, "lease-1", 1, "control", lambda _: None)
        await controller.start("first")
        controller._handle_gap("rate_limited")
        controller.gap_recovery.attempt = step
        tickets = []

        async def mint(lease: str) -> str:
            tickets.append(lease)
            return "synthetic"

        outcome = await drive_recovery(controller, mint)
        assert outcome == ("reacquire" if step == 2 else "reattached")
        assert len(tickets) == (0 if step == 2 else 1)
        await controller.close()

    asyncio.run(scenario())


def test_expired_resume_ticket_reacquires_under_existing_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from relaydeck.client.features.shell.ops import ShellOpError

    expire_waits(monkeypatch)

    async def body(h: Harness) -> None:
        session = (await h.open()).session
        session.auto_take_allowed = True
        gap(session, "rate_limited")
        original = h.scripted.ops.terminal_ticket
        expired = session.lease.lease_id

        async def ticket(lease: str) -> str:
            if lease == expired:
                raise ShellOpError("lease_expired", "synthetic")
            return await original(lease)

        h.scripted.ops.terminal_ticket = ticket
        before = len(h.scripted.ops.attaches)
        await session.drive_recovery()
        assert len(h.scripted.ops.attaches) == before + 1
        assert session.lease.lease_id != expired and session.gap_recovery.attempt == 1
        assert h.sent_bytes() == 0

    run_recovery("agent", body)


def test_reopen_and_composer_diagnostics_are_correlated_and_content_free(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from relaydeck.client.features.terminal.diagnostics import diagnostics_logger

    log = diagnostics_logger()
    log.addHandler(caplog.handler)
    caplog.set_level(logging.INFO)
    marker = "bearer-secret?ticket=grant-cookie/path"

    async def body(h: Harness) -> None:
        entry = await h.open()
        session = entry.session
        session._store.replace_draft(marker)
        session._store._submission = SUBMISSION_SENDING
        session.gap_recovery.record(marker)
        for site in (
            "auto_take",
            "recover_after_reload",
            "open",
            "take_over",
            "reconnect",
            "fg_reacquire",
        ):
            session.log_reopen(site)
        before = len(caplog.records)
        session._bump_generation("suspend")
        session.set_foreground(False)
        assert not any(r.message.startswith("composer_reset ") for r in caplog.records[before:])
        session._store.reconnect()
        session._store.unbind_writer()
        h.recovery._emit(entry, "loss", "disconnected", "deferred")

    try:
        run_recovery("agent", body)
    finally:
        log.removeHandler(caplog.handler)
    lines = caplog.text
    assert marker not in lines and "ticket=" not in lines
    for site in (
        "auto_take",
        "recover_after_reload",
        "open",
        "take_over",
        "reconnect",
        "fg_reacquire",
    ):
        assert f"reopen site={site} term=" in lines
    for site in ("reconnect_flow", "detach"):
        assert f"composer_reset site={site} submission=" in lines
    assert "composer_reset site=suspend" not in lines
    assert "generation_bump site=suspend term=" in lines
    assert "fg_recovery reason=loss" in lines
    assert "gap=other" in lines


@pytest.mark.parametrize("kind", ["agent", "shell"])
def test_foreground_resume_preserves_pending_gap_and_authorization(kind: str) -> None:
    async def body(h: Harness) -> None:
        session = (await h.open()).session
        gap(session, "host_offline")
        assert await reopen(h, "foreground") == "deferred"
        old = session._gap_retry
        await until(lambda: session.gap_recovery.waiting)
        await h.hide()
        await asyncio.gather(old, return_exceptions=True)
        h.show()
        before = (len(h.scripted.ops.attaches), len(h.scripted.ops.tickets))
        assert await reopen(h, "foreground") == "deferred"
        task = session._gap_retry
        await until(lambda: session.gap_recovery.waiting)
        assert (len(h.scripted.ops.attaches), len(h.scripted.ops.tickets)) == before
        session.set_host_online(True)
        await asyncio.wait_for(task, 2)
        assert len(h.scripted.ops.tickets) == before[1] + 1
        assert h.sent_bytes() == 0

    run_recovery(kind, body)


def test_foreground_bounce_does_not_revive_a_deliberately_released_gap() -> None:
    async def body(h: Harness) -> None:
        session = (await h.open()).session
        gap(session, "host_offline")
        await reopen(h, "auto_take")
        old = session._gap_retry
        await until(lambda: session.gap_recovery.waiting)
        await session.disconnect()
        await asyncio.gather(old, return_exceptions=True)
        before = len(h.scripted.ops.attaches)
        session.set_foreground(False)
        session.set_foreground(True)
        session.set_host_online(True)
        await asyncio.sleep(0)
        assert session._gap_retry is None and session.recovery_cause is None
        assert len(h.scripted.ops.attaches) == before

    run_recovery("agent", body)


def test_fresh_viewer_does_not_refill_session_budget() -> None:
    async def body(h: Harness) -> None:
        session = (await h.open()).session
        budget = session.gap_recovery
        budget.attempt, budget.foreground_attempts = 4, 2
        await drive_recovery(session.controller, h.scripted.ops.terminal_ticket, fresh_viewer=True)
        assert budget.attempt == 4 and budget.foreground_attempts == 2
        budget.stable_since = 1.0
        controller = session._spawn_controller(session.lease)
        assert controller.gap_recovery is budget and budget.stable_since is None
        assert budget.attempt == 4 and budget.foreground_attempts == 2

    run_recovery("agent", body)


@pytest.mark.parametrize("detail", ["rate_limited", "host_offline"])
@pytest.mark.parametrize("failure", ["holder", "attach", "stream"])
def test_failed_admission_rearms_without_a_new_gap(
    monkeypatch: pytest.MonkeyPatch, failure: str, detail: str
) -> None:
    delays = expire_waits(monkeypatch)

    async def body(h: Harness) -> None:
        session = (await h.open()).session
        session.auto_take_allowed = True
        session._store.replace_draft("unsent")
        gap(session, detail)
        budget = session.gap_recovery
        revision = budget.revision
        before = len(h.scripted.ops.attaches)
        method = "control_holder" if failure == "holder" else "attach"
        original = getattr(h.scripted.ops, method)
        calls = 0

        async def fail_once(*args: Any, **kwargs: Any) -> Any:
            nonlocal calls
            calls += 1
            if calls == 1 and failure != "stream":
                raise ShellOpError("host_offline", "synthetic")
            return await original(*args, **kwargs)

        monkeypatch.setattr(h.scripted.ops, method, fail_once)
        assert await reopen(h, "foreground") == "deferred"
        first = session._gap_retry
        assert first is not None
        await first
        if failure == "stream":
            assert session.phase == "live"
            assert session.controller.live_output_received is False
            await h.scripted.streams.opened[-1][2].close()
            assert await session.run() == "disconnected"
        assert budget.revision > revision and budget.detail == detail
        assert budget.consumed != budget.revision and session._gap_retry is None
        assert budget.attempt == (1 if detail == "rate_limited" else 0)
        # A normal foreground note must schedule another flight, not strand "deferred".
        h.recovery.note(h.entry)
        await until(lambda: session._gap_retry is not None)
        second = session._gap_retry
        assert second is not first
        await second
        assert session.phase == "live"
        assert len(h.scripted.ops.attaches) == before + (2 if failure == "stream" else 1)
        assert delays == pytest.approx([1, 2] if detail == "rate_limited" else [60, 60], abs=0.02)
        assert session._store.draft == "unsent" and h.sent_bytes() == 0

    run_recovery("agent", body)


def test_failed_last_admission_shows_exhaustion(monkeypatch: pytest.MonkeyPatch) -> None:
    expire_waits(monkeypatch)

    async def body(h: Harness) -> None:
        session = (await h.open()).session
        gap(session, "rate_limited")
        session.gap_recovery.attempt = 4

        async def refuse(*_: Any) -> Any:
            raise ShellOpError("host_offline", "synthetic")

        monkeypatch.setattr(h.scripted.ops, "control_holder", refuse)
        assert await reopen(h, "auto_take") == "deferred"
        await session._gap_retry
        assert session.gap_recovery.attempt == 5 and session.gap_recovery.exhausted
        assert session.notice == GAP_EXHAUSTED_NOTICE and session._gap_retry is None
        assert await reopen(h, "auto_take") == "exhausted"

    run_recovery("agent", body)


@pytest.mark.parametrize("kind", ["agent", "shell"])
def test_foreground_loss_during_admission_rearms_on_return(
    monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    delays = expire_waits(monkeypatch)

    async def body(h: Harness) -> None:
        session = (await h.open()).session
        session.auto_take_allowed = True
        gap(session, "rate_limited")
        started = asyncio.Event()
        method = "control_holder" if kind == "agent" else "attach"
        original = getattr(h.scripted.ops, method)
        calls = 0

        async def block_once(*args: Any, **kwargs: Any) -> Any:
            nonlocal calls
            calls += 1
            if calls == 1:
                started.set()
                await asyncio.Event().wait()
            return await original(*args, **kwargs)

        monkeypatch.setattr(h.scripted.ops, method, block_once)
        tickets = len(h.scripted.ops.tickets)
        assert await reopen(h, "foreground") == "deferred"
        first = session._gap_retry
        await started.wait()
        session.set_foreground(False)
        await asyncio.gather(first, return_exceptions=True)
        assert session._gap_retry is None
        session.set_foreground(True)
        second = session._gap_retry
        assert second is not None and second is not first
        await second
        assert session.phase == "live" and len(h.scripted.ops.tickets) == tickets + 1
        assert delays == pytest.approx([1, 2], abs=0.02) and h.sent_bytes() == 0

    run_recovery(kind, body)


@pytest.mark.parametrize("kind", ["agent", "shell"])
def test_warm_view_output_cannot_clear_a_control_gap(kind: str) -> None:
    async def body(h: Harness) -> None:
        session = (await h.open()).session
        gap(session, "rate_limited")
        budget = session.gap_recovery
        budget.attempt, budget.foreground_attempts = 4, 2
        view = replace(session.lease, mode="view")
        session._lease = view
        session._controller = session._spawn_controller(view)
        session.controller._handle_output(0, b"warm ring")
        assert budget.detail == "rate_limited" and session.recovery_cause == "transport"
        assert budget.attempt == 4 and budget.foreground_attempts == 2
        control = replace(view, mode="control")
        session._lease = control
        session._controller = session._spawn_controller(control)
        session.controller._handle_output(0, b"live")
        assert budget.detail == "" and session.recovery_cause is None
        assert budget.attempt == 4

    run_recovery(kind, body)


@pytest.mark.parametrize("kind", ["agent", "shell"])
def test_explicit_reconnect_clears_automatic_cause(kind: str) -> None:
    async def body(h: Harness) -> None:
        session = (await h.open()).session
        gap(session, "host_offline")
        assert session.recovery_cause == "transport"
        await session.reconnect()
        assert session.recovery_cause is None and session.gap_recovery.detail == ""
        assert h.recovery.request(h.entry, "loss") is None

    run_recovery(kind, body)


@pytest.mark.parametrize("site", ["detach", "takeover", "fence_hidden", "reconnect_flow"])
def test_composer_reset_logs_the_actual_caller(caplog: pytest.LogCaptureFixture, site: str) -> None:
    from relaydeck.client.features.terminal.diagnostics import diagnostics_logger
    from relaydeck.client.features.terminal.warm_watch import fence_hidden_writer
    from relaydeck.lease_diagnostics import opaque

    log = diagnostics_logger()
    log.addHandler(caplog.handler)
    caplog.set_level(logging.INFO)

    async def body(h: Harness) -> None:
        session = (await h.open()).session
        caplog.clear()
        if site == "detach":
            session._detach_store()
        elif site == "takeover":
            session.request_takeover()
            await session.confirm_takeover()
        elif site == "fence_hidden":
            fence_hidden_writer(session)
        else:
            await session.reconnect()
        resets = [r.message for r in caplog.records if r.message.startswith("composer_reset ")]
        assert len(resets) == (1 if site == "takeover" else 2)
        term = opaque("terminal", session.host_id, "a")
        assert all(f"site={site} " in line and f"term={term}" in line for line in resets)

    try:
        run_recovery("agent", body)
    finally:
        log.removeHandler(caplog.handler)


@pytest.mark.parametrize("outcome", ["admitted", "deferred", "invalid", "exhausted"])
def test_gap_retry_logs_outcome_without_content(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, outcome: str
) -> None:
    from relaydeck.client.features.terminal.diagnostics import diagnostics_logger

    log = diagnostics_logger()
    log.addHandler(caplog.handler)
    caplog.set_level(logging.INFO)
    marker = "private?ticket=grant-cookie/path"
    gate = asyncio.Event() if outcome == "deferred" else None
    expire_waits(monkeypatch, gate)

    async def body(h: Harness) -> None:
        session = (await h.open()).session
        gap(session, marker)
        if outcome == "exhausted":
            session.gap_recovery.attempt = 5
        await reopen(h, "auto_take")
        task = session._gap_retry
        if outcome == "invalid":
            session._controller = session._spawn_controller(session.lease)
        if gate is not None:
            await until(lambda: session.gap_recovery.waiting)
            session.gap_recovery.record(marker)
            gate.set()
        await task
        lines = [r.message for r in caplog.records if r.message.startswith("gap_retry ")]
        assert len(lines) == 1 and f"outcome={outcome} gap=other waited_ms=" in lines[0]
        assert " term=" in lines[0] and marker not in caplog.text and "ticket=" not in caplog.text

    try:
        run_recovery("agent", body)
    finally:
        log.removeHandler(caplog.handler)
