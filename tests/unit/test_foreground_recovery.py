"""Foreground return reconnects the visible terminal once, safely, and never forces control."""

from __future__ import annotations

import asyncio
import dataclasses
from typing import Any

import pytest

from relaydeck.client.features.terminal.foreground_recovery import (
    MAX_ATTEMPTS,
    ForegroundRecovery,
)
from tests.support.foreground_recovery import Harness
from tests.support.foreground_recovery import run_recovery as _run
from tests.support.ui.instant_open import jobs

KINDS = ["agent", "shell"]


@pytest.mark.parametrize("kind", KINDS)
def test_returning_after_a_long_hidden_period_reconnects_once_without_input(kind: str) -> None:
    async def body(h: Harness) -> None:
        entry = await h.open()
        session = entry.session
        await h.hide()
        assert session.phase == "disconnected"
        assert session.recovery_cause == "lifecycle"
        h.now[0] += 3_600_000  # long suspension: leases expired meanwhile
        before_bytes = h.sent_bytes()
        h.show()
        task = h.recovery.request(entry, "visible")
        assert task is not None
        assert h.recovery.request(entry, "reconnect") is task  # either event order: one flight
        assert await task == "connected"
        assert session.phase != "disconnected"
        assert not session.recovering
        assert h.verified == [entry]
        assert h.sent_bytes() == before_bytes
        assert h.scripted.submitted == [] and h.scripted.prompted == []

    _run(kind, body)


@pytest.mark.parametrize("kind", KINDS)
def test_a_transport_loss_that_cleared_the_controller_is_recovered(kind: str) -> None:
    async def body(h: Harness) -> None:
        entry = await h.open()
        session = entry.session
        session.require_ui_recovery()
        await jobs(h.pool)
        assert session.phase == "disconnected" and session.controller is None
        assert session.recovery_cause == "transport"
        task = h.recovery.request(entry, "reconnect")
        assert task is not None
        assert await task == "connected"
        assert session.phase != "disconnected"

    _run(kind, body)


@pytest.mark.parametrize("kind", KINDS)
def test_nothing_is_spent_or_started_while_hidden(kind: str) -> None:
    async def body(h: Harness) -> None:
        entry = await h.open()
        await h.hide()
        for _ in range(MAX_ATTEMPTS + 2):
            task = h.recovery.request(entry, "reconnect")
            if task is not None:
                assert await task == "skipped"
        assert entry.session.phase == "disconnected"
        assert h.scripted.ops.attaches == [] or len(h.scripted.ops.attaches) == 1
        attaches = len(h.scripted.ops.attaches)
        h.show()
        task = h.recovery.request(entry, "visible")
        assert task is not None and await task == "connected"
        assert len(h.scripted.ops.attaches) >= attaches

    _run(kind, body)


@pytest.mark.parametrize("kind", KINDS)
def test_a_deliberate_release_is_never_revived(kind: str) -> None:
    async def body(h: Harness) -> None:
        entry = await h.open()
        await entry.session.disconnect()
        assert entry.session.recovery_cause is None
        h.show()
        task = h.recovery.request(entry, "visible")
        if task is not None:
            assert await task == "skipped"
        assert entry.session.phase == "disconnected"

    _run(kind, body)


@pytest.mark.parametrize("kind", KINDS)
def test_returning_before_the_pause_finishes_waits_for_it(kind: str) -> None:
    async def body(h: Harness) -> None:
        entry = await h.open()
        h.pool.set_device_foreground(False)  # pause job is queued, not finished
        h.show()
        task = h.recovery.request(entry, "visible")
        assert task is not None
        assert await task == "connected"
        assert entry.session.phase != "disconnected"

    _run(kind, body)


@pytest.mark.parametrize("kind", KINDS)
def test_leaving_the_terminal_during_recovery_publishes_nothing(kind: str) -> None:
    async def body(h: Harness) -> None:
        entry = await h.open()
        await h.hide()
        h.show()
        task = h.recovery.request(entry, "visible")
        assert task is not None
        h.pool.release()  # another terminal or the list is selected
        assert await task in {"skipped", "superseded"}
        assert h.verified == []
        assert entry.session.phase == "disconnected" or h.pool.foreground is not entry

    _run(kind, body)


