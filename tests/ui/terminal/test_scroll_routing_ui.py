"""Real Flet dispatch/native packets prove routing and stable terminal ancestry."""

from __future__ import annotations

import asyncio
import contextlib
import struct
from dataclasses import replace
from typing import Any

import flet as ft
import pytest
from flet.messaging.protocol import MessageAction
from relaydeck_terminal import protocol

from relaydeck.client.features.terminal.events import click
from relaydeck.client.features.terminal.scrolling import ALT_SELECTION_HINT, ScrollButtons
from tests.support.host_harness import render_component, run
from tests.support.terminal.binding import _wait_for
from tests.support.terminal.latch_text import _inputs
from tests.support.terminal.scroll_ui import _app, _scroll_packets, _view
from tests.support.terminal.select_sheet import _sheet
from tests.support.terminal.selection import _open_with_snapshot, _snapshot_requests
from tests.support.ui.phone_terminal import _ancestry, _shape


@pytest.mark.parametrize("shell", [False, True])
@pytest.mark.parametrize("tier", ["compact", "medium", "expanded"])
def test_all_widths_native_wheel_routing_local_normal_and_guarded_alt(
    shell: bool, tier: str
) -> None:
    async def scenario() -> None:
        app, session, socket, channel = await _app(shell, tier)
        try:
            term = session.current_term
            shape = _shape(_ancestry(app, term))
            token = session._view_token
            overlay = app.find("terminal-scroll-overlay")
            assert overlay.visible == (tier == "compact")
            assert overlay.right == 8 and overlay.width == 32
            assert app.find("terminal-scroll-up").tooltip is None
            assert app.find("terminal-scroll-up-semantics").label == (
                "Scroll up" if tier == "compact" else "Scroll up, unavailable"
            )
            assert not app.find("terminal-scroll-latest").visible
            # Normal xterm drag/wheel uses its ScrollController, not native INPUT.
            # Even a forged wheel packet cannot become a normal-buffer PTY write.
            await _view(app, channel, alt=False, bottom=False)
            channel.handler(protocol.encode_input(b"\x1b[<64;1;1M", protocol.ORIGIN_MOUSE))
            await app.settle()
            assert _inputs(socket) == []
            if tier == "compact":
                await app.set_props_and_fire("terminal-scroll-up", {}, "click")
                await app.set_props_and_fire("terminal-scroll-down", {}, "click")
                assert _scroll_packets(channel)[-2:] == [b"\x0b\xff", b"\x0b\x01"]
                assert app.find("terminal-scroll-latest").visible
                await app.set_props_and_fire("terminal-scroll-latest", {}, "click")
                assert _scroll_packets(channel)[-1] == b"\x0b\x7f"
                assert _inputs(socket) == []
            await _view(app, channel, alt=True, mouse=True)
            channel.handler(protocol.encode_input(b"\x1b[<64;12;3M", protocol.ORIGIN_MOUSE))
            channel.handler(protocol.encode_input(b"\x1b[<65;12;3M", protocol.ORIGIN_MOUSE))
            channel.handler(protocol.encode_input(b"\x1b[<0;12;3M", protocol.ORIGIN_MOUSE))
            await app.settle()
            assert _inputs(socket) == [b"\x1b[<64;12;3M", b"\x1b[<65;12;3M"]
            await _view(app, channel, alt=True)
            channel.handler(protocol.encode_input(b"\x1b[A", protocol.ORIGIN_KEY))
            channel.handler(protocol.encode_input(b"\x1bOB", protocol.ORIGIN_KEY))
            await app.settle()
            assert _inputs(socket)[-2:] == [b"\x1b[A", b"\x1bOB"]
            if tier == "compact":
                await app.set_props_and_fire("terminal-scroll-up", {}, "click")
                await app.set_props_and_fire("terminal-scroll-down", {}, "click")
                assert _inputs(socket)[-2:] == [b"\x1b[5~", b"\x1b[6~"]
            assert session.current_term is term and session._view_token == token
            assert _shape(_ancestry(app, term)) == shape
            if not shell:
                assert session.store.draft == "retained compose draft"
        finally:
            await session.dispose()

    run(scenario())


