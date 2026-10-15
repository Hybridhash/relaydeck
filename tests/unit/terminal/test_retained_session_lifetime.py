"""Real session/controller lifetime, with explicit transport/engine doubles."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from relaydeck_terminal.terminal import RelayDeckTerminal

from relaydeck.client.features.terminal.agent_session import AgentSession
from relaydeck.client.features.terminal.output_log import OUTPUT_LOG_LIMIT
from relaydeck.client.features.terminal.retention import RetentionFullError
from relaydeck.client.retained_sessions import SLOT_IDLE_MS, RetainedSession, RetainedSessions
from tests.support.scripted_sessions import make_scripted_sessions


def test_retained_agent_switch_keeps_engine_writer_draft_and_offscreen_output() -> None:
    async def scenario() -> None:
        sessions = make_scripted_sessions()
        pool = RetainedSessions()

        def agent() -> AgentSession:
            session = sessions.factory.make_agent(
                host_id="home-mac", host_epoch="e1", agent_id="a", target="a"
            )
            assert isinstance(session, AgentSession)
            session.open()
            return session

        try:
            a = await pool.acquire("home-mac", "agent", "home-mac:a", "e1", agent)
            await a.session.confirm_takeover()
            term = RelayDeckTerminal(key="retained-engine", input_enabled=True)
            received: list[bytes] = []

            def write(engine: Any, data: bytes) -> bool:
                received.append(data)
                return True

            a.session.bind_view(term, write=write)
            a.session.on_draft_change("kept draft")
            old_token = a.session.input_token
            old_composer = a.session.composer_callbacks()
            controller = a.session.controller
            task = a.task
            b = await pool.acquire(
                "other-mac",
                "shell",
                "other-mac:a",
                "e2",
                lambda: sessions.factory.make_shell(host_id="other-mac"),
            )
            await b.session.open("a")
            assert (
                a.session.current_term is term
                and a.session.controller is controller
                and a.task is task
            )
            assert not a.session.foreground and a.session.input_token is None
            assert await a.session.handle_view_input(1, b"late", token=old_token) == "dropped"
            old_composer["on_draft_change"]("wrong")
            assert await old_composer["on_send"]() == "ignored"
            a.session._handle_bytes(b"offscreen output")
            assert (
                received == [b"offscreen output"]
                and bytes(a.session.output_log) == b"offscreen output"
            )
            # Native renderer state is represented by the same control identity;
            # physical Flutter scroll/layout behavior is still a separate gate.
            again = await pool.acquire(
                "home-mac", "agent", "home-mac:a", "e1", lambda: pytest.fail("must reuse")
            )
            assert again is a and again.session.current_term is term
            assert a.session.composer_props()["draft"] == "kept draft"
            assert a.session.input_token != old_token
            assert len(sessions.ops.attaches) == 2 and not sessions.ops.closes
            assert sessions.submitted == [] and sessions.prompted == []
            pool.release()
            a.session._handle_bytes(b"x" * (OUTPUT_LOG_LIMIT + 17))
            assert len(a.session.output_log) == OUTPUT_LOG_LIMIT
            assert a.session.output_log.dropped_bytes > 0
        finally:
            await pool.shutdown()
        assert all(session.disposed for session in sessions.factory.created)
        assert all(not session.output_log for session in sessions.factory.created)
        assert not sessions.ops.closes and not pool.entries()

    asyncio.run(scenario())


def test_retained_shell_lru_expiry_visibility_and_epoch_revocation() -> None:
    async def scenario() -> None:
        sessions = make_scripted_sessions()
        now = [0]
        pool = RetainedSessions(clock=lambda: now[0])

        async def acquire(index: int, epoch: str = "e1") -> RetainedSession:
            entry = await pool.acquire(
                "h", "shell", f"h:{index}", epoch, lambda: sessions.factory.make_shell(host_id="h")
            )
            assert entry is not None
            return entry

        try:
            entries = []
            for index in range(5):
                entry = await acquire(index)
                await entry.session.open(str(index))
                entries.append(entry)
                now[0] += 1000
            token = entries[0].session.input_token
            assert (
                await entries[0].session.handle_view_input(1, b"hidden", token=token) == "dropped"
            )
            sixth = await acquire(5)
            await sixth.session.open("5")
            assert entries[0].session.disposed and len(pool.registry.keys("h")) == 5
            assert not sessions.ops.closes
            pool.registry.set_visible("h", pool.registry.keys("h"))
            with pytest.raises(RetentionFullError):
                await acquire(6)
            assert len(pool.entries()) == 5 and pool.foreground is sixth
            pool.registry.set_visible("h", (sixth.slot,))
            now[0] += SLOT_IDLE_MS
            await pool.evict_idle()
            assert pool.entries() == (sixth,) and not sixth.session.disposed
            replacement = await acquire(5, "e2")
            assert replacement is not sixth and sixth.session.disposed
            await replacement.session.open("5")
            await pool.prune({}, set())
            assert not pool.entries() and replacement.session.disposed
            assert not sessions.ops.closes
        finally:
            await pool.shutdown()

    asyncio.run(scenario())


def test_retention_teardown_failure_still_fences_clears_and_removes_slots() -> None:
    async def scenario() -> None:
        sessions = make_scripted_sessions()
        pool = RetainedSessions()
        one = await pool.acquire(
            "h", "shell", "h:one", "e1", lambda: sessions.factory.make_shell(host_id="h")
        )
        await one.session.open("one")
        original = one.session.dispose

        async def failed_dispose(clear_sensitive: bool = False) -> None:
            await original(clear_sensitive=clear_sensitive)
            raise RuntimeError("synthetic teardown failure")

        one.session.dispose = failed_dispose
        two = await pool.acquire(
            "h", "shell", "h:two", "e1", lambda: sessions.factory.make_shell(host_id="h")
        )
        await two.session.open("two")
        one.session._handle_bytes(b"private output")
        await pool.shutdown()
        assert pool.error == "retained_detach_failed" and not pool.entries()
        assert one.session.disposed and two.session.disposed
        assert not one.session.output_log and not two.session.output_log
        assert not sessions.ops.closes

    asyncio.run(scenario())
