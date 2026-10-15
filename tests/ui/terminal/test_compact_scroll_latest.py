"""Actual compact agent/shell controls with native mode fixtures."""

from dataclasses import replace

import pytest
from relaydeck_terminal import protocol

from relaydeck.client.features.terminal.scrolling import MAX_ALT_PAGES, MAX_ALT_WHEEL_STEPS
from tests.support.host_harness import run
from tests.support.terminal.latch_text import _inputs
from tests.support.terminal.scroll_ui import _app, _scroll_packets, _view
from tests.support.ui.phone_terminal import _ancestry, _shape


@pytest.mark.parametrize("shell", [False, True])
@pytest.mark.parametrize(
    "wheel_up,wheel_down",
    [
        (b"\x1b[<64;12;3M", b"\x1b[<65;12;3M"),
        (b"\x1b[M`!!", b"\x1b[Ma!!"),
        (b"\x1b[96;12;3M", b"\x1b[97;12;3M"),
    ],
)
def test_alt_mouse_jump_uses_observed_native_encoder_not_prompt_arrows(shell, wheel_up, wheel_down):
    async def scenario():
        app, session, socket, channel = await _app(shell, "compact")
        term, ancestry = session.current_term, _shape(_ancestry(app, session.current_term))
        try:
            await _view(app, channel, alt=True, mouse=True)
            # The native report has no encoding bit. Buttons remain hidden
            # until a real accepted wheel teaches the encoding, not guessed SGR.
            assert not app.find("terminal-scroll-latest").visible
            channel.handler(protocol.encode_input(wheel_up, protocol.ORIGIN_MOUSE))
            await app.settle()
            assert app.find("terminal-scroll-latest").visible
            before = len(_inputs(socket))
            await app.set_props_and_fire("terminal-scroll-latest", {}, "click")
            assert _inputs(socket)[before:] == [wheel_down]
            assert app.find("terminal-scroll-latest").visible  # stays offered in a full-screen app
            assert _scroll_packets(channel) == []
            assert session.current_term is term and _shape(_ancestry(app, term)) == ancestry
            if not shell:
                assert session.store.draft == "retained compose draft"
        finally:
            await session.dispose()

    run(scenario())


@pytest.mark.parametrize("shell", [False, True])
@pytest.mark.parametrize("mouse", [False, True])
def test_alt_unknown_position_is_bounded_and_watching_or_selection_has_no_dead_button(shell, mouse):
    async def scenario():
        app, session, socket, channel = await _app(shell, "compact")
        try:
            await _view(app, channel, alt=True, bottom=False, mouse=mouse)
            if mouse:
                # Down at an unknown native-reported position learns mode
                # while leaving the device's navigation estimate at zero.
                channel.handler(protocol.encode_input(b"\x1b[<65;1;1M", protocol.ORIGIN_MOUSE))
                await app.settle()
            latest = app.find("terminal-scroll-latest")
            assert latest.visible and not latest.disabled
            before = len(_inputs(socket))
            await app.set_props_and_fire("terminal-scroll-latest", {}, "click")
            expected = (
                [b"\x1b[<65;1;1M"] * MAX_ALT_WHEEL_STEPS if mouse else [b"\x1b[6~"] * MAX_ALT_PAGES
            )
            assert _inputs(socket)[before:] == expected
            assert not any(
                data in (b"\x1b[A", b"\x1b[B", b"\x1bOA", b"\x1bOB") for data in expected
            )
            old = app.find("terminal-scroll-latest").on_click
            before = len(_inputs(socket))
            closed_selection = session.term_state.selection
            session._term_state = replace(
                session.term_state, selection=replace(closed_selection, open=True)
            )
            session._notify()
            await app.settle()
            assert not app.find("terminal-scroll-latest").visible
            await old(None)
            assert len(_inputs(socket)) == before
            session._term_state = replace(session.term_state, selection=closed_selection)
            session.controller.mode = "view"
            session.controller.state = replace(session.controller.state, lease_mode="view")
            session._term_state = replace(session.term_state, lease_mode="view")
            session._notify()
            await app.settle()
            assert not app.find("terminal-scroll-latest").visible
            before = len(_inputs(socket))
            await old(None)
            assert len(_inputs(socket)) == before
            # Normal-buffer watch scrolling still routes solely to the view.
            await _view(app, channel, alt=False, bottom=False, mouse=mouse)
            assert app.find("terminal-scroll-latest").visible
            await app.set_props_and_fire("terminal-scroll-latest", {}, "click")
            assert _scroll_packets(channel)[-1] == b"\x0b\x7f"
            assert len(_inputs(socket)) == before
        finally:
            await session.dispose()

    run(scenario())


@pytest.mark.parametrize("shell", [False, True])
def test_alt_mouse_jump_fences_mode_and_generation_change_mid_batch(shell):
    async def scenario():
        app, session, socket, channel = await _app(shell, "compact")
        try:
            await _view(app, channel, alt=True, mouse=True)
            channel.handler(protocol.encode_input(b"\x1b[<64;1;1M", protocol.ORIGIN_MOUSE))
            channel.handler(protocol.encode_input(b"\x1b[<64;1;1M", protocol.ORIGIN_MOUSE))
            await app.settle()
            send = session.sessions.keys._sender

            async def reset(data):
                await send(data)
                session.current_term.reset(reason="epoch_changed")

            session.sessions.keys._sender = reset
            before = len(_inputs(socket))
            await app.set_props_and_fire("terminal-scroll-latest", {}, "click")
            assert _inputs(socket)[before:] == [b"\x1b[<65;1;1M"]
            # RESET's native ViewState arrives on the next renderer frame.
            await _view(app, channel, alt=True, mouse=True)
            assert not app.find("terminal-scroll-latest").visible
        finally:
            await session.dispose()

    run(scenario())


@pytest.mark.parametrize("compose", [False, True])
def test_legacy_alt_no_mouse_gesture_key_tag_is_indistinguishable_from_physical_arrow(compose):
    async def scenario():
        app, session, socket, channel = await _app(False, "compact")
        try:
            if compose:
                await app.set_props_and_fire("agent-compose-toggle", {}, "click")
            await _view(app, channel, alt=True, mouse=False)
            # Pinned xterm2 scroll_handler._sendScrollEvent invokes keyInput
            # for unhandled alt/no-mouse gestures. The extension's keyInput
            # gives it this same tag as a physical arrow; no gesture tag exists.
            assert session.current_term.input_enabled
            channel.handler(protocol.encode_input(b"\x1b[A", protocol.ORIGIN_KEY))
            channel.handler(protocol.encode_input(b"\x1b[B", protocol.ORIGIN_KEY))
            await app.settle()
            assert _inputs(socket) == [b"\x1b[A", b"\x1b[B"]
            assert session.store.draft == "retained compose draft"
        finally:
            await session.dispose()

    run(scenario())