@pytest.mark.parametrize("kind", KINDS)
def test_revocation_during_recovery_stops_it(kind: str) -> None:
    async def body(h: Harness) -> None:
        entry = await h.open()
        await h.hide()
        h.show()
        task = h.recovery.request(entry, "visible")
        assert task is not None
        h.revoked = True
        assert await task in {"skipped", "superseded"}
        assert h.verified == []

    _run(kind, body)


@pytest.mark.parametrize("kind", KINDS)
def test_an_obsolete_transport_epoch_does_not_publish(kind: str) -> None:
    async def body(h: Harness) -> None:
        entry = await h.open()
        await h.hide()
        h.show()
        task = h.recovery.request(entry, "visible")
        assert task is not None
        h.stamp[1] += 1  # the transport was replaced before the flight ran
        outcome = await task
        assert outcome in {"connected", "skipped", "superseded"}
        assert h.verified in ([], [entry])

    _run(kind, body)


def test_retries_survive_remount_and_transport_epoch_changes() -> None:
    async def body(h: Harness) -> None:
        entry = await h.open()
        await h.hide()
        h.show()
        refused = 0

        async def refuse(*_: Any, **__: Any) -> bool:
            nonlocal refused
            refused += 1
            return False

        h.pool.resume_watch = refuse  # type: ignore[method-assign]
        outcomes = []
        for _ in range(MAX_ATTEMPTS + 2):
            task = h.recovery.request(entry, "reconnect")
            if task is not None:
                outcomes.append(await task)
        assert refused == MAX_ATTEMPTS
        assert outcomes == ["refused"] * MAX_ATTEMPTS
        assert h.recovery.request(entry, "reconnect") is None  # budget spent: nothing starts
        h.stamp[1] += 1
        assert h.recovery.request(entry, "reconnect") is None
        h.recovery.close()
        h.recovery = ForegroundRecovery(
            retained=h.pool,
            current=h._current,
            stamp=lambda: (h.stamp[0], h.stamp[1]),
            verify=h.verified.append,
        )
        assert h.recovery.request(entry, "reconnect") is None  # remount cannot refill
        assert refused == MAX_ATTEMPTS
        assert entry.session.notice == "Connection keeps dropping. Tap Reconnect."
        budget = entry.session.gap_recovery
        budget.attempt, budget.stale_retry_at = 4, 123.0
        h.stamp[0] += 1
        for _ in range(MAX_ATTEMPTS):
            task = h.recovery.request(entry, "visible")
            assert task is not None and await task == "refused"
        assert refused == 2 * MAX_ATTEMPTS
        assert h.recovery.request(entry, "visible") is None
        assert budget.attempt == 4 and budget.stale_retry_at == 123.0

    _run("agent", body)


def test_an_explicit_reconnect_renews_the_budget() -> None:
    async def body(h: Harness) -> None:
        entry = await h.open()
        await h.hide()
        h.show()

        async def refuse(*_: Any, **__: Any) -> bool:
            return False

        h.pool.resume_watch = refuse  # type: ignore[method-assign]
        for _ in range(MAX_ATTEMPTS):
            task = h.recovery.request(entry, "reconnect")
            assert task is not None
            await task
        task = h.recovery.request(entry, "reconnect")
        assert task is None  # budget spent
        await entry.session.reconnect()  # the user tapped Reconnect
        entry.session.require_ui_recovery()
        assert entry.session.gap_recovery.foreground_attempts == 0
        assert entry.session.recovery_cause is None
        assert h.recovery.request(entry, "reconnect") is None  # disclosure stays explicit
        entry.session._recovery_cause = "lifecycle"  # a subsequent genuine lifecycle loss
        task = h.recovery.request(entry, "reconnect")
        assert task is not None  # the tap renewed the budget

    _run("agent", body)


def test_view_stays_view_and_control_is_never_forced_on_a_shell() -> None:
    async def body(h: Harness) -> None:
        entry = await h.open()
        session = entry.session
        await h.hide()
        # Another device now holds this terminal.
        ops = h.scripted.ops
        record = ops._attachments[("mac", "a")]
        record.info = dataclasses.replace(record.info, owner_device_id="other", owner_label="Other")
        h.show()
        task = h.recovery.request(entry, "visible")
        assert task is not None
        await task
        assert session.phase == "conflict"
        assert all(item["takeover"] is False for item in ops.attaches[1:])

    _run("shell", body)


