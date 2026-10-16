"""Stop intent, guarded awaits and the loss trigger of foreground recovery."""

from __future__ import annotations

import asyncio
import dataclasses
from typing import Any

import pytest

from relaydeck.client.features.terminal.foreground_recovery import MAX_ATTEMPTS
from relaydeck.client.resources import AppResources
from tests.support.foreground_recovery import Harness
from tests.support.foreground_recovery import run_recovery as _run
from tests.support.ui.instant_open import jobs

KINDS = ["agent", "shell"]


async def _settle(h: Harness) -> None:
    for _ in range(5):
        await jobs(h.pool)
        await asyncio.sleep(0)


def _takeovers(h: Harness) -> list[dict[str, Any]]:
    return [item for item in h.scripted.ops.attaches if item["takeover"] is True]


@pytest.mark.parametrize("kind", KINDS)
def test_release_then_hide_then_show_is_not_revived(kind: str) -> None:
    async def body(h: Harness) -> None:
        entry = await h.open()
        taken = len(_takeovers(h))
        await entry.session.disconnect()
        await h.hide()
        assert entry.session.recovery_cause is None
        h.show()
        task = h.recovery.request(entry, "visible")
        if task is not None:
            assert await task == "skipped"
        assert entry.session.phase == "disconnected"
        assert len(_takeovers(h)) == taken

    _run(kind, body)


@pytest.mark.parametrize("kind", KINDS)
def test_a_revoked_writer_stays_stopped_across_hide_and_show(kind: str) -> None:
    async def body(h: Harness) -> None:
        entry = await h.open()
        entry.session.controller.invalidate_writer("lease_revoked")
        await h.hide()
        assert entry.session.recovery_cause is None
        h.show()
        task = h.recovery.request(entry, "visible")
        if task is not None:
            assert await task == "skipped"
        assert entry.session.phase == "disconnected"

    _run(kind, body)


def test_a_conflict_stays_a_conflict_across_hide_and_show() -> None:
    async def body(h: Harness) -> None:
        entry = await h.open()
        session = entry.session
        await h.hide()
        record = h.scripted.ops._attachments[("mac", "a")]
        record.info = dataclasses.replace(record.info, owner_device_id="other", owner_label="Other")
        h.show()
        task = h.recovery.request(entry, "visible")
        assert task is not None
        await task
        assert session.phase == "conflict"
        await h.hide()
        assert session.phase == "conflict" or session.recovery_cause is None
        h.show()
        task = h.recovery.request(entry, "visible")
        if task is not None:
            assert await task == "skipped"
        assert session.phase != "live"
        assert not _takeovers(h)

    _run("shell", body)


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("stop", ["exit", "release"])
def test_a_stop_arriving_while_the_pause_is_suspended_wins(kind: str, stop: str) -> None:
    async def body(h: Harness) -> None:
        entry = await h.open()
        session = entry.session
        gate = asyncio.Event()
        entered = asyncio.Event()
        original = h.scripted.ops.release_lease

        async def slow_release(lease_id: str) -> None:
            entered.set()
            await gate.wait()
            await original(lease_id)

        h.scripted.ops.release_lease = slow_release  # type: ignore[method-assign]
        h.pool.set_device_foreground(False)
        await asyncio.wait_for(entered.wait(), 2)
        if stop == "exit":
            session._enter_exited("gone")
            leaving = None
        else:
            leaving = asyncio.ensure_future(session.disconnect())
            await asyncio.sleep(0)
        gate.set()
        if leaving is not None:
            await leaving
        await _settle(h)
        assert session.recovery_cause is None
        assert session.phase in {"exited", "disconnected"}
        if stop == "exit":
            assert session.phase == "exited"
        h.show()
        task = h.recovery.request(entry, "visible")
        if task is not None:
            assert await task == "skipped"
        assert session.phase in {"exited", "disconnected"}

    _run(kind, body)


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("await_point", ["acquire_lease", "terminal_ticket"])
def test_an_obsolete_attempt_publishes_nothing_and_gives_back_the_lease(
    kind: str, await_point: str
) -> None:
    async def body(h: Harness) -> None:
        entry = await h.open()
        session = entry.session
        await h.hide()
        h.show()
        ops = h.scripted.ops
        gate = asyncio.Event()
        entered = asyncio.Event()
        original = getattr(ops, await_point)
        leases_before = len(ops.leases)

        async def slow(*args: Any, **kwargs: Any) -> Any:
            entered.set()
            await gate.wait()
            return await original(*args, **kwargs)

        setattr(ops, await_point, slow)
        task = h.recovery.request(entry, "visible")
        assert task is not None
        await asyncio.wait_for(entered.wait(), 2)
        h.revoked = True  # authorization went away during the real await
        gate.set()
        assert await task == "superseded"
        assert session.lease is None
        assert session.controller is None or session.controller.writer_revoked
        assert session.phase == "disconnected"
        assert h.verified == []
        acquired = len(ops.leases) - leases_before
        assert len(ops.releases) >= acquired

    _run(kind, body)


def test_a_holder_change_after_the_conflict_is_never_forced() -> None:
    async def body(h: Harness) -> None:
        entry = await h.open()
        session = entry.session
        await h.hide()
        ops = h.scripted.ops
        original = ops.attach
        from relaydeck.client.features.shell.ops import ShellOpError

        async def racing(host: str, terminal: str, kind: str, takeover: bool, *a: Any, **kw: Any):
            try:
                return await original(host, terminal, kind, takeover, *a, **kw)
            except ShellOpError:
                if not takeover:
                    record = ops._attachments[(host, terminal)]
                    ops._control_holder[record.info.attachment_id] = (ops.account, "other")
                raise

        ops.attach = racing  # type: ignore[method-assign]
        h.show()
        task = h.recovery.request(entry, "visible")
        assert task is not None
        await task
        assert session.phase == "conflict"
        assert _takeovers(h) == []
        assert ops._control_holder[session.attachment.attachment_id] == (ops.account, "other")

    _run("shell", body)