@pytest.mark.parametrize("shell", [False, True])
def test_normal_watching_scroll_is_local_alt_watching_and_stale_callbacks_write_nothing(
    shell: bool,
) -> None:
    async def scenario() -> None:
        app, session, socket, channel = await _sheet(shell)
        try:
            await _view(app, channel, alt=False)
            session.controller.state = replace(session.controller.state, lease_mode="view")
            session.controller.mode = "view"
            session._term_state = replace(session.term_state, lease_mode="view")
            session._notify()
            await app.settle()
            assert not session.keys_enabled
            assert not app.find("terminal-scroll-up").disabled
            await app.set_props_and_fire("terminal-scroll-up", {}, "click")
            assert _scroll_packets(channel)[-1] == b"\x0b\xff"
            await _view(app, channel, alt=True, mouse=True)
            assert app.find("terminal-scroll-up").disabled
            channel.handler(protocol.encode_input(b"\x1b[<64;1;1M", protocol.ORIGIN_MOUSE))
            await app.settle()
            assert _inputs(socket) == []
            old = app.find("terminal-scroll-up")
            term = session.current_term
            term.reset(reason="epoch_changed")
            count = len(channel.sent)
            await old._trigger_event("click", None)
            assert len(channel.sent) == count and _inputs(socket) == []
        finally:
            await session.dispose()

    run(scenario())


def test_composer_allows_alt_scrolling_without_native_text_or_draft_changes() -> None:
    async def scenario() -> None:
        app, session, socket, channel = await _sheet(False)
        try:
            await app.set_props_and_fire("agent-compose-toggle", {}, "click")
            await _view(app, channel, alt=True, mouse=True)
            assert session.current_term.input_enabled
            channel.handler(protocol.encode_input(b"hello", protocol.ORIGIN_TEXT))
            channel.handler(protocol.encode_input(b"\r", protocol.ORIGIN_KEY))
            channel.handler(protocol.encode_input(b"\x1b[<64;1;1M", protocol.ORIGIN_MOUSE))
            await app.settle()
            assert _inputs(socket) == [b"\x1b[<64;1;1M"]
            assert session.store.draft == "retained compose draft"
            assert app.find("terminal-direct-tap").on_tap_down is None
        finally:
            await session.dispose()

    run(scenario())


@pytest.mark.parametrize("end", ["release", "park", "reset"])
def test_scroll_hold_repeats_and_stops_without_replay(end: str) -> None:
    async def scenario() -> None:
        app, session, socket, channel = await _sheet(False)
        try:
            await _view(app, channel, alt=True)
            up = app.find("terminal-scroll-up-hold")
            await app.session.dispatch_event(up._i, "long_press_start", None)
            await asyncio.sleep(0.275)
            assert len(_inputs(socket)) >= 3
            if end == "release":
                await app.session.dispatch_event(up._i, "long_press_end", None)
            elif end == "park":
                session.set_foreground(False)
            elif end == "reset":
                session.current_term.reset(reason="epoch_changed")
            await app.settle()
            count = len(_inputs(socket))
            await asyncio.sleep(0.250)
            assert len(_inputs(socket)) == count
            assert all(data == b"\x1b[5~" for data in _inputs(socket))
        finally:
            await session.dispose()

    run(scenario())


def test_scroll_hold_is_cancelled_by_declarative_unmount() -> None:
    async def scenario() -> None:
        calls: list[int | None] = []

        async def scroll(pages: int | None) -> None:
            calls.append(pages)

        @ft.component
        def Host() -> Any:
            mounted, set_mounted = ft.use_state(True)
            return ft.Column(
                controls=[
                    ScrollButtons(
                        visible=True,
                        state=protocol.ViewState(False, True, False),
                        binding="same",
                        on_scroll=scroll,
                        can_scroll=lambda: True,
                    )
                    if mounted
                    else ft.Text("gone"),
                    ft.Button(
                        "Unmount", key="scroll-unmount", on_click=click(lambda: set_mounted(False))
                    ),
                ]
            )

        app = await render_component(Host)
        up = app.find("terminal-scroll-up-hold")
        await app.session.dispatch_event(up._i, "long_press_start", None)
        await asyncio.sleep(0.275)
        assert len(calls) >= 3
        await app.set_props_and_fire("scroll-unmount", {}, "click")
        count = len(calls)
        await asyncio.sleep(0.250)
        assert len(calls) == count
        with pytest.raises(KeyError):
            app.find("terminal-scroll-up")

    run(scenario())


