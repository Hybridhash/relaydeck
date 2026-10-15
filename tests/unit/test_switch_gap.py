"""Overlap dedupe and stale retained-watch jobs cannot replace explicit control."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from relaydeck.client.features.sync.controller import TerminalStreamClient
from relaydeck.client.features.terminal.warm_watch import watch_only
from relaydeck.client.retained_sessions import RetainedSessions
from relaydeck.contracts.wire.terminal import encode_output
from tests.support.scripted_sessions import ScriptedTerminalSocket, make_scripted_sessions
from tests.support.terminal.watch_drops import until, watching


def test_stream_skips_old_chunks_and_trims_overlap_without_rewinding_or_input() -> None:
    async def scenario() -> None:
        socket = ScriptedTerminalSocket()
        applied: list[tuple[int, bytes]] = []
        gaps: list[str] = []
        client = TerminalStreamClient(
            lambda _: socket,
            lambda offset, data: applied.append((offset, data)),
            lambda _: None,
            gaps.append,
        )
        await client.open("lease", 1, "ticket", "e1", 5)
        socket.queue(
            ("bytes", encode_output("e1", 0, b"old")),
            ("bytes", encode_output("e1", 3, b"deNEW")),
            ("bytes", encode_output("e1", 3, b"deNEW")),
            ("bytes", encode_output("e1", 8, b"!")),
        )
        reader = asyncio.create_task(client.run())
        try:
            await until(lambda: client.next_offset == 9 or reader.done())
            assert not reader.done() and not gaps
            assert applied == [(5, b"NEW"), (8, b"!")]
            assert client.offered_offset is None and not socket.sent
        finally:
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
            await client.close()

    asyncio.run(scenario())


def test_old_epoch_overlap_still_requires_reset() -> None:
    async def scenario() -> None:
        socket = ScriptedTerminalSocket()
        applied: list[bool] = []
        resets: list[str] = []
        client = TerminalStreamClient(
            lambda _: socket,
            lambda *_: applied.append(True),
            lambda _: None,
            lambda _: None,
            on_reset=resets.append,
        )
        await client.open("lease", 2, "ticket", "e2", 5)
        socket.queue(("bytes", encode_output("e1", 0, b"already rendered")))
        await client.run()
        assert resets == ["epoch e2 -> e1"] and not applied and client.next_offset == 5
        await client.close()

    asyncio.run(scenario())


def test_empty_frame_at_current_cursor_preserves_quiet_terminal_liveness() -> None:
    async def scenario() -> None:
        socket = ScriptedTerminalSocket()
        applied: list[tuple[int, bytes]] = []
        client = TerminalStreamClient(
            lambda _: socket,
            lambda offset, data: applied.append((offset, data)),
            lambda _: None,
            lambda _: pytest.fail("contiguous replay must not gap"),
        )
        await client.open("lease", 1, "ticket", "e1", 3)
        socket.queue(
            ("bytes", encode_output("e1", 0, b"old")),
            ("bytes", encode_output("e1", 3, b"")),
            ("bytes", encode_output("e1", 3, b"!")),
        )
        reader = asyncio.create_task(client.run())
        try:
            await until(lambda: client.next_offset == 4 or reader.done())
            assert not reader.done() and applied == [(3, b""), (3, b"!")]
            assert not socket.sent
        finally:
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
            await client.close()

    asyncio.run(scenario())


def test_pending_warm_acquisition_cannot_publish_over_explicit_takeover(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        scripted, session, term = await watching()
        # Begin a real control->view conversion, then hold its acquisition.
        session.request_takeover()
        await session.confirm_takeover()
        entered, finish = asyncio.Event(), asyncio.Event()
        acquire = scripted.ops.acquire_lease

        async def delayed(attachment: str, mode: str) -> Any:
            lease = await acquire(attachment, mode)
            if mode == "view":
                entered.set()
                await finish.wait()
            return lease

        monkeypatch.setattr(scripted.ops, "acquire_lease", delayed)
        task = asyncio.create_task(watch_only(session, lambda: True))
        try:
            await until(entered.is_set)
            session.request_takeover()
            await session.confirm_takeover()
            control, lease, token = session.controller, session.lease, session.input_token
            finish.set()
            assert not await task
            assert session.controller is control and session.lease is lease
            assert session.phase == "live" and session.keys_enabled and session.input_token == token
            assert lease.lease_id not in scripted.ops.releases
            assert session.current_term is term and session.store.draft == "unsent draft"
            assert not scripted.submitted and not scripted.prompted
            assert not any(
                kind == "bytes"
                for _, _, socket in scripted.streams.opened
                for kind, _ in socket.sent
            )
        finally:
            finish.set()
            await asyncio.gather(task, return_exceptions=True)
            await session.dispose()

    asyncio.run(scenario())


@pytest.mark.parametrize("entrypoint", ["preconnect", "resume_watch"])
def test_preconnect_waiting_for_warm_task_cannot_close_replacement_control(entrypoint: str) -> None:
    async def scenario() -> None:
        scripted = make_scripted_sessions()
        pool = RetainedSessions()
        entry = await pool.acquire(
            "mac",
            "agent",
            "a",
            "h1",
            lambda: scripted.factory.make_agent(
                host_id="mac", host_epoch="h1", agent_id="a", target="a"
            ),
        )
        session = entry.session
        session.open()
        await session.confirm_takeover()
        entered, finish = asyncio.Event(), asyncio.Event()

        async def held() -> None:
            entered.set()
            await finish.wait()

        entry.warm_task = asyncio.create_task(held())
        pending = asyncio.create_task(getattr(pool, entrypoint)(entry))
        try:
            await until(entered.is_set)
            await asyncio.sleep(0)  # preconnect is now waiting for the held warm job.
            session.request_takeover()
            await session.confirm_takeover()
            control, lease = session.controller, session.lease
            before = list(scripted.ops.leases), list(scripted.ops.releases)
            finish.set()
            assert not await pending
            assert session.controller is control and session.lease is lease and session.keys_enabled
            assert not scripted.streams.opened[-1][2].closed
            assert (scripted.ops.leases, scripted.ops.releases) == before
        finally:
            finish.set()
            await asyncio.gather(pending, return_exceptions=True)
            await pool.shutdown()

    asyncio.run(scenario())


def test_cancelled_warm_conversion_preserves_cursor_without_a_controller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        scripted, session, term = await watching()
        first_reader = asyncio.create_task(session.run())
        first_socket = scripted.streams.opened[-1][2]
        first_socket.queue(("bytes", encode_output(session.lease.epoch, 0, b"old")))
        await until(lambda: session.term_state.applied_offset == 3)
        first_reader.cancel()
        await asyncio.gather(first_reader, return_exceptions=True)
        entered, finish = asyncio.Event(), asyncio.Event()
        acquire = scripted.ops.acquire_lease

        async def delayed(attachment: str, mode: str) -> Any:
            lease = await acquire(attachment, mode)
            entered.set()
            await finish.wait()
            return lease

        # A failed/dropped view needs a replacement view. Returning to this
        # retained screen while acquisition is pending invalidates the old job.
        session._phase = "disconnected"
        session.set_foreground(False)
        monkeypatch.setattr(scripted.ops, "acquire_lease", delayed)
        warm = asyncio.create_task(watch_only(session, lambda: True))
        pool = RetainedSessions()
        try:
            await until(entered.is_set)
            session.set_foreground(True)
            finish.set()
            assert not await warm
            assert session.controller is None and session.current_term is term
            assert session.term_state.epoch and session.term_state.applied_offset == 3
            monkeypatch.setattr(scripted.ops, "acquire_lease", acquire)
            entry = await pool.acquire("mac", "agent", "a", "h1", lambda: session)
            assert await pool.preconnect(entry)
            assert session.controller is not None
            assert session.controller._client.next_offset == 3
            assert entry.task is not None and not entry.task.done()
            socket = scripted.streams.opened[-1][2]
            socket.queue(("bytes", encode_output(session.lease.epoch, 0, b"oldnew")))
            await until(lambda: session.term_state.applied_offset == 6)
            assert bytes(session.output_log) == b"oldnew"
            assert session.current_term is term and session.lease.mode == "view"
            assert len(scripted.ops.attaches) == 1 and not scripted.submitted
        finally:
            finish.set()
            await asyncio.gather(warm, return_exceptions=True)
            await pool.shutdown()
            await session.dispose()

    asyncio.run(scenario())