def test_a_shell_that_was_watching_recovers_as_a_viewer() -> None:
    async def body(h: Harness) -> None:
        entry = await h.open()
        session = entry.session
        await h.hide()
        session._resume_mode = "view"
        modes_before = [item["mode"] for item in h.scripted.ops.leases]
        h.show()
        task = h.recovery.request(entry, "visible")
        assert task is not None
        await task
        modes = [item["mode"] for item in h.scripted.ops.leases][len(modes_before) :]
        assert "control" not in modes

    _run("shell", body)


def test_an_agent_held_elsewhere_is_watched_and_control_is_not_forced() -> None:
    async def body(h: Harness) -> None:
        entry = await h.open()
        session = entry.session
        session.auto_take_allowed = True
        await h.hide()
        ops = h.scripted.ops
        attachment = session.attachment
        assert attachment is not None
        ops._control_holder[attachment.attachment_id] = (ops.account, "other-device")
        takeovers = sum(1 for item in ops.attaches if item["takeover"] is True)
        h.show()
        task = h.recovery.request(entry, "visible")
        assert task is not None
        await task
        assert sum(1 for item in ops.attaches if item["takeover"] is True) == takeovers
        assert ops._control_holder[attachment.attachment_id] == (ops.account, "other-device")
        assert session.lease is None or session.lease.mode == "view"

    _run("agent", body)


def test_page_visibility_observes_hide_and_transport_return_starts_one_flight() -> None:
    from relaydeck.client.resources import AppResources

    async def body(h: Harness) -> None:
        entry = await h.open()
        resources = AppResources(
            {},  # props
            None,  # type: ignore[arg-type]
            False,
            h.pool,
            None,  # type: ignore[arg-type]
            None,  # type: ignore[arg-type]
            None,  # type: ignore[arg-type]
            None,  # type: ignore[arg-type]
        )
        resources.recovery.close()
        resources.recovery = h.recovery
        epoch = resources.visibility_epoch
        controller, token = entry.session.controller, entry.session.input_token
        resources.set_page_visible(False)
        await jobs(h.pool)
        assert h.pool.device_foreground and not entry.session.paused
        assert entry.session.phase == "live"
        assert entry.session.controller is controller and entry.session.input_token == token
        assert resources._hidden_at is not None
        assert resources.visibility_epoch == epoch  # hidden spends no epoch
        resources.set_page_visible(False)  # duplicate hidden events change nothing
        await h.hide()  # Actual teardown remains distinct from an observed page hide.
        assert not h.pool.device_foreground and entry.session.paused
        resources.set_page_visible(True)
        resources.set_page_visible(True)  # duplicate visible events join one flight
        assert resources.visibility_epoch == epoch + 1
        state = h.recovery._states[entry]
        assert state.flight is not None
        await state.flight
        assert entry.session.phase != "disconnected"

    _run("shell", body)


def test_the_panels_say_reconnecting_instead_of_offering_the_button() -> None:
    import flet as ft

    from relaydeck.client.components.design_system.terminal_window import STATE_DISCONNECTED
    from relaydeck.client.features.terminal.surfaces.agent_panels import build_state_panels
    from relaydeck.client.features.terminal.surfaces.common import agent_window_state

    assert agent_window_state("reconnecting", False, False) == STATE_DISCONNECTED
    for reconnecting, key in ((True, "agent-reconnecting"), (False, "agent-disconnected")):
        bottom: list[Any] = []
        build_state_panels(
            bottom=bottom,
            confirm_takeover=False,
            conflict_owner=None,
            notice="",
            on_cancel_takeover=lambda: None,
            on_confirm_takeover=lambda: None,
            on_reconnect=lambda: None,
            on_takeover=lambda: None,
            phone=True,
            reconnecting=reconnecting,
            takeover_disclosure=None,
            tokens={"muted": "#888888"},
            window=STATE_DISCONNECTED,
        )
        assert [item.key for item in bottom] == [key]
        assert reconnecting == isinstance(bottom[0], ft.Text)

    async def body(h: Harness) -> None:
        entry = await h.open()
        assert entry.session.view().reconnecting is False
        entry.session.set_recovering(True)
        assert entry.session.view().reconnecting is True

    _run("shell", body)