def test_scroll_pointer_hold_and_accessible_activation_do_not_compete_with_inner_button() -> None:
    async def scenario() -> None:
        calls: list[int | None] = []

        async def scroll(pages: int | None) -> None:
            calls.append(pages)

        app = await render_component(
            ScrollButtons,
            visible=True,
            state=protocol.ViewState(False, True, False),
            binding="same",
            on_scroll=scroll,
            can_scroll=lambda: True,
        )
        hold = app.find("terminal-scroll-up-hold")
        assert hold.content.bgcolor == ft.Colors.TRANSPARENT
        assert hold.content.width == hold.content.height == 32
        assert hold.content.content.ignore_interactions
        assert hold.on_tap_cancel is None and hold.on_long_press_cancel is not None
        assert hold.exclude_from_semantics
        label = app.find("terminal-scroll-up-semantics")
        assert label.label == "Scroll up" and label.button and label.exclude_semantics
        # Pointer, explicit semantic action, and the retained Material button
        # callback each invoke the same action once. Native focus is not faked.
        await app.set_props_and_fire("terminal-scroll-up-hold", {}, "tap")
        await app.set_props_and_fire("terminal-scroll-up-semantics", {}, "tap")
        await app.set_props_and_fire("terminal-scroll-up", {}, "click")
        assert calls == [-1, -1, -1]

    run(scenario())


@pytest.mark.parametrize("shell", [False, True])
@pytest.mark.parametrize("alt", [False, True])
def test_phone_select_history_cap_bottom_scroll_and_alt_hint(shell: bool, alt: bool) -> None:
    async def scenario() -> None:
        app, session, _socket, channel = await _sheet(shell)
        try:
            await _view(app, channel, alt=alt)
            text = "\n".join(f"line {i:04d}" for i in range(5100))
            term = session.current_term
            await _open_with_snapshot(app, term, channel.sent, text)
            expected = text if alt else text[text.index("line 0100") :]
            assert app.find("phone-select-field").value == expected
            assert app.find("phone-select-alt-hint").visible == alt
            assert app.find("phone-select-alt-hint").value == ALT_SELECTION_HINT
            scroller = app.find("phone-select-text-scroll")
            assert any(
                msg.action == MessageAction.INVOKE_METHOD
                and msg.body.name == "scroll_to"
                and msg.body.control_id == scroller._i
                and msg.body.args["offset"] == -1
                for msg in app.conn.sent
            )
            await app.set_props_and_fire("phone-select-copy-all", {}, "click")
            assert session._clipboard.writes[-1] == expected
        finally:
            await session.dispose()

    run(scenario())


@pytest.mark.parametrize("shell", [False, True])
def test_scroll_does_not_consume_or_apply_typing_latch(shell: bool) -> None:
    async def scenario() -> None:
        app, session, socket, channel = await _sheet(shell)
        try:
            await _view(app, channel, alt=True, mouse=True)
            await app.set_props_and_fire("mod-ctrl", {}, "click")
            channel.handler(protocol.encode_input(b"\x1b[<65;1;1M", protocol.ORIGIN_MOUSE))
            await app.settle()
            await app.set_props_and_fire("terminal-scroll-up", {}, "click")
            assert session.sessions.keys.pending_modifiers == frozenset({"ctrl"})
            channel.handler(protocol.encode_input(b"c", protocol.ORIGIN_TEXT))
            await app.settle()
            assert _inputs(socket) == [b"\x1b[<65;1;1M"] + [b"\x1b[<64;1;1M"] * 23 + [b"\x03"]
            assert session.sessions.keys.pending_modifiers == frozenset()
        finally:
            await session.dispose()

    run(scenario())


@pytest.mark.parametrize("shell", [False, True])
def test_native_buffer_change_invalidates_pending_selection_and_old_control_scroll(
    shell: bool,
) -> None:
    async def scenario() -> None:
        app, session, socket, channel = await _sheet(shell)
        opener = None
        try:
            await _view(app, channel, alt=True)
            old_up = app.find("terminal-scroll-up")
            opener = asyncio.create_task(
                app.set_props_and_fire("terminal-select-text", {}, "click")
            )
            await _wait_for(lambda: len(_snapshot_requests(channel.sent)) == 1)
            await _view(app, channel, alt=False)
            (request,) = struct.unpack_from(">I", _snapshot_requests(channel.sent)[0], 1)
            channel.handler(protocol.encode_snapshot_chunk(request, 3, 0, b"old"))
            await opener
            assert not session.term_state.selection.open
            count = len(channel.sent)
            # The old control may already be unmounted; either way its click sends nothing.
            with contextlib.suppress(RuntimeError):
                await old_up._trigger_event("click", None)
            assert len(channel.sent) == count and _inputs(socket) == []
        finally:
            if opener is not None and not opener.done():
                opener.cancel()
            await session.dispose()

    run(scenario())


