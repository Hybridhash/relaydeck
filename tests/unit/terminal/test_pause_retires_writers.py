"""Pause retires callbacks and writers before waiting on lease-renewal cleanup."""

import asyncio
from typing import Any

import pytest

from relaydeck.client.features.terminal.callbacks import foreground_action
from tests.support.sync.lifecycle import _agent, _shell


@pytest.mark.parametrize("kind", ["agent", "shell"])
def test_pause_fences_before_first_await_preserving_terminal_state(
    kind: str, monkeypatch: Any
) -> None:
    async def scenario() -> None:
        session: Any
        if kind == "agent":
            session, ops, factory, submitted = _agent()
            session.open()
            await session.confirm_takeover()
            session.on_draft_change("preserved draft")
            composer = session.composer_callbacks()["on_send"]
        else:
            session, ops, factory = _shell()
        if kind == "shell":
            await session.open("term-1")
        token = session.input_token
        assert token is not None
        keys = session.sessions.keys
        keys.toggle_modifier("ctrl")
        old_callback = foreground_action(session, session.disconnect)
        entered, release = asyncio.Event(), asyncio.Event()
        stop = session.stop_lease_renewal

        async def delayed_stop() -> None:
            entered.set()
            await release.wait()
            await stop()

        monkeypatch.setattr(session, "stop_lease_renewal", delayed_stop)
        task = asyncio.create_task(session.pause())
        try:
            await entered.wait()
            assert session.paused and session.foreground
            assert session.input_token is None and not session._token_live(token)
            assert await old_callback() == "ignored"
            assert keys.pending_modifiers == frozenset({"ctrl"})
            assert keys.press_key("x") is None
            if kind == "agent":
                assert await composer() == "ignored"
                assert session.composer_props()["draft"] == "preserved draft"
                assert submitted == []
            release.set()
            await task
            await session.resume_after_pause()
            assert await old_callback() == "ignored"
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
            await session.dispose(clear_sensitive=True)

    asyncio.run(scenario())


@pytest.mark.parametrize("operation", ["agent", "shell-open", "shell-takeover", "shell-reconnect"])
def test_late_lease_after_pause_is_released_without_binding_a_writer(
    operation: str, monkeypatch: Any
) -> None:
    async def scenario() -> None:
        session: Any
        if operation == "agent":
            session, ops, factory, submitted = _agent()
            session.open()
        else:
            session, ops, factory = _shell()
            if operation != "shell-open":
                await session.open("term-1")
                await session.disconnect()
        entered, release = asyncio.Event(), asyncio.Event()
        acquire = ops.acquire_lease
        returned: list[str] = []

        async def held(attachment: str, mode: str) -> Any:
            lease = await acquire(attachment, mode)
            returned.append(lease.lease_id)
            entered.set()
            await release.wait()
            return lease

        monkeypatch.setattr(ops, "acquire_lease", held)
        action = (
            session.confirm_takeover
            if operation == "agent"
            else session.take_over
            if operation == "shell-takeover"
            else session.reconnect
            if operation == "shell-reconnect"
            else lambda: session.open("term-1")
        )
        previous_controller = session.controller
        task = asyncio.create_task(action())
        try:
            await entered.wait()
            await session.pause()
            socket_count = len(factory.sockets)
            release.set()
            await task
            assert returned[0] in ops.releases
            assert (
                session.paused
                and session.controller is previous_controller
                and session.lease is None
            )
            assert session.input_token is None and len(factory.sockets) == socket_count
            assert ops.closes == []  # Remote process survives the local lease cleanup.
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
            await session.dispose(clear_sensitive=True)

    asyncio.run(scenario())
