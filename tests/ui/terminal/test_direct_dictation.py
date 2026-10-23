"""Cumulative touch edits, explicit boundaries, and no uncertain input replay."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from flet.messaging.protocol import MessageAction

from relaydeck.client.features.terminal.paste_review import CONFIRM_KEY
from tests.support.fakes import FakeClipboard
from tests.support.host_harness import run
from tests.support.terminal.direct_typing import _ack_reset, _change, _direct, _typing_key
from tests.support.terminal.latch_text import _inputs


@pytest.mark.parametrize("shell", [False, True])
def test_direct_dictation_growth_identical_commit_and_unrelated_render(shell: bool) -> None:
    async def scenario() -> None:
        app, session, socket, _ = await _direct(shell)
        try:
            start = len(app.conn.sent)
            field_id = app.find(_typing_key(app))._i
            for value in ("  h", "  hello", "  hello wor", "  hello world", "  hello world"):
                await _change(app, value)
                assert app.find(_typing_key(app)).value == value
                assert not app.find(_typing_key(app)).data["direct_reset"]
            session.set_bracketed_paste(True)
            await app.settle()
            assert app.find(_typing_key(app)).value == "  hello world"
            assert app.find(_typing_key(app))._i == field_id
            assert _inputs(socket) == [b"h", b"ello", b" wor", b"ld"]
            assert not any(
                len(op) == 4 and op[2] in ("value", "selection")
                for message in app.conn.sent[start:]
                if message.action == MessageAction.PATCH_CONTROL
                for op in message.body.patch
            )
        finally:
            await session.dispose()

    run(scenario())


@pytest.mark.parametrize("shell", [False, True])
@pytest.mark.parametrize(
    ("before", "after", "removed", "inserted"),
    [
        ("  hello world", "  hello words", 2, "ds"),
        ("  cafe\u0301😀", "  café😎", 3, "é😎"),
        ("  你好😀", "  你好😎", 1, "😎"),
    ],
)
def test_direct_dictation_corrections_use_sent_code_points(
    shell: bool, before: str, after: str, removed: int, inserted: str
) -> None:
    async def scenario() -> None:
        app, session, socket, _ = await _direct(shell)
        try:
            await _change(app, before)
            await _change(app, after)
            assert _inputs(socket) == [before[2:].encode()] + [b"\x7f"] * removed + [
                inserted.encode()
            ]
            assert app.find(_typing_key(app)).value == after
        finally:
            await session.dispose()

    run(scenario())


@pytest.mark.parametrize("shell", [False, True])
def test_direct_cap_crossing_keeps_revision_until_blur_and_reopen(shell: bool) -> None:
    async def scenario() -> None:
        app, session, socket, _ = await _direct(shell)
        try:
            text = "a" * 4100
            await _change(app, "  " + text)
            await _change(app, "  " + text[:-1] + "b")
            assert _inputs(socket) == [text.encode(), b"\x7f", b"b"]
            assert app.find(_typing_key(app)).value == "  " + text[:-1] + "b"
            await app.set_props_and_fire(_typing_key(app), {}, "blur")
            assert app.find(_typing_key(app)).value == "  "
            await _ack_reset(app)
            await app.set_props_and_fire(_typing_key(app), {}, "focus")
            await _change(app, "  fresh")
            assert _inputs(socket)[-1] == b"fresh"
        finally:
            await session.dispose()

    run(scenario())


@pytest.mark.parametrize("shell", [False, True])
@pytest.mark.parametrize("boundary", ["ctrl", "arrow", "paste"])
def test_direct_correction_never_deletes_across_external_boundary(
    shell: bool, boundary: str
) -> None:
    async def scenario() -> None:
        app, session, socket, _ = await _direct(shell)
        try:
            await _change(app, "  prior")
            if boundary == "ctrl":
                await app.set_props_and_fire("mod-ctrl", {}, "click")
                await _change(app, "  c")
                assert _inputs(socket)[-1] == b"\x03"
                before = list(_inputs(socket))
                await _change(app, "  ")
                assert _inputs(socket) == before
            elif boundary == "arrow":
                await app.set_props_and_fire("key-left", {}, "click")
                assert _inputs(socket)[-1] == b"\x1b[D"
            else:
                session._clipboard = FakeClipboard("external")
                await app.set_props_and_fire("shell-paste" if shell else "agent-paste", {}, "click")
                if session.pending_review:
                    await app.set_props_and_fire(CONFIRM_KEY, {}, "click")
                assert _inputs(socket)[-1] == b"external"
            count = len(_inputs(socket))
            await _change(app, "  corrected")
            assert _inputs(socket)[count:] == [b"corrected"]
        finally:
            await session.dispose()

    run(scenario())


@pytest.mark.parametrize("shell", [False, True])
def test_direct_stale_reset_acknowledgement_cannot_clear_current_edit(shell: bool) -> None:
    async def scenario() -> None:
        app, session, socket, _ = await _direct(shell)
        try:
            await _change(app, "  old")
            await app.set_props_and_fire(_typing_key(app), {}, "blur")
            stale = app.find(_typing_key(app)).on_selection_change
            await _change(app, "  new")
            await stale(SimpleNamespace(selection=SimpleNamespace(base_offset=2, extent_offset=2)))
            await app.set_props_and_fire(
                _typing_key(app),
                {},
                "selection_change",
                {"selected_text": "", "selection": {"base_offset": 2, "extent_offset": 2}},
            )
            await _change(app, "  newer")
            assert _inputs(socket) == [b"old", b"new", b"er"]
            assert app.find(_typing_key(app)).value == "  newer"
        finally:
            await session.dispose()

    run(scenario())


@pytest.mark.parametrize("shell", [False, True])
@pytest.mark.parametrize("finish", ["cancel", "confirm", "unsafe"])
def test_direct_review_boundaries_never_replay_replacement_deletions(
    shell: bool, finish: str
) -> None:
    async def scenario() -> None:
        app, session, socket, _ = await _direct(shell)
        try:
            await _change(app, "  prior")
            await _change(app, "  replaced\nnext" if finish != "unsafe" else "  bad\x1b")
            assert _inputs(socket) == [b"prior"]
            if finish == "unsafe":
                assert session.pending_review is None and session.paste_error
            elif finish == "confirm":
                await app.set_props_and_fire(CONFIRM_KEY, {}, "click")
                assert _inputs(socket) == [b"prior", b"replaced\nnext"]
            else:
                session.cancel_paste()
                await app.settle()
            count = len(_inputs(socket))
            await _change(app, "  new")
            assert _inputs(socket)[count:] == [b"new"]
        finally:
            await session.dispose()

    run(scenario())


@pytest.mark.parametrize("shell", [False, True])
@pytest.mark.parametrize("submit", [False, True])
def test_direct_duplicate_return_and_submit_are_not_replayed(shell: bool, submit: bool) -> None:
    async def scenario() -> None:
        app, session, socket, _ = await _direct(shell)
        try:
            await _change(app, "  hello")
            original = app.find(_typing_key(app))._i
            if submit:
                await app.set_props_and_fire(_typing_key(app), {}, "submit", "  hello")
                await app.session.dispatch_event(original, "submit", "  hello")
            else:
                await _change(app, "  hello\n", ack=False)
                await app.session.dispatch_event(original, "change", "  hello\n")
            await app.settle()
            assert _inputs(socket) == [b"hello", b"\r"]
            await _ack_reset(app)
            await _change(app, "  \n")
            assert _inputs(socket) == [b"hello", b"\r", b"\r"]
        finally:
            await session.dispose()

    run(scenario())


@pytest.mark.parametrize("shell", [False, True])
def test_direct_partial_send_failure_drops_queued_revision_without_replay(shell: bool) -> None:
    async def scenario() -> None:
        app, session, socket, _ = await _direct(shell)
        tasks: list[asyncio.Task[Any]] = []
        released, started = asyncio.Event(), asyncio.Event()
        send = session.sessions.keys._sender
        calls = 0

        async def fail(data: bytes) -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                started.set()
                await released.wait()
                raise OSError("synthetic uncertain write")
            await send(data)

        try:
            await _change(app, "  old")
            session.sessions.keys._sender = fail
            field = app.find(_typing_key(app))
            for value in ("  new", "  newer"):
                app.session.apply_patch(field._i, {"value": value})
                tasks.append(
                    asyncio.create_task(app.session.dispatch_event(field._i, "change", value))
                )
                await asyncio.wait_for(started.wait(), 1)
            released.set()
            await asyncio.gather(*tasks, return_exceptions=True)
            await app.settle()
            assert _inputs(socket) == [b"old", b"\x7f"]
            session.sessions.keys._sender = send
            await _change(app, "  fresh")
            assert _inputs(socket) == [b"old", b"\x7f", b"fresh"]
        finally:
            released.set()
            await asyncio.gather(*tasks, return_exceptions=True)
            await session.dispose()

    run(scenario())