def test_a_reconnect_without_lifecycle_events_recovers_the_terminal() -> None:
    async def body(h: Harness) -> None:
        entry = await h.open()
        session = entry.session
        await h.hide()  # the transport died while the page was away
        assert session.phase == "disconnected"
        h.pool.set_device_foreground(True)  # the session reconnect marks the page visible
        task = h.recovery.request(entry, "reconnect")
        assert task is not None
        assert await task == "connected"
        assert session.phase != "disconnected"

    _run("shell", body)


def _resources(h: Harness, now: list[float]) -> Any:
    from relaydeck.client.resources import AppResources

    resources = AppResources({}, None, False, h.pool, None, None, None, None)  # type: ignore[arg-type]
    resources.recovery.close()
    resources.recovery = h.recovery
    resources._clock = lambda: now[0]
    return resources


def test_a_show_on_a_visible_page_recovers_a_lifecycle_loss() -> None:
    async def body(h: Harness) -> None:
        entry = await h.open()
        resources = _resources(h, [0.0])
        entry.session.require_ui_recovery()  # transport loss; the page was never marked hidden
        await jobs(h.pool)
        assert h.pool.device_foreground and entry.session.phase == "disconnected"
        resources.activate("show")
        state = h.recovery._states[entry]
        assert state.flight is not None
        assert await state.flight == "connected"
        assert entry.session.phase != "disconnected"

    _run("shell", body)


def test_a_resume_on_a_silently_dead_stream_hands_it_to_recovery() -> None:
    async def body(h: Harness) -> None:
        entry = await h.open()
        now = [0.0]
        resources = _resources(h, now)
        h.pool.recovery_note = h.recovery.note
        probed: list[str] = []

        def failing_probe(item: Any, *, force: bool = False) -> None:
            probed.append("probe")
            item.session.require_ui_recovery()  # the liveness check found the stream dead

        resources._reconcile_terminal = failing_probe  # type: ignore[method-assign]
        assert entry.session.phase != "disconnected"
        resources.note_hidden()
        now[0] += 20.0  # a real absence
        resources.activate("show")
        resources.activate("resume")  # the same return: no second probe
        assert probed == ["probe"]
        await jobs(h.pool)
        state = h.recovery._states[entry]
        assert state.flight is not None
        await state.flight
        assert h.verified == [entry]

    _run("agent", body)


def test_a_burst_of_activations_is_one_flight_and_bounded() -> None:
    async def body(h: Harness) -> None:
        entry = await h.open()
        now = [0.0]
        resources = _resources(h, now)
        await h.hide()
        refusals = 0

        async def refuse(*_: Any, **__: Any) -> bool:
            nonlocal refusals
            refusals += 1
            return False

        h.pool.resume_watch = refuse  # type: ignore[method-assign]
        flights = set()
        for _ in range(10):
            resources.activate("show")
            resources.activate("resume")
            state = h.recovery._states[entry]
            if state.flight is not None:
                flights.add(id(state.flight))
                await asyncio.wait({state.flight})
            now[0] += 0.05
        assert refusals <= 2  # one budget for the whole burst
        assert resources.activations == 20
        assert len(flights) <= 2

    _run("agent", body)


@pytest.mark.parametrize("stop", ["release", "exit"])
def test_a_stopped_terminal_stays_stopped_on_activation(stop: str) -> None:
    async def body(h: Harness) -> None:
        entry = await h.open()
        resources = _resources(h, [0.0])
        if stop == "release":
            await entry.session.disconnect()
        else:
            entry.session._enter_exited("gone")
        phase = entry.session.phase
        resources.activate("show")
        state = h.recovery._states.get(entry)
        if state is not None and state.flight is not None:
            await state.flight
        assert entry.session.phase == phase

    _run("shell", body)


def test_focus_bounces_never_probe_or_fence_input() -> None:
    async def body(h: Harness) -> None:
        entry = await h.open()
        now = [0.0]
        resources = _resources(h, now)
        probed: list[str] = []
        resources._reconcile_terminal = lambda item, *, force=False: probed.append("probe")  # type: ignore[method-assign]
        controller = entry.session.controller
        before = h.sent_bytes()
        for _ in range(20):
            resources.note_hidden()
            now[0] += 0.1
            resources.activate("show")
            resources.activate("resume")
            now[0] += 0.1
        assert probed == []
        assert controller is entry.session.controller and not controller.ui_input_fenced
        await entry.session.type_text("x")
        assert h.sent_bytes() == before + 1

    _run("shell", body)
