"""Async-safe event handlers.

Flet only awaits `async def` handlers (`iscoroutinefunction`): a sync
lambda wrapping an async callback drops the coroutine unawaited. The
`events` helpers produce real coroutine handlers that await awaitable
results and leave sync callbacks untouched; `spawn` schedules
non-Flet listener callbacks with exception logging instead of silent
drops or unawaited warnings.
"""

from __future__ import annotations

import asyncio
import inspect

import pytest

from relaydeck.client.features.terminal.events import (
    click,
    event_index,
    event_text,
    spawn,
    value_change,
)


def test_click_handler_is_a_coroutine_function() -> None:
    async def scenario() -> None:
        calls: list[str] = []

        async def target(name: str) -> str:
            calls.append(name)
            return name

        handler = click(target, "enter")
        assert inspect.iscoroutinefunction(handler)
        await handler(object())
        assert calls == ["enter"]

    asyncio.run(scenario())


def test_click_handler_runs_sync_callbacks_too() -> None:
    async def scenario() -> None:
        calls: list[str] = []
        handler = click(calls.append, "x")
        assert inspect.iscoroutinefunction(handler)
        await handler(object())
        assert calls == ["x"]

    asyncio.run(scenario())


def test_value_change_prefers_payload_over_stale_control() -> None:
    """Pinned flet 1.0.3 sends the new value as the event payload plus a
    patch; the patch may land on a different indexed control than
    dispatch resolves, so `control.value` can be stale (browser: typed
    composer text reset on round-trip). The payload wins — including a
    real clear ("") — and control props are only a fallback for
    synthetic events without data."""

    async def scenario() -> None:
        seen: list[str] = []

        class _Control:
            value = "stale"

        async def target(text: str) -> None:
            seen.append(text)

        handler = value_change(target)
        assert inspect.iscoroutinefunction(handler)

        class _Live:
            control = _Control()
            data = "typed"

        await handler(_Live())
        assert seen == ["typed"]

        class _Cleared:
            control = _Control()
            data = ""

        await handler(_Cleared())
        assert seen == ["typed", ""]

        class _Synthetic:
            control = _Control()
            data = None

        await handler(_Synthetic())
        assert seen == ["typed", "", "stale"]

    asyncio.run(scenario())


def test_event_text_and_index_fall_back_without_data() -> None:
    class _Control:
        value = "patched"
        selected_index = 2

    class _Event:
        control = _Control()
        data = None

    assert event_text(_Event()) == "patched"
    assert event_index(_Event()) == 2

    class _Payload:
        control = _Control()
        data = 1

    assert event_index(_Payload()) == 1

    class _Hostile:
        control = object()
        data = {"value": "nope"}

    assert event_text(_Hostile()) == ""
    assert event_index(_Hostile()) == 0


def test_spawn_runs_coro_and_logs_failures() -> None:
    async def scenario() -> None:
        done: list[str] = []

        async def ok() -> None:
            done.append("ok")

        await spawn(ok(), label="rd1t-ok")
        assert done == ["ok"]

        async def boom() -> None:
            raise ValueError("rd1t-boom")

        # spawn() itself must not raise to the sync caller; the task
        # carries the exception (and the done-callback logs it).
        task = spawn(boom(), label="rd1t-boom")
        with pytest.raises(ValueError, match="rd1t-boom"):
            await task

    asyncio.run(scenario())
