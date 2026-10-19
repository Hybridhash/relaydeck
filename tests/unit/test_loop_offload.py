"""Real locks/journals, synthetic delays; no listeners, credentials or upstream calls."""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path
from types import FunctionType, SimpleNamespace

import httpx
import pytest

from relaydeck.connector.journal import ConnectorDispatch
from relaydeck.gateway.app import create_app
from relaydeck.gateway.command_io import command_io
from relaydeck.gateway.loop_watch import LoopWatch
from relaydeck.gateway.production import JournalBackend
from relaydeck.gateway.usage import UsageService
from relaydeck.journal import CommandJournal, JournalConflict
from relaydeck.offload import OrderedGate
from relaydeck.usage.contracts import UsageQuery
from relaydeck.usage.store import SqliteUsageStore
from tests.support.creation.command_journal import NOW, _dispatch_msg
from tests.support.pairing import foundation, proof


def _blocking(seconds: float, content: str) -> None:
    time.sleep(seconds)


@pytest.mark.parametrize("seconds,expected", [(0.4, True), (0.02, False)])
@pytest.mark.parametrize("module", ["relaydeck.synthetic_load", "uvicorn.protocols.http.h11_impl"])
def test_loop_watch_attribution_is_static_and_content_free(caplog, seconds, expected, module):
    block = FunctionType(_blocking.__code__, {"__name__": module, "time": time})

    async def scenario():
        watch = LoopWatch()
        watch.start()
        try:
            await asyncio.sleep(0)
            block(seconds, "Bearer SECRET /private/SECRET?ticket=SECRET")
            await asyncio.sleep(0.12)
        finally:
            await watch.close()
        assert watch._thread is not None and not watch._thread.is_alive()

    asyncio.run(scenario())
    lines = [r.message for r in caplog.records if r.name.endswith("loop_watch")]
    assert any("loop_lag ms=" in line for line in lines) is expected
    assert any("loop_blocked ms=" in line for line in lines) is expected
    if expected:
        assert any(f"source={module}:_blocking:" in line for line in lines)
    assert all("SECRET" not in line and "/" not in line for line in lines)


def test_journal_loop_lane_retains_order_on_repeated_cancellation(tmp_path):
    async def scenario():
        journal = CommandJournal(tmp_path / "commands.db", clock=lambda: NOW)
        entered, release = threading.Event(), threading.Event()
        order = []

        def commit_first():
            entered.set()
            assert release.wait(2)
            journal.submit(
                command_id="first",
                principal="owner",
                host_id="mac",
                kind="agent.prompt",
                target={"agent_id": "a"},
                payload={"text": "synthetic"},
                now=NOW,
            )
            order.append("first")

        def next_read():
            order.append("second")
            return journal.record("first")

        try:
            first = asyncio.create_task(journal.run(commit_first))
            while not entered.is_set():
                await asyncio.sleep(0)
            first.cancel()
            await asyncio.sleep(0)
            first.cancel()
            second = asyncio.create_task(journal.run(next_read))
            await asyncio.sleep(0.02)
            assert order == [] and not second.done()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await first
            assert (await second)["status"] == "accepted"
            assert order == ["first", "second"]
            with pytest.raises(JournalConflict):
                await journal.run(
                    journal.submit,
                    command_id="first",
                    principal="owner",
                    host_id="mac",
                    kind="agent.prompt",
                    target={"agent_id": "a"},
                    payload={"text": "changed"},
                )
        finally:
            release.set()
            journal.close()

    asyncio.run(scenario())


