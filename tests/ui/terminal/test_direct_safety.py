"""Native reset identities, serialized modifiers and parked authority fences."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import flet as ft
import pytest

from relaydeck.client.features.terminal.surfaces.direct_typing import DirectTypingField
from relaydeck.client.features.terminal.warm_watch import watch_only
from tests.support.host_harness import render_component, run
from tests.support.terminal.direct_typing import (
    _change,
    _direct,
    _typing_key,
    open_direct_input,
)
from tests.support.terminal.latch_text import _inputs


@pytest.mark.parametrize("shell", [False, True])
@pytest.mark.parametrize("boundary", ["blur", "tools", "failure"])
def test_direct_retired_native_editor_cannot_replay_after_reset(shell: bool, boundary: str) -> None:
    async def scenario() -> None:
        app, session, socket, _ = await _direct(shell)
        try:
            await _change(app, "  abc")
            retired = app.find(_typing_key(app))._i
            if boundary == "blur":
                await app.set_props_and_fire(_typing_key(app), {}, "blur")
            elif boundary == "tools":
                await app.set_props_and_fire("key-left", {}, "click")
            else:
                sender = session.sessions.keys._sender

                async def fail(_data: bytes) -> None:
                    raise OSError("synthetic uncertain input")

                session.sessions.keys._sender = fail
                await _change(app, "  abcd", ack=False)
                session.sessions.keys._sender = sender
            expected = list(_inputs(socket))
            current = app.find(_typing_key(app))
            assert current._i != retired
            await app.session.dispatch_event(retired, "change", "  abcd")
            await app.session.dispatch_event(retired, "blur", "")
            await app.settle()
            assert _inputs(socket) == expected
            assert app.find(_typing_key(app)).value == "  "
            await _change(app, "  fresh")
            assert _inputs(socket) == expected + [b"fresh"]
        finally:
            await session.dispose()

    run(scenario())


@pytest.mark.parametrize("shell", [False, True])
def test_direct_old_selection_cannot_ack_second_reset_or_replay_return(shell: bool) -> None:
    async def scenario() -> None:
        app, session, socket, _ = await _direct(shell)
        try:
            await _change(app, "  abc")
            first = app.find(_typing_key(app))._i
            await app.set_props_and_fire(_typing_key(app), {}, "blur")
            second = app.find(_typing_key(app))._i
            await _change(app, "  next")
            await app.set_props_and_fire(_typing_key(app), {}, "submit")
            current = app.find(_typing_key(app))
            assert len({first, second, current._i}) == 3
            pending = current.data["direct_reset"]
            assert pending
            await app.session.dispatch_event(
                first,
                "selection_change",
                {"selected_text": "", "selection": {"base_offset": 2, "extent_offset": 2}},
            )
            await app.session.dispatch_event(second, "submit", "")
            await app.settle()
            assert app.find(_typing_key(app)).data["direct_reset"] == pending
            assert _inputs(socket) == [b"abc", b"next", b"\r"]
        finally:
            await session.dispose()

    run(scenario())


@pytest.mark.parametrize("shell", [False, True])
def test_direct_queued_modifier_invalidates_baseline_inside_writer(shell: bool) -> None:
    async def scenario() -> None:
        app, session, socket, _ = await _direct(shell)
        tasks: list[asyncio.Task[Any]] = []
        started, release = asyncio.Event(), asyncio.Event()
        sender = session.sessions.keys._sender

        async def delayed(data: bytes) -> None:
            if not started.is_set():
                started.set()
                await release.wait()
            await sender(data)

        try:
            session.sessions.keys._sender = delayed
            field = app.find(_typing_key(app))
            app.session.apply_patch(field._i, {"value": "  a"})
            tasks.append(asyncio.create_task(app.session.dispatch_event(field._i, "change", "  a")))
            await asyncio.wait_for(started.wait(), 1)
            tasks.append(
                asyncio.create_task(
                    app.session.dispatch_event(app.find("mod-ctrl")._i, "click", "")
                )
            )
            await asyncio.sleep(0)
            app.session.apply_patch(field._i, {"value": "  ac"})
            tasks.append(
                asyncio.create_task(app.session.dispatch_event(field._i, "change", "  ac"))
            )
            await asyncio.sleep(0)
            release.set()
            await asyncio.gather(*tasks)
            await app.settle()
            assert _inputs(socket) == [b"a", b"\x03"]
            assert app.find(_typing_key(app)).value == "  "
            await _change(app, "  a")
            assert _inputs(socket) == [b"a", b"\x03", b"a"]
        finally:
            release.set()
            await asyncio.gather(*tasks, return_exceptions=True)
            await session.dispose()

    run(scenario())


@pytest.mark.parametrize("shell", [False, True])
@pytest.mark.parametrize("boundary", ["generation", "controller", "navigation_to_view"])
@pytest.mark.parametrize("replacement", [False, True])
def test_direct_hidden_authority_change_rebases_restored_display(
    shell: bool, boundary: str, replacement: bool
) -> None:
    async def scenario() -> None:
        app, session, socket, _ = await _direct(shell)
        try:
            await _change(app, "  abc")
            retired = app.find(_typing_key(app))._i
            app.session.apply_patch(retired, {"value": "  abc pending"})
            session.set_foreground(False)
            await app.settle()
            restored = "  abc pending"
            if boundary == "generation":
                session._bump_generation("test")
                session._notify()
            elif boundary == "controller":
                await session.reconnect()
            else:
                assert await watch_only(session, lambda: not session.foreground)
                await app.settle()
                restored = "  abc updated while hidden"
                app.session.apply_patch(app.find(_typing_key(app))._i, {"value": restored})
            await app.settle()
            session.set_foreground(True)
            if not session.keys_enabled:
                if shell:
                    await session.take_over()
                else:
                    session.request_takeover()
                    await session.confirm_takeover()
            await app.settle()
            if not app.find(_typing_key(app)).visible:
                if shell:
                    await app.set_props_and_fire("shell-keyboard-toggle", {}, "click")
                else:
                    await open_direct_input(app)
            await app.settle()
            assert session.keys_enabled and not app.find(_typing_key(app)).disabled
            assert app.find(_typing_key(app)).value == restored
            assert app.find(_typing_key(app))._i != retired
            observed: list[bytes] = []
            sender = session.sessions.keys._sender

            async def capture(data: bytes) -> None:
                observed.append(data)
                await sender(data)

            session.sessions.keys._sender = capture
            await app.session.dispatch_event(retired, "change", "  abc pendingOLD")
            await app.settle()
            assert observed == []
            if replacement:
                await _change(app, "  xyz")
                assert observed == [b"xyz"]
            else:
                await _change(app, restored + "!")
                assert observed == [b"!"]
                await _change(app, "  xyz")
                # Only the newly sent ! may be erased, never the old displayed text.
                assert observed == [b"!", b"\x7f", b"xyz"]
        finally:
            await session.dispose()

    run(scenario())


@pytest.mark.parametrize("shell", [False, True])
@pytest.mark.parametrize("boundary", ["blur", "hide"])
def test_direct_native_reversion_clears_previous_parked_text(shell: bool, boundary: str) -> None:
    async def scenario() -> None:
        app, session, socket, _ = await _direct(shell)
        try:
            await _change(app, "  abc")
            app.session.apply_patch(app.find(_typing_key(app))._i, {"value": "  abcX"})
            await app.set_props_and_fire(_typing_key(app), {}, "blur")
            assert app.find(_typing_key(app)).value == "  abcX"
            app.session.apply_patch(app.find(_typing_key(app))._i, {"value": "  abc"})
            if boundary == "blur":
                await app.set_props_and_fire(_typing_key(app), {}, "blur")
            else:
                session.set_foreground(False)
                await app.settle()
                session.set_foreground(True)
                await app.settle()
                if shell:
                    await app.set_props_and_fire("shell-keyboard-toggle", {}, "click")
                else:
                    await open_direct_input(app)
            assert "X" not in app.find(_typing_key(app)).value
            await _change(app, "  !")
            assert _inputs(socket) == [b"abc", b"!"]
        finally:
            await session.dispose()

    run(scenario())


@pytest.mark.parametrize("shell", [False, True])
def test_direct_unmounted_authority_change_rebases_without_replay(shell: bool) -> None:
    async def scenario() -> None:
        _app, session, _socket, _ = await _direct(shell)
        field = ft.Ref[ft.TextField]()
        actions: dict[str, Any] = {}
        sent: list[bytes] = []

        async def send(_origin: int, payload: bytes) -> str:
            sent.append(payload)
            return "pty"

        @ft.component
        def Fixture() -> Any:
            mounted, actions["mount"] = ft.use_state(True)
            return ft.Column(
                controls=[
                    DirectTypingField(
                        key="direct-component",
                        on_input=send,
                        visible=True,
                        enabled=True,
                        binding=(session, session.input_token, session.interaction_generation),
                        field_ref=field,
                        focus_requests=0,
                        focus_handled=SimpleNamespace(current=0),
                        dark=True,
                        field_key="direct-unmounted",
                    )
                ]
                if mounted
                else []
            )

        try:
            app = await render_component(Fixture)
            await app.set_props_and_fire("direct-unmounted", {"value": "  abc"}, "change", "  abc")
            retired = app.find("direct-unmounted")._i
            app.session.apply_patch(retired, {"value": "  abc parked"})
            actions["mount"](False)
            await app.settle()
            session._bump_generation("test")
            actions["mount"](True)
            await app.settle()
            current = app.find("direct-unmounted")
            assert current.value == "  abc parked" and current._i != retired
            assert sent == [b"abc"]
            await app.session.dispatch_event(retired, "change", "  abc parkedOLD")
            await app.set_props_and_fire(
                "direct-unmounted", {"value": "  fresh"}, "change", "  fresh"
            )
            assert sent == [b"abc", b"fresh"]
        finally:
            await session.dispose()

    run(scenario())


@pytest.mark.parametrize("shell", [False, True])
def test_direct_rebased_native_reversion_cannot_resurrect_parked_text(shell: bool) -> None:
    async def scenario() -> None:
        app, session, socket, _ = await _direct(shell)
        try:
            await _change(app, "  abc")
            app.session.apply_patch(app.find(_typing_key(app))._i, {"value": "  abc parked"})
            await app.set_props_and_fire(_typing_key(app), {}, "blur")
            session._bump_generation("test")
            session._notify()
            await app.settle()
            assert app.find(_typing_key(app)).value == "  abc parked"
            for value in ("  abc parkedX", "  abc parked"):
                app.session.apply_patch(app.find(_typing_key(app))._i, {"value": value})
                await app.set_props_and_fire(_typing_key(app), {}, "blur")
                assert app.find(_typing_key(app)).value == value
            await _change(app, "  abc parked!")
            assert _inputs(socket) == [b"abc", b"!"]
        finally:
            await session.dispose()

    run(scenario())


@pytest.mark.parametrize("shell", [False, True])
def test_direct_rebased_mid_text_edit_never_sends_or_deletes_parked_suffix(shell: bool) -> None:
    async def scenario() -> None:
        app, session, socket, _ = await _direct(shell)
        try:
            await _change(app, "  abc")
            app.session.apply_patch(app.find(_typing_key(app))._i, {"value": "  abc parked"})
            await app.set_props_and_fire(_typing_key(app), {}, "blur")
            session._bump_generation("test")
            session._notify()
            await app.settle()
            identity = app.find(_typing_key(app))._i
            await _change(app, "  abc Xparked")
            assert _inputs(socket) == [b"abc", b"X"]
            await _change(app, "  abc Xparked!")
            assert _inputs(socket) == [b"abc", b"X", b"!"]
            await _change(app, "  q")
            assert _inputs(socket) == [b"abc", b"X", b"!", b"\x7f", b"\x7f", b"q"]
            assert app.find(_typing_key(app))._i == identity
        finally:
            await session.dispose()

    run(scenario())
