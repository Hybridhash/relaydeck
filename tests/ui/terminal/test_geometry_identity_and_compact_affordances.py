"""Actual Flet/native dispatch; geometry identities, compact affordances and guarded jump."""

from dataclasses import replace

import flet as ft
import pytest
from relaydeck_terminal import protocol

from relaydeck.client.features.a11y.focus import iter_controls
from relaydeck.client.features.inventory.routing import RouteBridge
from relaydeck.client.features.terminal.key_bar import KEY_BAR
from relaydeck.client.features.terminal.scrolling import MAX_ALT_PAGES
from relaydeck.client.features.terminal.surfaces import direct_typing
from tests.support.host_harness import run
from tests.support.terminal.direct_typing import _change, _direct, _typing_key
from tests.support.terminal.latch_text import _inputs
from tests.support.terminal.scroll_ui import _app, _scroll_packets, _view
from tests.support.ui.instant_inventory import navigate, valid_inventory
from tests.support.ui.navigation import _StubTransport
from tests.support.ui.phone_terminal import _ancestry, _shape
from tests.support.ui.retained_root import _render


@pytest.mark.parametrize("shell", [False, True])
@pytest.mark.parametrize("tier", ["compact", "medium"])
def test_typing_and_icons_share_stable_fixed_height_row(shell, tier, monkeypatch) -> None:
    now = [1.0]
    monkeypatch.setattr(direct_typing, "_typing_now", lambda: now[0])

    async def scenario():
        app, session, _socket, _focuses = await _direct(shell, tier)
        try:
            term = session.current_term
            shape = _shape(_ancestry(app, term))
            row_key = "shell-keyboard-row" if shell else "agent-tab-row"
            keyboard_key = "shell-keyboard-toggle" if shell else "agent-workflow-toggle"
            row = app.find(row_key)
            keyboard, attachment = app.find(keyboard_key), app.find("terminal-attach-picture")
            field = app.find(_typing_key(app))
            identity = (row._i, keyboard._i, attachment._i, field._i)
            assert row.height == 40
            for key in (_typing_key(app), keyboard_key, "terminal-attach-picture"):
                path = _ancestry(app, app.find(key))
                assert row in path
            slot = app.find("shell-typing-slot" if shell else "agent-typing-slot")
            assert slot.expand and slot.height == 36
            if not shell:
                assert app.find("agent-tabs").visible is False
            for _ in range(2):
                now[0] += 0.5
                await app.set_props_and_fire(keyboard_key, {}, "click")
                row = app.find(row_key)
                assert (
                    row._i,
                    app.find(keyboard_key)._i,
                    app.find("terminal-attach-picture")._i,
                    app.find(_typing_key(app))._i,
                ) == identity
                assert row.height == 40 and row.controls[-1].key == (
                    "shell-keyboard-semantics" if shell else "agent-keyboard-semantics"
                )
                assert _shape(_ancestry(app, term)) == shape
            if not shell:
                await app.set_props_and_fire("agent-compose-toggle", {}, "click")
                assert not app.find(_typing_key(app)).visible
                assert app.find(keyboard_key)._i == identity[1]
                assert session.store.draft == "retained compose draft"
                now[0] += 0.5
                await app.set_props_and_fire(keyboard_key, {}, "click")
                assert app.find(_typing_key(app)).visible
                assert app.find(keyboard_key)._i == identity[1]
            if tier == "compact":
                assert app.find(keyboard_key).tooltip is None
                assert app.find("terminal-attach-picture").tooltip is None
                for item in KEY_BAR:
                    if item.kind == "action":
                        continue
                    control = app.find(item.id)
                    assert control.tooltip is None
                assert (
                    app.find(
                        "agent-keyboard-semantics" if not shell else "shell-keyboard-semantics"
                    ).label
                    == "Terminal keyboard"
                )
        finally:
            await session.dispose()

    run(scenario())


@pytest.mark.parametrize("shell", [False, True])
def test_alt_jump_matches_sent_pages_and_wheel_lines_and_normal_stays_local(shell) -> None:
    async def scenario():
        app, session, socket, channel = await _app(shell, "compact")
        try:
            term, shape = session.current_term, _shape(_ancestry(app, session.current_term))
            await _view(app, channel, alt=True, mouse=True)
            assert not app.find("terminal-scroll-latest").visible
            channel.handler(protocol.encode_input(b"\x1b[<64;1;1M", protocol.ORIGIN_MOUSE))
            await app.settle()
            for key in ("terminal-scroll-up", "terminal-scroll-up", "terminal-scroll-down"):
                await app.set_props_and_fire(key, {}, "click")
            channel.handler(protocol.encode_input(b"\x1b[A", protocol.ORIGIN_KEY))
            await app.settle()
            latest = app.find("terminal-scroll-latest")
            assert latest.visible and not latest.disabled
            assert latest.icon == ft.Icons.VERTICAL_ALIGN_BOTTOM
            before = len(_inputs(socket))
            await app.set_props_and_fire("terminal-scroll-latest", {}, "click")
            assert _inputs(socket)[before:] == [b"\x1b[<65;1;1M"] * 25
            # A full-screen app owns its scroll position, so the jump stays offered.
            assert app.find("terminal-scroll-latest").visible
            assert _scroll_packets(channel) == []
            await _view(app, channel, alt=False, bottom=False)
            count = len(_inputs(socket))
            await app.set_props_and_fire("terminal-scroll-latest", {}, "click")
            assert _scroll_packets(channel)[-1] == b"\x0b\x7f"
            assert len(_inputs(socket)) == count
            assert session.current_term is term and _shape(_ancestry(app, term)) == shape
        finally:
            await session.dispose()

    run(scenario())