def test_gateway_cancelled_accept_still_enqueues_and_keeps_loop_effects_local(tmp_path):
    async def scenario():
        journal = CommandJournal(tmp_path / "commands.db", clock=lambda: NOW)
        backend = JournalBackend(journal, now_ms=lambda: NOW)
        entered, release = threading.Event(), threading.Event()
        submit = journal.submit
        loop_thread = threading.get_ident()
        effects = []
        enqueue = backend.enqueue_record

        def delayed(**kwargs):
            assert threading.get_ident() != loop_thread
            entered.set()
            assert release.wait(2)
            return submit(**kwargs)

        def record_effect(*args):
            effects.append(threading.get_ident())
            return enqueue(*args)

        journal.submit = delayed
        backend.enqueue_record = record_effect
        command = {
            "command_id": "c1",
            "host_id": "mac",
            "principal": "owner",
            "kind": "agent.prompt",
            "target": {"agent_id": "a"},
            "payload": {"text": "synthetic"},
        }
        try:
            task = asyncio.create_task(command_io(backend, "submit_command", command))
            while not entered.is_set():
                await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert effects == [loop_thread]
            pending = await command_io(backend, "drain_outbox", "mac")
            assert [p["command_id"] for p in pending] == ["c1"]
            assert await command_io(backend, "mark_command_dispatched", "c1", NOW)
            # A redelivery after write-ahead cannot enqueue or execute it again.
            await command_io(backend, "submit_command", command)
            assert await command_io(backend, "drain_outbox", "mac") == []
        finally:
            release.set()
            journal.close()

    asyncio.run(scenario())


def test_connector_write_ahead_finishes_off_loop_before_upstream(tmp_path):
    async def scenario():
        journal = CommandJournal(tmp_path / "commands.db", clock=lambda: NOW)
        loop_thread = threading.get_ident()
        commits = []
        commit = journal._commit

        def checked_commit():
            commits.append(threading.get_ident())
            commit()

        journal._commit = checked_commit

        async def prompt(target, text):
            assert threading.get_ident() == loop_thread
            assert journal.record("c1")["status"] == "dispatched"
            return {"type": "agent_prompted"}

        dispatch = ConnectorDispatch(
            journal, SimpleNamespace(agent_prompt=prompt), host_id="home-mac", clock=lambda: NOW
        )
        message = _dispatch_msg("c1", "agent.prompt", {"agent_id": "a"}, {"text": "test"})
        try:
            assert (await dispatch.dispatch(message))["status"] == "succeeded"
            assert commits and loop_thread not in commits
            assert (await dispatch.dispatch(message))["status"] == "succeeded"
            assert dispatch.herdr_calls == 1
        finally:
            journal.close()

    asyncio.run(scenario())


def test_usage_cache_validation_does_not_wait_for_collector_on_loop(tmp_path: Path):
    async def scenario():
        store = SqliteUsageStore(tmp_path / "usage.db", "UTC", clock=lambda: NOW)
        service = UsageService(
            store,
            SimpleNamespace(sources=(), timezone="UTC", inferred_linking=True),
            "mac",
            tmp_path,
            lambda: NOW,
        )
        query = UsageQuery("mac", "2023-11-14", "2023-11-14")
        entered, release = threading.Event(), threading.Event()

        def collector():
            with store._lock:
                entered.set()
                assert release.wait(2)

        thread = threading.Thread(target=collector)
        try:
            assert (await service.page(query, "owner", "phone")).status_code == 200
            thread.start()
            while not entered.is_set():
                await asyncio.sleep(0)
            task = asyncio.create_task(service.page(query, "owner", "phone"))
            # A delayed coroutine cannot execute this release if page blocks the loop.
            await asyncio.sleep(0.02)
            assert task.done() and task.result().status_code == 200
            # The immutable as-of page bypasses the held collector; only its one
            # background validation waits, without occupying an HTTP waiter.
            assert len(service._inflight) == 1 and service._waiters == 0
            assert not next(iter(service._inflight.values())).done()
            release.set()
            await asyncio.gather(*service._inflight.values())
            assert not service._inflight
        finally:
            release.set()
            if thread.ident is not None:
                thread.join()
            await service.close()
            store.close()

    asyncio.run(scenario())


