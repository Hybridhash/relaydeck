"""Native focus handoffs across Direct tools actions and their two reset edges."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from relaydeck.client.features.terminal.surfaces import direct_typing
from tests.support.host_harness import run
from tests.support.terminal.direct_typing import _change, _direct, _typing_key
from tests.support.terminal.latch_text import _inputs


@pytest.mark.parametrize("shell", [False, True])
@pytest.mark.parametrize(("key", "encoded"), [("key-ctrl-c", b"\x03"), ("key-left", b"\x1b[D")])
@pytest.mark.parametrize("delayed", [False, True])
def test_direct_focused_tools_key_hands_focus_to_only_the_final_native_epoch(
    shell: bool, key: str, encoded: bytes, delayed: bool
) -> None:
    async def scenario() -> None:
        app, session, socket, focuses = await _direct(shell)
        sender = session.sessions.keys._sender
        started, release = asyncio.Event(), asyncio.Event()
        task: asyncio.Task[Any] | None = None

        async def held(data: bytes) -> None:
            started.set()
            await release.wait()
            await sender(data)

        try:
            await _change(app, "  old")
            await app.set_props_and_fire(_typing_key(app), {}, "focus")
            retired = app.find(_typing_key(app))._i
            count = len(focuses)
            if delayed:
                session.sessions.keys._sender = held
                task = asyncio.create_task(
                    app.session.dispatch_event(app.find(key)._i, "click", "")
                )
                await asyncio.wait_for(started.wait(), 1)
                await app.settle()
                intermediate = app.find(_typing_key(app))._i
                assert intermediate != retired
                assert focuses[count:] == []
                await app.session.dispatch_event(retired, "blur", "")
                release.set()
                await task
                await app.settle()
            else:
                await app.set_props_and_fire(key, {}, "click")
                intermediate = retired
            replacement = app.find(_typing_key(app))._i
            assert replacement not in {retired, intermediate}
            assert focuses[count:] == [replacement]
            assert _inputs(socket) == [b"old", encoded]
            for old in {retired, intermediate}:
                for event, data in (
                    ("focus", ""),
                    ("blur", ""),
                    ("change", "  oldlate"),
                    ("submit", ""),
                ):
                    await app.session.dispatch_event(old, event, data)
            await app.settle()
            assert focuses[count:] == [replacement]
            assert app.find(_typing_key(app))._i == replacement
            session.sessions.keys._sender = sender
            await _change(app, "  next")
            assert _inputs(socket) == [b"old", encoded, b"next"]
        finally:
            release.set()
            if task is not None:
                await asyncio.gather(task, return_exceptions=True)
            await session.dispose()

    run(scenario())


@pytest.mark.parametrize("shell", [False, True])
@pytest.mark.parametrize("key", ["key-ctrl-c", "key-left"])
@pytest.mark.parametrize("state", ["unfocused", "blurred", "closed"])
def test_direct_tools_key_does_not_request_focus_without_current_native_focus(
    shell: bool, key: str, state: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = [1000.0]
    monkeypatch.setattr(direct_typing, "_typing_now", lambda: now[0])

    async def scenario() -> None:
        app, session, socket, focuses = await _direct(shell)
        try:
            if state != "unfocused":
                await app.set_props_and_fire(_typing_key(app), {}, "focus")
                retired = app.find(_typing_key(app))._i
                if state == "blurred":
                    await app.set_props_and_fire(_typing_key(app), {}, "blur")
                else:
                    now[0] += 1
                    await app.set_props_and_fire(
                        "shell-keyboard-toggle" if shell else "agent-workflow-toggle", {}, "click"
                    )
                await app.session.dispatch_event(retired, "focus", "")
            count = len(focuses)
            await app.set_props_and_fire(key, {}, "click")
            assert len(focuses) == count
            assert _inputs(socket) == [b"\x03" if key == "key-ctrl-c" else b"\x1b[D"]
        finally:
            await session.dispose()

    run(scenario())


@pytest.mark.parametrize("shell", [False, True])
@pytest.mark.parametrize("boundary", ["blur", "keyboard", "review", "authority"])
def test_direct_pending_tools_handoff_cannot_reopen_after_focus_or_authority_loss(
    shell: bool, boundary: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = [1000.0]
    monkeypatch.setattr(direct_typing, "_typing_now", lambda: now[0])

    async def scenario() -> None:
        app, session, socket, focuses = await _direct(shell)
        sender = session.sessions.keys._sender
        started, release = asyncio.Event(), asyncio.Event()
        task: asyncio.Task[Any] | None = None

        async def held(data: bytes) -> None:
            started.set()
            await release.wait()
            await sender(data)

        try:
            await app.set_props_and_fire(_typing_key(app), {}, "focus")
            count = len(focuses)
            session.sessions.keys._sender = held
            task = asyncio.create_task(
                app.session.dispatch_event(app.find("key-ctrl-c")._i, "click", "")
            )
            await asyncio.wait_for(started.wait(), 1)
            await app.settle()
            assert len(focuses) == count
            if boundary == "blur":
                await app.set_props_and_fire(_typing_key(app), {}, "blur")
            elif boundary == "keyboard":
                now[0] += 1
                await app.set_props_and_fire(
                    "shell-keyboard-toggle" if shell else "agent-workflow-toggle", {}, "click"
                )
            elif boundary == "review":
                await session.request_paste("review\ntext", require_review=True)
                await app.settle()
            else:
                session._bump_generation("test")
                session._notify()
                await app.settle()
            release.set()
            await task
            await app.settle()
            assert len(focuses) == count
            session.set_bracketed_paste(True)
            await app.settle()
            assert len(focuses) == count
            assert _inputs(socket) in ([], [b"\x03"])
        finally:
            release.set()
            if task is not None:
                await asyncio.gather(task, return_exceptions=True)
            await session.dispose()

    run(scenario())


@pytest.mark.parametrize("shell", [False, True])
def test_direct_overlapping_tools_keys_only_focus_after_both_actions_settle(shell: bool) -> None:
    async def scenario() -> None:
        app, session, socket, focuses = await _direct(shell)
        sender = session.sessions.keys._sender
        started, release = asyncio.Event(), asyncio.Event()
        tasks: list[asyncio.Task[Any]] = []

        async def held(data: bytes) -> None:
            if not started.is_set():
                started.set()
                await release.wait()
            await sender(data)

        try:
            await app.set_props_and_fire(_typing_key(app), {}, "focus")
            count = len(focuses)
            session.sessions.keys._sender = held
            for key in ("key-ctrl-c", "key-left"):
                tasks.append(
                    asyncio.create_task(app.session.dispatch_event(app.find(key)._i, "click", ""))
                )
                await asyncio.wait_for(started.wait(), 1)
                await app.settle()
                assert len(focuses) == count
            release.set()
            await asyncio.gather(*tasks)
            await app.settle()
            assert focuses[count:] == [app.find(_typing_key(app))._i]
            assert _inputs(socket) == [b"\x03", b"\x1b[D"]
        finally:
            release.set()
            await asyncio.gather(*tasks, return_exceptions=True)
            await session.dispose()

    run(scenario())
