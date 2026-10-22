"""Real Flet field dispatch into the shared generation-bound PTY encoder."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from flet.messaging.protocol import MessageAction

from relaydeck.client.features.terminal.surfaces import direct_typing
from tests.support.host_harness import run
from tests.support.terminal.direct_typing import _ack_reset, _change, _direct, _typing_key
from tests.support.terminal.latch_text import _inputs


async def _stale_event(field: Any, name: str, data: str) -> None:
    """Fire an event on a field object captured before a rerender. A field the rerender orphaned
    (its screen left the tree) refuses the event outright; a still-parented one must ignore it."""
    try:
        await field._trigger_event(name, data)
    except RuntimeError as exc:
        assert "added to the page" in str(exc)


@pytest.mark.parametrize("shell", [False, True])
def test_direct_field_exact_text_backspace_enter_latch_and_sentinel(shell: bool) -> None:
    async def scenario() -> None:
        app, session, socket, focuses = await _direct(shell)
        try:
            field = app.find(_typing_key(app))
            assert field.visible and field.height == 36 and not field.disabled
            assert field.multiline and field.fit_parent_size and not field.shift_enter
            assert field.min_lines is None and field.max_lines is None
            assert field.hint_text == "Type to terminal" and field.value == "  "
            assert not field.autocorrect and not field.enable_suggestions
            assert field.capitalization.value == "none"
            for char in "abc":
                await _change(app, "  " + char)
            assert any(
                # Flet patches use subtree-local indices, not global IDs.
                len(op) == 4 and op[2:] == ["value", "  "]
                for message in app.conn.sent
                if message.action == MessageAction.PATCH_CONTROL
                for op in message.body.patch
            )
            await _change(app, " ")
            await _change(app, "")
            before = len(focuses)
            await app.set_props_and_fire(_typing_key(app), {}, "submit", "  ")
            assert _inputs(socket) == [b"a", b"b", b"c", b"\x7f", b"\x7f", b"\x7f", b"\r"]
            assert len(focuses) > before
            # iOS multiline Return commits a newline through native change;
            # the shared ORIGIN_TEXT encoder normalizes it without refocus.
            before = len(focuses)
            await _change(app, "  \n")
            assert _inputs(socket)[-1] == b"\r" and len(focuses) == before
            await app.set_props_and_fire("mod-ctrl", {}, "click")
            await _change(app, "  c")
            assert _inputs(socket)[-1] == b"\x03"
            assert app.find("mod-ctrl").data["latch"] == "off"
            await app.set_props_and_fire("mod-ctrl", {}, "click")
            await _change(app, "  hello")
            assert _inputs(socket)[-1] == b"hello"
            assert app.find("mod-ctrl").data["latch"] == "once"
            await _change(app, "  c")
            assert _inputs(socket)[-1] == b"\x03"
            await _change(app, "  你好😀")
            assert _inputs(socket)[-1] == "你好😀".encode()
            field = app.find(_typing_key(app))
            assert (
                field.value == "  "
                and field.selection.base_offset == field.selection.extent_offset == 2
            )
            if not shell:
                assert session.store.draft == "retained compose draft"
        finally:
            await session.dispose()

    run(scenario())


@pytest.mark.parametrize("shell", [False, True])
@pytest.mark.parametrize("blocked", ["park", "lease", "selection", "generation"])
def test_direct_changes_and_focus_fail_closed_before_the_subscription_rerender(
    shell: bool, blocked: str
) -> None:
    async def scenario() -> None:
        app, session, socket, focuses = await _direct(shell)
        try:
            field = app.find(_typing_key(app))
            if blocked == "park":
                session.set_foreground(False)
            elif blocked == "lease":
                session.controller.mode = "view"
            elif blocked == "selection":
                session.open_selection()
            else:
                await session.reconnect()
                if not shell:
                    await session.confirm_takeover()
            state, count = session.term_state, len(focuses)
            await _stale_event(field, "change", "  forbidden")
            await _stale_event(field, "submit", "  forbidden")
            assert session.term_state is state and not _inputs(socket)
            assert len(focuses) == count
            if not shell:
                assert session.store.draft == "retained compose draft"
        finally:
            await session.dispose()

    run(scenario())


@pytest.mark.parametrize("shell", [False, True])
def test_medium_field_and_real_viewport_tap_never_requests_focus(
    shell: bool,
) -> None:
    async def scenario() -> None:
        app, session, socket, focuses = await _direct(shell, "medium")
        try:
            term, binding, count = session.current_term, session._view_token, len(focuses)
            field = app.find(_typing_key(app))
            assert field.height == 36 and field.visible and field.can_request_focus
            await app.set_props_and_fire("terminal-direct-tap", {}, "tap", {"l": {"x": 5, "y": 5}})
            # Agent taps never open the keyboard; only the keyboard button
            # does. The shell terminal keeps its tap-to-type (outside this change).
            assert len(focuses) == count + (1 if shell else 0)
            assert session.current_term is term and session._view_token == binding
            assert _inputs(socket) == []
        finally:
            await session.dispose()

    run(scenario())


@pytest.mark.parametrize("shell", [False, True])
def test_rapid_cumulative_changes_do_not_repeat_prefix_or_replay_after_reset(shell: bool) -> None:
    async def scenario() -> None:
        app, session, socket, _focuses = await _direct(shell)
        release, started = asyncio.Event(), asyncio.Event()
        tasks: list[asyncio.Task[Any]] = []
        try:
            send = session.sessions.keys._sender

            async def delayed(data: bytes) -> None:
                started.set()
                await release.wait()
                await send(data)

            session.sessions.keys._sender = delayed
            field = app.find(_typing_key(app))
            for value in ("  a", "  ab", "  abc", "  abc"):
                app.session.apply_patch(field._i, {"value": value})
                tasks.append(
                    asyncio.create_task(app.session.dispatch_event(field._i, "change", value))
                )
                await asyncio.sleep(0)
            await asyncio.wait_for(started.wait(), 1)
            release.set()
            await asyncio.gather(*tasks)
            await app.settle()
            assert _inputs(socket) == [b"a", b"b", b"c"]
            await _ack_reset(app)
            await _change(app, "  c")
            assert _inputs(socket) == [b"a", b"b", b"c", b"c"]
        finally:
            release.set()
            await asyncio.gather(*tasks, return_exceptions=True)
            await session.dispose()

    run(scenario())


@pytest.mark.parametrize("shell", [False, True])
def test_old_field_changes_and_submit_cannot_touch_replacement_writer_or_draft(shell: bool) -> None:
    async def scenario() -> None:
        app, session, socket, focuses = await _direct(shell)
        try:
            field = app.find(_typing_key(app))
            await session.reconnect()
            if not shell:
                await session.confirm_takeover()
            await app.settle()
            current, state = session.controller, session.term_state
            count = len(focuses)
            await _stale_event(field, "change", "  stale")
            await _stale_event(field, "submit", "  stale")
            assert session.controller is current and session.term_state is state
            assert _inputs(socket) == [] and len(focuses) == count
            if not shell:
                assert session.store.draft == "retained compose draft"
        finally:
            await session.dispose()

    run(scenario())


@pytest.mark.parametrize("shell", [False, True])
def test_keyboard_single_clicks_toggle_and_rapid_duplicate_click_toggles_once(
    shell: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = [1.0]
    monkeypatch.setattr(direct_typing, "_typing_now", lambda: now[0])

    async def scenario() -> None:
        app, session, _socket, focuses = await _direct(shell)
        try:
            key = "shell-keyboard-toggle" if shell else "agent-workflow-toggle"
            term, controller = session.current_term, session.controller
            field_id = app.find(_typing_key(app))._i
            assert app.find(_typing_key(app)).visible and focuses
            now[0] += 0.5
            await app.set_props_and_fire(key, {}, "click")
            closed = app.find(_typing_key(app))
            if shell:
                assert not closed.visible and not closed.can_request_focus and closed.disabled
            else:
                # Agents keep the Direct field on screen as the tap that reopens the keyboard.
                assert closed.visible and closed.can_request_focus
            count = len(focuses)
            now[0] += 0.5
            await app.set_props_and_fire(key, {}, "click")
            assert app.find(_typing_key(app)).visible and len(focuses) == count + 1
            now[0] += 0.5
            await app.set_props_and_fire(key, {}, "click")
            now[0] += 0.02
            await app.set_props_and_fire(key, {}, "click")
            assert app.find(_typing_key(app)).visible is not shell
            assert app.find(_typing_key(app))._i == field_id
            assert session.current_term is term and session.controller is controller
        finally:
            await session.dispose()

    run(scenario())


@pytest.mark.parametrize("shell", [False, True])
@pytest.mark.parametrize(
    ("value", "expected"),
    [("x", [b"\x7f", b"\x7f", b"x"]), (" ab", [b"\x7f", b"ab"]), ("xy", [b"\x7f", b"\x7f", b"xy"])],
)
def test_reset_baseline_sentinel_replacement_keeps_every_inserted_character(
    shell: bool, value: str, expected: list[bytes]
) -> None:
    async def scenario() -> None:
        app, session, socket, _focuses = await _direct(shell)
        try:
            await _change(app, value)
            assert _inputs(socket) == expected
        finally:
            await session.dispose()

    run(scenario())


@pytest.mark.parametrize("shell", [False, True])
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("x", [b"a", b"\x7f", b"\x7f", b"\x7f", b"x"]),
        (" ab", [b"a", b"\x7f", b"\x7f", b"ab"]),
        ("xy", [b"a", b"\x7f", b"\x7f", b"\x7f", b"xy"]),
    ],
)
def test_pending_reset_replacement_diffs_from_the_last_dispatched_edit(
    shell: bool, value: str, expected: list[bytes]
) -> None:
    async def scenario() -> None:
        app, session, socket, _focuses = await _direct(shell)
        try:
            await _change(app, "  a", ack=False)
            await _change(app, value, ack=False)
            assert _inputs(socket) == expected
        finally:
            await session.dispose()

    run(scenario())


@pytest.mark.parametrize("shell", [False, True])
def test_focus_uses_the_attached_normal_node_and_bulk_return_requires_review(shell: bool) -> None:
    async def scenario() -> None:
        app, session, socket, focuses = await _direct(shell)
        try:
            field = app.find(_typing_key(app))
            # Pinned TextField.focus() requests the normal node. The
            # alternate shift-enter node must never be attached here.
            assert field.shift_enter is False
            assert field.multiline and field.fit_parent_size and field.height == 36
            assert focuses[-1] == field._i
            await _change(app, "  a\n")
            # Scalar Flet cannot distinguish bulk Paste from coalesced typing.
            # Paste handling takes precedence: bulk newline text needs review.
            assert _inputs(socket) == []
            assert session.pending_review.preview == "a\n"
            assert await session.confirm_paste()
            assert _inputs(socket) == [b"a\n"]
        finally:
            await session.dispose()

    run(scenario())


@pytest.mark.parametrize("shell", [False, True])
@pytest.mark.parametrize("next_value", ["xy", "z"])
@pytest.mark.parametrize("ctrl", ["off", "once", "locked"])
def test_delayed_replacement_keeps_the_next_delta_ordered_and_modifiers_per_packet(
    shell: bool, next_value: str, ctrl: str
) -> None:
    async def scenario() -> None:
        app, session, socket, _focuses = await _direct(shell)
        release, started = asyncio.Event(), asyncio.Event()
        tasks: list[asyncio.Task[Any]] = []
        try:
            if ctrl != "off":
                await app.set_props_and_fire("mod-ctrl", {}, "click")
                if ctrl == "locked":
                    await app.set_props_and_fire("mod-ctrl", {}, "click")
            send = session.sessions.keys._sender

            async def delayed(data: bytes) -> None:
                started.set()
                await release.wait()
                await send(data)

            session.sessions.keys._sender = delayed
            field = app.find(_typing_key(app))
            for value in ("x", next_value):
                app.session.apply_patch(field._i, {"value": value})
                tasks.append(
                    asyncio.create_task(app.session.dispatch_event(field._i, "change", value))
                )
                await asyncio.sleep(0)
                await asyncio.wait_for(started.wait(), 1)
            release.set()
            await asyncio.wait_for(asyncio.gather(*tasks), 2)
            await app.settle()
            # Ctrl cannot encode DEL, so the first inserted character consumes
            # the one-shot. A locked Ctrl is rearmed for the later insertion.
            expected = [b"\x7f", b"\x7f", b"x" if ctrl == "off" else b"\x18"]
            if next_value == "z":
                expected.append(b"\x7f")
            expected.append(
                (b"\x19" if next_value == "xy" else b"\x1a")
                if ctrl == "locked"
                else (b"y" if next_value == "xy" else b"z")
            )
            assert _inputs(socket) == expected
            assert app.find("mod-ctrl").data["latch"] == ("locked" if ctrl == "locked" else "off")
            await _ack_reset(app)
            await _change(app, "  c")
            assert _inputs(socket) == expected + [b"\x03" if ctrl == "locked" else b"c"]
            if not shell:
                assert session.store.draft == "retained compose draft"
        finally:
            release.set()
            await asyncio.gather(*tasks, return_exceptions=True)
            await session.dispose()

    run(scenario())


@pytest.mark.parametrize("shell", [False, True])
def test_delayed_replacement_holds_the_shared_writer_against_a_chip_press(shell: bool) -> None:
    async def scenario() -> None:
        app, session, socket, _focuses = await _direct(shell)
        release, started = asyncio.Event(), asyncio.Event()
        tasks: list[asyncio.Task[Any]] = []
        try:
            send = session.sessions.keys._sender

            async def delayed(data: bytes) -> None:
                started.set()
                await release.wait()
                await send(data)

            session.sessions.keys._sender = delayed
            field = app.find(_typing_key(app))
            app.session.apply_patch(field._i, {"value": "x"})
            tasks.append(asyncio.create_task(app.session.dispatch_event(field._i, "change", "x")))
            await asyncio.wait_for(started.wait(), 1)
            tasks.append(
                asyncio.create_task(
                    app.session.dispatch_event(app.find("key-slash")._i, "click", "")
                )
            )
            await asyncio.sleep(0)
            release.set()
            await asyncio.wait_for(asyncio.gather(*tasks), 2)
            await app.settle()
            assert _inputs(socket) == [b"\x7f", b"\x7f", b"x", b"/"]
        finally:
            release.set()
            await asyncio.gather(*tasks, return_exceptions=True)
            await session.dispose()

    run(scenario())


@pytest.mark.parametrize("shell", [False, True])
def test_delayed_replacement_drops_its_remainder_and_queued_delta_on_generation_loss(
    shell: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = [1.0]
    monkeypatch.setattr(direct_typing, "_typing_now", lambda: now[0])

    async def scenario() -> None:
        app, session, socket, _focuses = await _direct(shell)
        release, started = asyncio.Event(), asyncio.Event()
        tasks: list[asyncio.Task[Any]] = []
        try:
            send = session.sessions.keys._sender
            term, old_token = session.current_term, session.input_token

            async def delayed(data: bytes) -> None:
                started.set()
                await release.wait()
                await send(data)

            session.sessions.keys._sender = delayed
            field = app.find(_typing_key(app))
            for value in ("x", "xy"):
                app.session.apply_patch(field._i, {"value": value})
                tasks.append(
                    asyncio.create_task(app.session.dispatch_event(field._i, "change", value))
                )
                await asyncio.sleep(0)
                await asyncio.wait_for(started.wait(), 1)
            session.set_foreground(False)
            session.set_foreground(True)
            assert session.input_token != old_token
            release.set()
            await asyncio.wait_for(asyncio.gather(*tasks), 2)
            await app.settle()
            # The first accepted write may finish; neither its remaining DEL+x
            # nor the queued y may be replayed into the new writer generation.
            assert _inputs(socket) == [b"\x7f"]
            # A coalesced park/resume can retain keyboard intent; explicitly
            # close it before testing a fresh open, rather than assuming close.
            if app.find(_typing_key(app)).visible:
                now[0] += 0.5
                await app.set_props_and_fire(
                    "shell-keyboard-toggle" if shell else "agent-workflow-toggle", {}, "click"
                )
            now[0] += 0.5
            await app.set_props_and_fire(
                "shell-keyboard-toggle" if shell else "agent-workflow-toggle", {}, "click"
            )
            await _change(app, "  z")
            assert _inputs(socket) == [b"\x7f", b"z"]
            assert session.current_term is term
            if not shell:
                assert session.store.draft == "retained compose draft"
        finally:
            release.set()
            await asyncio.gather(*tasks, return_exceptions=True)
            await session.dispose()

    run(scenario())