def test_usage_shutdown_waits_for_inflight_cache_validation(tmp_path: Path):
    async def scenario():
        store = SqliteUsageStore(tmp_path / "usage.db", "UTC", clock=lambda: NOW)
        service = UsageService(
            store,
            SimpleNamespace(sources=(), timezone="UTC", inferred_linking=True),
            "mac",
            tmp_path,
            lambda: NOW,
        )
        entered, release = threading.Event(), threading.Event()
        original = service._validate
        threads = []

        def delayed(*args):
            threads.append(threading.current_thread())
            entered.set()
            assert release.wait(2)
            return original(*args)

        service._validate = delayed
        query = UsageQuery("mac", "2023-11-14", "2023-11-14")
        task = asyncio.create_task(service.page(query, "owner", "phone"))
        try:
            async with asyncio.timeout(1):
                while not entered.is_set():
                    await asyncio.sleep(0)
            closing = asyncio.create_task(service.close())
            done, _ = await asyncio.wait({closing}, timeout=0.02)
            assert service._closed and not done
            release.set()
            await closing
            assert (await task).status_code == 503
            assert threads and all(not thread.is_alive() for thread in threads)
        finally:
            release.set()
            await service.close()
            store.close()

    asyncio.run(scenario())


def test_gateway_lifespan_starts_and_stops_the_always_on_watchdog():
    from relaydeck.companion.runtime import _uvicorn_logging_config
    from relaydeck.gateway.app import create_app
    from tests.security.helpers import make_state

    async def scenario():
        state, _ = make_state()
        app = create_app(state)
        before = set(threading.enumerate())
        async with app.router.lifespan_context(app):
            created = set(threading.enumerate()) - before
            assert any(t.name == "loop-watchdog" and t.is_alive() for t in created)
        assert not any(t.name == "loop-watchdog" and t.is_alive() for t in created)
        state.push.close()

    config = _uvicorn_logging_config()["loggers"]["relaydeck.gateway.loop_watch"]
    assert config == {"level": "WARNING", "handlers": ["default"], "propagate": False}
    asyncio.run(scenario())


@pytest.mark.parametrize("side", ["gateway", "connector"])
def test_queued_dispatch_checks_fresh_deadline_before_any_upstream_byte(tmp_path, side):
    async def scenario():
        clock = [NOW]
        journal = CommandJournal(tmp_path / "commands.db", clock=lambda: clock[0])
        backend = JournalBackend(journal, now_ms=lambda: clock[0])
        entered, release = threading.Event(), threading.Event()
        message = _dispatch_msg("ttl", "agent.prompt", {"agent_id": "a"}, {"text": "test"})
        message["expires_ms"] = NOW + 1
        if side == "gateway":
            backend.submit_command(
                {
                    "command_id": "ttl",
                    "host_id": "home-mac",
                    "kind": "agent.prompt",
                    "target": {"agent_id": "a"},
                    "payload": {"text": "test"},
                    "expires_ms": NOW + 1,
                }
            )

        def occupy_lane():
            entered.set()
            assert release.wait(2)

        try:
            occupying = asyncio.create_task(journal.run(occupy_lane))
            while not entered.is_set():
                await asyncio.sleep(0)
            dispatch = ConnectorDispatch(
                journal, SimpleNamespace(), host_id="home-mac", clock=lambda: clock[0]
            )
            work = (
                command_io(backend, "mark_command_dispatched", "ttl", NOW)
                if side == "gateway"
                else dispatch.dispatch(message)
            )
            task = asyncio.create_task(work)
            await asyncio.sleep(0.01)
            clock[0] += 2
            release.set()
            await occupying
            result = await task
            if side == "gateway":
                assert result is False
            else:
                assert result["error_code"] == "expired"
            assert journal.record("ttl")["status"] == "expired"
            assert dispatch.herdr_calls == 0
        finally:
            release.set()
            journal.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("outcome", ["cancel", "replace_host"])