def test_an_explicit_take_over_is_still_unconditional() -> None:
    async def body(h: Harness) -> None:
        entry = await h.open()
        await entry.session.take_over()
        assert _takeovers(h)[-1]["takeover"] is True

    _run("shell", body)


@pytest.mark.parametrize("kind", KINDS)
def test_a_transport_loss_notification_starts_the_bounded_owner(kind: str) -> None:
    async def body(h: Harness) -> None:
        resources = AppResources({}, None, False, h.pool, None, None, None, None)  # type: ignore[arg-type]
        resources.recovery.close()
        h.pool.recovery_note = h.recovery.note  # the hook AppResources installs
        entry = await h.open()
        session = entry.session
        session.require_ui_recovery()  # the failed remount/probe outcome
        await _settle(h)
        assert session.phase != "disconnected"
        assert h.verified == [entry]
        await h.pool.shutdown()

    _run(kind, body)


def test_repeated_failures_are_coalesced_and_bounded() -> None:
    async def body(h: Harness) -> None:
        h.pool.recovery_note = h.recovery.note
        entry = await h.open()
        attempts = 0

        async def refuse(*_: Any, **__: Any) -> bool:
            nonlocal attempts
            attempts += 1
            return False

        h.pool.resume_watch = refuse  # type: ignore[method-assign]
        entry.session.require_ui_recovery()
        for _ in range(10):
            entry.session._notify()  # repeated renders must not spin
            await _settle(h)
        assert attempts == MAX_ATTEMPTS

    _run("agent", body)


def test_production_resources_install_the_loss_hook() -> None:
    from relaydeck.client.retained_sessions import RetainedSessions

    retained = RetainedSessions()
    resources = AppResources({}, None, False, retained, None, None, None, None)  # type: ignore[arg-type]
    assert retained.recovery_note == resources.recovery.note


@pytest.mark.parametrize("stop", ["exit", "release", "transport"])
@pytest.mark.parametrize("completion", ["success", "failure"])
@pytest.mark.parametrize("await_point", ["acquire_lease", "terminal_ticket"])
def test_a_late_shell_completion_never_overwrites_a_stop(
    stop: str, completion: str, await_point: str
) -> None:
    from relaydeck.client.features.shell.ops import ShellOpError

    async def body(h: Harness) -> None:
        entry = await h.open()
        session = entry.session
        await h.hide()
        h.show()
        ops = h.scripted.ops
        gate = asyncio.Event()
        entered = asyncio.Event()
        original = getattr(ops, await_point)

        async def slow(*args: Any, **kwargs: Any) -> Any:
            entered.set()
            await gate.wait()
            if completion == "failure":
                code = "lease_conflict" if await_point == "acquire_lease" else "lease_expired"
                raise ShellOpError(code, "late", {"owner": {"device_id": "x", "label": "X"}})
            return await original(*args, **kwargs)

        setattr(ops, await_point, slow)
        task = h.recovery.request(entry, "visible")
        assert task is not None
        await asyncio.wait_for(entered.wait(), 2)
        leaving = None
        if stop == "exit":
            session._enter_exited("gone")
        elif stop == "release":
            leaving = asyncio.ensure_future(session.disconnect())
            await asyncio.sleep(0)
        else:
            h.stamp[1] += 1
        phase_before = session.phase
        gate.set()
        await task
        if leaving is not None:
            await leaving
        if stop == "exit":
            assert session.phase == "exited"
        elif stop == "release":
            assert session.recovery_cause is None
            assert session.phase == "disconnected"
        if stop != "transport":
            assert session.phase != "conflict"
            assert not session.notice.startswith("Could not")
        else:
            # The obsolete attempt published nothing; a retry under the new epoch is its own.
            assert phase_before != "live"
            assert session.lease is None or session.lease.lease_id in ops._live_leases

    _run("shell", body)


def test_the_conditional_attach_binds_the_lease_to_its_generation() -> None:
    async def body(h: Harness) -> None:
        entry = await h.open()
        session = entry.session
        await h.hide()
        ops = h.scripted.ops
        original = ops.acquire_lease
        record = ops._attachments[("mac", "a")]
        calls: list[Any] = []

        async def raced(attachment_id: str, mode: str, **kwargs: Any) -> Any:
            calls.append(kwargs)
            # Another device replaces the attachment between the attach and the lease request.
            record.info = dataclasses.replace(
                record.info,
                generation=record.info.generation + 1,
                owner_device_id="other",
                owner_label="Other",
            )
            return await original(attachment_id, mode, **kwargs)

        ops.acquire_lease = raced  # type: ignore[method-assign]
        h.show()
        task = h.recovery.request(entry, "visible")
        assert task is not None
        await task
        assert calls and "expected_generation" in calls[0]
        assert session.phase == "conflict"
        assert session.lease is None

    _run("shell", body)


def test_a_recovered_shell_writer_stays_fenced_until_native_verification() -> None:
    async def body(h: Harness) -> None:
        entry = await h.open()
        session = entry.session
        await h.hide()
        h.show()
        before = h.sent_bytes()
        task = h.recovery.request(entry, "visible")
        assert task is not None
        await task
        assert session.controller is not None and session.controller.ui_input_fenced
        await session.type_text("x")
        assert h.sent_bytes() == before

    _run("shell", body)