def test_wheel_block_drops_stale_reports_until_ack() -> None:
    async def scenario() -> None:
        app, session, socket, channel = await _app(False, "compact")
        try:
            term = session.current_term
            await _view(app, channel, alt=True, mouse=True)
            channel.handler(protocol.encode_input(b"\x1b[<64;12;3M", protocol.ORIGIN_MOUSE))
            await app.settle()
            assert _inputs(socket) == [b"\x1b[<64;12;3M"]
            # A server-side key lands: later wheel reports left the
            # widget before it saw the cancel, so they are stale.
            channel.handler(protocol.encode_scroll_cancelled(0))
            await app.settle()
            term.cancel_scroll()
            channel.handler(protocol.encode_input(b"\x1b[<65;12;3M", protocol.ORIGIN_MOUSE))
            await app.settle()
            assert _inputs(socket) == [b"\x1b[<64;12;3M"]
            # Explicit arrow keys are fresh input, never stale scroll.
            channel.handler(protocol.encode_input(b"\x1b[A", protocol.ORIGIN_KEY))
            await app.settle()
            assert _inputs(socket)[-1] == b"\x1b[A"
            # The widget ack releases the block; wheels flow again.
            channel.handler(protocol.encode_scroll_cancelled(1))
            await app.settle()
            channel.handler(protocol.encode_input(b"\x1b[<65;12;3M", protocol.ORIGIN_MOUSE))
            await app.settle()
            assert _inputs(socket)[-1] == b"\x1b[<65;12;3M"
        finally:
            await session.dispose()

    run(scenario())


@pytest.mark.parametrize("combined", [True, False])
def test_stale_wheel_received_before_ack_drops(combined: bool) -> None:
    async def scenario() -> None:
        app, session, socket, channel = await _app(False, "compact")
        try:
            await _view(app, channel, alt=True, mouse=True)
            channel.handler(protocol.encode_scroll_cancelled(0))  # viewer acks
            await app.settle()
            assert await session.press_key("enter")
            before = list(_inputs(socket))
            wheel = protocol.encode_input(b"\x1b[<64;12;3M", protocol.ORIGIN_MOUSE)
            ack = protocol.encode_scroll_cancelled(1)
            if combined:
                channel.handler(wheel + ack)
            else:
                channel.handler(wheel)
                channel.handler(ack)
            await app.settle()
            assert _inputs(socket) == before
            # A fresh wheel after the ack flows again.
            channel.handler(protocol.encode_input(b"\x1b[<64;12;3M", protocol.ORIGIN_MOUSE))
            await app.settle()
            assert _inputs(socket)[-1] == b"\x1b[<64;12;3M"
        finally:
            await session.dispose()

    run(scenario())


def test_wheel_accepted_before_key_drops_after_ack() -> None:
    async def scenario() -> None:
        app, session, socket, channel = await _app(False, "compact")
        try:
            await _view(app, channel, alt=True, mouse=True)
            channel.handler(protocol.encode_scroll_cancelled(0))  # viewer acks
            await app.settle()
            # Accepted now; its callback is queued but has not run.
            channel.handler(protocol.encode_input(b"\x1b[<64;12;3M", protocol.ORIGIN_MOUSE))
            # The key lands (cancel issued) and the viewer acks before
            # the queued wheel callback reaches the writer.
            assert await session.press_key("enter")
            channel.handler(protocol.encode_scroll_cancelled(1))
            await app.settle()
            assert b"\x1b[<64;12;3M" not in _inputs(socket)
            # A fresh wheel after the cancel boundary flows again.
            channel.handler(protocol.encode_input(b"\x1b[<64;12;3M", protocol.ORIGIN_MOUSE))
            await app.settle()
            assert _inputs(socket)[-1] == b"\x1b[<64;12;3M"
        finally:
            await session.dispose()

    run(scenario())