def test_pairing_redemption_worker_cannot_replay_or_mint_for_replaced_host(tmp_path, outcome):
    async def scenario():
        state, clock, body = foundation(tmp_path)
        host = state.hosts.record_for_host("home-mac")
        state.pairing.open_window(host, state.public_origin, clock[0], 120)
        row = state.pairing.request(body, host, clock[0])
        state.pairing.decide(
            row["request_id"],
            row["matching_code"],
            host,
            state.public_origin,
            clock[0],
            approve=True,
        )
        entered, release = threading.Event(), threading.Event()
        redeem = state.pairing.redeem
        minted = []
        create = state.sessions.create

        def blocked(*args):
            entered.set()
            assert release.wait(2)
            return redeem(*args)

        def record_mint(*args, **kwargs):
            minted.append(True)
            return create(*args, **kwargs)

        state.pairing.redeem = blocked
        state.sessions.create = record_mint
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(create_app(state)), base_url=state.public_origin
            ) as client:
                path = f"/v1/pairing/{row['request_id']}/redeem"
                task = asyncio.create_task(client.post(path, json=proof()))
                while not entered.is_set():
                    await asyncio.sleep(0)
                if outcome == "cancel":
                    task.cancel()
                else:
                    state.hosts.revoke("home-mac")
                release.set()
                if outcome == "cancel":
                    with pytest.raises(asyncio.CancelledError):
                        await task
                else:
                    assert (await task).status_code == 404
                assert minted == []
                # Restore the host only for checking the consumed proof, not its authority.
                from relaydeck.gateway.pairing import PairingError

                with pytest.raises(PairingError):
                    redeem(row["request_id"], proof(), host, clock[0])
        finally:
            release.set()
            state.push.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("side", ["journal", "gateway"])
def test_ordered_work_wakes_waiter_on_another_event_loop(tmp_path, side):
    journal = CommandJournal(tmp_path / "commands.db", clock=lambda: NOW)
    backend = JournalBackend(journal, now_ms=lambda: NOW)
    entered, release, waiting = threading.Event(), threading.Event(), threading.Event()
    finished, errors = [], []
    submit = journal.submit

    def blocked_submit(**kwargs):
        entered.set()
        assert release.wait(3)
        return submit(**kwargs)

    journal.submit = blocked_submit
    command = {
        "command_id": "cross-loop",
        "principal": "owner",
        "host_id": "mac",
        "kind": "agent.prompt",
        "target": {"agent_id": "a"},
        "payload": {"text": "test"},
    }

    async def first():
        if side == "gateway":
            await command_io(backend, "submit_command", command)
        else:
            await journal.run(journal.submit, **command)
        finished.append("first")

    async def second():
        waiting.set()
        result = (
            await command_io(backend, "command_owner", "cross-loop")
            if side == "gateway"
            else await journal.run(journal.owner_of, "cross-loop")
        )
        assert result == ("owner", "mac")
        finished.append("second")

    def run(coro):
        try:
            asyncio.run(coro())
        except BaseException as exc:
            errors.append(type(exc).__name__)

    threads = [threading.Thread(target=run, args=(coro,), daemon=True) for coro in (first, second)]
    try:
        threads[0].start()
        assert entered.wait(2)
        threads[1].start()
        assert waiting.wait(2)
        time.sleep(0.02)
        release.set()
        for thread in threads:
            thread.join(timeout=2)
        assert not any(thread.is_alive() for thread in threads)
        assert errors == [] and finished == ["first", "second"]
    finally:
        release.set()
        journal.close()


def test_cancelled_queued_work_cannot_release_a_successor_before_its_predecessor():
    async def scenario():
        gate = OrderedGate()
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []

        async def first():
            async with gate.hold():
                entered.set()
                await release.wait()
                calls.append("first")

        async def next_work(name):
            async with gate.hold():
                calls.append(name)

        first_task = asyncio.create_task(first())
        await entered.wait()
        cancelled = asyncio.create_task(next_work("cancelled"))
        await asyncio.sleep(0)
        cancelled.cancel()
        third = asyncio.create_task(next_work("third"))
        await asyncio.sleep(0)
        cancelled.cancel()
        await asyncio.sleep(0)
        assert calls == [] and not third.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await cancelled
        await first_task
        await third
        assert calls == ["first", "third"]

    asyncio.run(scenario())