@pytest.mark.parametrize("shell", [False, True])
@pytest.mark.parametrize("reset_mid_jump", [False, True])
def test_alt_jump_caps_writer_transaction_and_drops_remainder_on_native_reset(
    shell, reset_mid_jump
) -> None:
    async def scenario():
        app, session, socket, channel = await _app(shell, "compact")
        try:
            await _view(app, channel, alt=True)
            up = app.find("terminal-scroll-up").on_click
            for _ in range(MAX_ALT_PAGES + 2 if not reset_mid_jump else 2):
                await up(None)
            await app.settle()
            before = len(_inputs(socket))
            if reset_mid_jump:
                send = session.sessions.keys._sender

                async def reset_after_send(data):
                    await send(data)
                    session.current_term.reset(reason="epoch_changed")

                session.sessions.keys._sender = reset_after_send
            await app.set_props_and_fire("terminal-scroll-latest", {}, "click")
            assert _inputs(socket)[before:] == [b"\x1b[6~"] * (
                1 if reset_mid_jump else MAX_ALT_PAGES
            )
            await _view(app, channel, alt=True)
            assert app.find("terminal-scroll-latest").visible
        finally:
            await session.dispose()

    run(scenario())


@pytest.mark.parametrize(
    "reset", ["typing", "native-key", "enter", "generation", "screen", "watching", "send"]
)
def test_alt_jump_resets_and_stale_jump_never_writes(reset) -> None:
    async def scenario():
        app, session, socket, channel = await _app(False, "compact")
        try:
            await _view(app, channel, alt=True)
            await app.set_props_and_fire("terminal-scroll-up", {}, "click")
            assert app.find("terminal-scroll-latest").visible
            old = app.find("terminal-scroll-latest").on_click
            if reset == "typing":
                await _change(app, "  a")
            elif reset == "native-key":
                channel.handler(protocol.encode_input(b"a", protocol.ORIGIN_KEY))
            elif reset == "enter":
                await app.set_props_and_fire("key-enter", {}, "click")
            elif reset == "generation":
                session.current_term.reset(reason="epoch_changed")
                await _view(app, channel, alt=True)
            elif reset == "screen":
                await _view(app, channel, alt=False)
                await _view(app, channel, alt=True)
            elif reset == "watching":
                session.controller.mode = "view"
                session.controller.state = replace(session.controller.state, lease_mode="view")
                session._term_state = replace(session.term_state, lease_mode="view")
                session._notify()
            else:
                await app.set_props_and_fire("agent-compose-toggle", {}, "click")
                send = app.find("composer-draft").suffix_icon
                await app.session.dispatch_event(send._i, "click", None)
            await app.settle()
            # Still offered in control; a watcher cannot scroll, so it is hidden there.
            assert app.find("terminal-scroll-latest").visible == (reset != "watching")
            if reset in ("watching", "generation", "screen"):
                count = len(_inputs(socket))
                await old(None)
                assert len(_inputs(socket)) == count
        finally:
            await session.dispose()

    run(scenario())


@pytest.mark.parametrize("width", [390, 1280])
def test_actual_inventory_rows_keep_text_labels_without_compact_tooltips(
    width, monkeypatch
) -> None:
    monkeypatch.setattr(_StubTransport, "snapshot", valid_inventory)

    async def scenario():
        app, _controller, _scripted, _pool, _now = await _render(
            bridge=RouteBridge(width=width, height=844)
        )
        try:
            row = app.find("agent-home-mac:term-1")
            assert (row.tooltip is None) == (width == 390)
            assert any(isinstance(node, ft.Text) and node.value for node in iter_controls([row]))
            await navigate(app, "terminals", width)
            shell = app.find("shell-home-mac:tab-1")
            labels = [node.value for node in iter_controls([shell]) if isinstance(node, ft.Text)]
            assert _controller.state.inventory.tabs["home-mac:tab-1"].label in labels
            assert any("home" in label.lower() for label in labels)
            assert (shell.tooltip is None) == (width == 390)
        finally:
            await app.set_props_and_fire("test-unmount", {}, "click")

    run(scenario())
