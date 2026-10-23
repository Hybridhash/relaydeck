"""Late native edits and deferred opens cannot restore cancelled keyboard intent."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from relaydeck.client.features.terminal.surfaces import direct_typing
from tests.support.host_harness import run
from tests.support.terminal.direct_typing import _change, _direct, _typing_key
from tests.support.terminal.latch_text import _inputs


@pytest.mark.parametrize("shell", [False, True])
@pytest.mark.parametrize("modifier", [False, True])
def test_direct_parked_blur_then_late_change_never_refocuses(shell: bool, modifier: bool) -> None:
    async def scenario() -> None:
        app, session, socket, focuses = await _direct(shell)
        try:
            await _change(app, "  abc")
            if modifier:
                await app.set_props_and_fire("mod-ctrl", {}, "click")
            await app.set_props_and_fire(_typing_key(app), {}, "focus")
            field = app.find(_typing_key(app))
            value = "  abcc" if modifier else "  abc\n"
            app.session.apply_patch(field._i, {"value": value})
            await app.session.dispatch_event(field._i, "blur", "")
            await app.settle()
            assert app.find(_typing_key(app))._i == field._i  # Parked native value precedes change.
            count = len(focuses)
            await app.session.dispatch_event(field._i, "change", value)
            await app.settle()
            assert _inputs(socket) == [b"abc", b"\x03" if modifier else b"\r"]
            assert app.find(_typing_key(app))._i != field._i
            assert focuses[count:] == []
            if not shell:
                assert session.store.draft == "retained compose draft"
        finally:
            await session.dispose()

    run(scenario())


@pytest.mark.parametrize("shell", [False, True])
@pytest.mark.parametrize(
    "boundary",
    [
        "blur", "blur_unrendered", "keyboard", "keyboard_unrendered", "review", "review_unrendered",
        "authority", "authority_unrendered",
    ],
)  # fmt: skip
def test_direct_deferred_open_is_cancelled_at_intent_or_authority_loss(
    shell: bool, boundary: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = [1000.0]
    monkeypatch.setattr(direct_typing, "_typing_now", lambda: now[0])

    async def scenario() -> None:
        app, session, socket, focuses = await _direct(shell)
        sender = session.sessions.keys._sender
        started, release = asyncio.Event(), asyncio.Event()
        task: asyncio.Task[Any] | None = None
        toggle = "shell-keyboard-toggle" if shell else "agent-workflow-toggle"
        loss = boundary.removesuffix("_unrendered")
        unrendered = boundary != loss

        async def held(data: bytes) -> None:
            started.set()
            await release.wait()
            await sender(data)

        try:
            now[0] += 1
            await app.set_props_and_fire(toggle, {}, "click")
            session.sessions.keys._sender = held
            task = asyncio.create_task(
                app.session.dispatch_event(app.find("key-ctrl-c")._i, "click", "")
            )
            await asyncio.wait_for(started.wait(), 1)
            await app.settle()
            count = len(focuses)
            if shell:
                now[0] += 1
                if unrendered:
                    await app.session.dispatch_event(app.find(toggle)._i, "click", "")
                else:
                    await app.set_props_and_fire(toggle, {}, "click")
            # Agent's idle field opens the keyboard through its real focus callback.
            field = app.find(_typing_key(app))
            if unrendered:
                await app.session.dispatch_event(field._i, "focus", "")
            else:
                await app.set_props_and_fire(_typing_key(app), {}, "focus")
                assert app.find(_typing_key(app)).visible
            assert focuses[count:] == []
            if loss == "blur":
                await app.session.dispatch_event(app.find(_typing_key(app))._i, "blur", "")
            elif loss == "keyboard":
                now[0] += 1
                if unrendered:
                    await app.session.dispatch_event(app.find(toggle)._i, "click", "")
                else:
                    await app.set_props_and_fire(toggle, {}, "click")
            elif loss == "review":
                await session.request_paste("review\ntext", require_review=True)
                await app.settle()
            else:
                session._bump_generation("test")
                session._notify()
                await app.settle()
            release.set()
            await task
            await app.settle()
            assert focuses[count:] == []
            assert _inputs(socket) == [b"\x03"]
            session.set_bracketed_paste(True)
            await app.settle()
            assert focuses[count:] == []
            # Cancellation must not disable a later explicit gesture.
            if loss in ("blur", "keyboard"):
                if loss != "keyboard":
                    now[0] += 1
                    await app.set_props_and_fire(toggle, {}, "click")
                now[0] += 1
                await app.set_props_and_fire(toggle, {}, "click")
                assert focuses[count:] == [app.find(_typing_key(app))._i]
            assert _inputs(socket) == [b"\x03"]
            if not shell:
                assert session.store.draft == "retained compose draft"
        finally:
            release.set()
            if task is not None:
                await asyncio.gather(task, return_exceptions=True)
            await session.dispose()

    run(scenario())
