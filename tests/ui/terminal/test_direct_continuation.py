"""Successful input resets hand native focus to their replacement epoch only."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from relaydeck.client.features.terminal.surfaces import direct_typing
from tests.support.host_harness import run
from tests.support.terminal.direct_typing import _change, _direct, _typing_key
from tests.support.terminal.latch_text import _inputs


@pytest.mark.parametrize("shell", [False, True])
@pytest.mark.parametrize("continuation", ["return", "modifier", "backspace"])
def test_direct_continuation_focuses_replacement_and_retires_old_native_id(
    shell: bool, continuation: str
) -> None:
    async def scenario() -> None:
        app, session, socket, focuses = await _direct(shell)
        try:
            if continuation == "return":
                await _change(app, "  hello")
                value, expected = "  hello\n", [b"hello", b"\r"]
            elif continuation == "modifier":
                await app.set_props_and_fire("mod-ctrl", {}, "click")
                value, expected = "  c", [b"\x03"]
            else:
                value, expected = " ", [b"\x7f"]
            await app.set_props_and_fire(_typing_key(app), {}, "focus")
            retired = app.find(_typing_key(app))._i
            before = len(focuses)
            await _change(app, value)
            replacement = app.find(_typing_key(app))
            assert replacement._i != retired
            assert focuses[before:] == [replacement._i]
            assert _inputs(socket) == expected
            # Events retain their originating native ID, including Return's
            # duplicate submit. None may revive the retired editor or CR.
            for event, data in (
                ("change", value),
                ("submit", ""),
                ("blur", ""),
                (
                    "selection_change",
                    {"selected_text": "", "selection": {"base_offset": 2, "extent_offset": 2}},
                ),
            ):
                await app.session.dispatch_event(retired, event, data)
            await app.settle()
            assert app.find(_typing_key(app))._i == replacement._i
            assert focuses[before:] == [replacement._i]
            assert _inputs(socket) == expected
            await _change(app, "  next")
            assert _inputs(socket) == expected + [b"next"]
            assert app.find(_typing_key(app))._i == replacement._i
            if not shell:
                assert session.store.draft == "retained compose draft"
        finally:
            await session.dispose()

    run(scenario())


@pytest.mark.parametrize("shell", [False, True])
@pytest.mark.parametrize("boundary", ["blur", "keyboard", "review", "authority"])
def test_direct_continuation_focus_task_is_consumed_after_intent_or_authority_loss(
    shell: bool, boundary: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = [1000.0]
    monkeypatch.setattr(direct_typing, "_typing_now", lambda: now[0])

    async def scenario() -> None:
        app, session, socket, focuses = await _direct(shell)
        pending: list[Any] = []
        spawn = direct_typing.spawn

        def hold_focus(coro: Any, *, label: str) -> Any:
            if label == "Direct typing focus":
                pending.append(coro)
                return None
            return spawn(coro, label=label)

        monkeypatch.setattr(direct_typing, "spawn", hold_focus)
        try:
            await app.set_props_and_fire(_typing_key(app), {}, "focus")
            before = len(focuses)
            await _change(app, "  \n")
            assert len(pending) == 1 and _inputs(socket) == [b"\r"]
            if boundary == "blur":
                await app.set_props_and_fire(_typing_key(app), {}, "blur")
            elif boundary == "keyboard":
                now[0] += 1
                key = "shell-keyboard-toggle" if shell else "agent-workflow-toggle"
                await app.set_props_and_fire(key, {}, "click")
            elif boundary == "review":
                await session.request_paste("review\ntext", require_review=True)
                await app.settle()
                assert session.pending_review is not None
            else:
                session._bump_generation("test")
                session._notify()
                await app.settle()
            while pending:
                await pending.pop(0)
            await app.settle()
            assert len(focuses) == before
            # An unrelated render must not replay a consumed handoff.
            session.set_bracketed_paste(True)
            await app.settle()
            assert not pending and len(focuses) == before
            assert _inputs(socket) == [b"\r"]
        finally:
            for coro in pending:
                coro.close()
            await session.dispose()

    run(scenario())


@pytest.mark.parametrize("shell", [False, True])
@pytest.mark.parametrize("continuation", ["return", "modifier", "backspace"])
@pytest.mark.parametrize("outcome", ["blur_during_send", "failure"])
def test_direct_continuation_does_not_refocus_a_failed_or_stale_send(
    shell: bool, continuation: str, outcome: str
) -> None:
    async def scenario() -> None:
        app, session, socket, focuses = await _direct(shell)
        started, release = asyncio.Event(), asyncio.Event()
        sender = session.sessions.keys._sender
        task: asyncio.Task[Any] | None = None

        async def delayed(data: bytes) -> None:
            started.set()
            await release.wait()
            if outcome == "failure":
                raise OSError("synthetic uncertain write")
            await sender(data)

        try:
            if continuation == "modifier":
                await app.set_props_and_fire("mod-ctrl", {}, "click")
            await app.set_props_and_fire(_typing_key(app), {}, "focus")
            value = {"return": "  \n", "modifier": "  c", "backspace": " "}[continuation]
            field = app.find(_typing_key(app))
            before = len(focuses)
            session.sessions.keys._sender = delayed
            app.session.apply_patch(field._i, {"value": value})
            task = asyncio.create_task(app.session.dispatch_event(field._i, "change", value))
            await asyncio.wait_for(started.wait(), 1)
            if outcome == "blur_during_send":
                await app.set_props_and_fire(_typing_key(app), {}, "blur")
            release.set()
            await task
            await app.settle()
            assert len(focuses) == before
            expected = {"return": b"\r", "modifier": b"\x03", "backspace": b"\x7f"}
            assert _inputs(socket) == ([] if outcome == "failure" else [expected[continuation]])
        finally:
            release.set()
            if task is not None:
                await asyncio.gather(task, return_exceptions=True)
            await session.dispose()

    run(scenario())
