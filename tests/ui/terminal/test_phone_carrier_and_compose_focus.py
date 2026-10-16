"""Actual Root regressions for Direct carrier text and Compose focus/taps."""

from __future__ import annotations

import struct

import pytest
from flet.data_channel import _ProtocolMuxedDataChannel
from relaydeck_terminal import protocol

from relaydeck.client.features.inventory.routing import RouteBridge
from relaydeck.client.features.terminal.input_modes import WORKFLOW_AGENT_DIRECT
from relaydeck.gateway import terminal_wire
from tests.support.host_harness import run
from tests.support.ui.editor_patches import assert_only_editor_idle_patches
from tests.support.ui.phone_terminal import _ancestry, _shape
from tests.support.ui.retained_root import _render


@pytest.mark.parametrize("latch", ["off", "ctrl-once", "ctrl-locked", "alt-once"])
def test_reading_icon_native_text_reaches_pty_before_and_after_keyboard_resize(latch: str) -> None:
    async def scenario() -> None:
        bridge = RouteBridge(width=390, height=844)
        app, _controller, sessions, pool, _now = await _render(bridge=bridge)
        try:
            await app.set_props_and_fire("agent-home-mac:term-1", {}, "click")
            await app.set_props_and_fire("agent-takeover-confirm", {}, "click")
            session = pool.foreground.session
            term, writer, token = session.current_term, session.controller, session.input_token
            session.on_draft_change("unsent phone draft")
            await app.settle()
            channel = _ProtocolMuxedDataChannel(81, app.conn)
            app.conn.data_channel_for = lambda number: channel
            await app.set_props_and_fire(
                "terminal-surface",
                {},
                "data_channel_open",
                {"channel_name": "terminal", "channel_id": 81},
            )
            binding = session._viewer_binding
            socket = sessions.streams.opened[0][2]

            def inputs() -> list[bytes]:
                return [
                    frame.data
                    for kind, raw in socket.sent
                    if kind == "bytes"
                    if (frame := terminal_wire.decode_input(raw)).kind == "text"
                ]

            async def arm() -> None:
                if latch == "off":
                    return
                key = "mod-alt" if latch == "alt-once" else "mod-ctrl"
                button = app.find(key)
                await app.session.dispatch_event(button._i, "click", "")
                if latch == "ctrl-locked":
                    await app.session.dispatch_event(button._i, "click", "")
                await app.settle()

            await arm()
            start = len(app.conn.data_frames)
            # Channel already open, matching the phone: native text can arrive
            # immediately after the explicit icon, before a settle/rerender.
            await app.session.dispatch_event(app.find("agent-workflow-toggle")._i, "click", "")
            channel._deliver(protocol.encode_input(b"a", protocol.ORIGIN_TEXT))
            await app.settle()
            assert session.view().workflow == WORKFLOW_AGENT_DIRECT
            assert session.input_token == token and session.keys_enabled and session.foreground
            assert term.input_enabled is True
            packets = [p for _, p in app.conn.data_frames[start:]]
            at = packets.index(protocol.encode_request_keyboard())
            assert packets[at - 1] == protocol.encode_input_enabled(True)
            single = b"a" if latch == "off" else b"\x1ba" if latch == "alt-once" else b"\x01"
            assert inputs() == [single]
            if latch.endswith("once"):
                await arm()  # the multi-character commit is also tested while armed
            app.page.on_resize = lambda e: bridge.set_size(app.page.width, app.page.height)
            app.session.apply_patch(app.page._i, {"width": 390, "height": 544})
            await app.session.dispatch_event(app.page._i, "resize", {"width": 390, "height": 544})
            await app.settle()
            channel._deliver(protocol.encode_input(b"hello", protocol.ORIGIN_TEXT))
            await app.settle()
            assert inputs() == [single, b"hello"]  # unchanged multi-character IME fall-through
            if latch != "off":
                assert session.sessions.keys.pending_modifiers == frozenset(
                    {"alt" if latch == "alt-once" else "ctrl"}
                )
            channel._deliver(protocol.encode_input(b"a", protocol.ORIGIN_TEXT))
            await app.settle()
            assert inputs() == [single, b"hello", single]
            assert term.input_enabled is True and session.input_token == token
            assert session.current_term is term and session.controller is writer
            assert session._viewer_binding == binding and term.viewer_generation == 1
            assert session.store.draft == "unsent phone draft"
            assert len(sessions.ops.attaches) == 1 and len(sessions.streams.opened) == 1
        finally:
            await app.set_props_and_fire("test-unmount", {}, "click")

    run(scenario())


@pytest.mark.parametrize("width", [360, 390])
def test_multiline_compose_focus_and_tap_do_not_rebuild_or_rewrite_field(width: int) -> None:
    async def scenario() -> None:
        app, _controller, _sessions, pool, _now = await _render(
            bridge=RouteBridge(width=width, height=544)
        )
        try:
            await app.set_props_and_fire("agent-home-mac:term-1", {}, "click")
            await app.set_props_and_fire("agent-takeover-confirm", {}, "click")
            session = pool.foreground.session
            draft = "\n".join(f"line {n}: long scrolling draft with words" for n in range(16))
            session.on_draft_change(draft)
            await app.set_props_and_fire("agent-compose-toggle", {}, "click")
            field = app.find("composer-draft")
            term = session.current_term
            ancestry = _shape(_ancestry(app, field))
            props = (field.value, field.selection, field.min_lines, field.max_lines)
            start = len(app.conn.sent)
            await app.session.dispatch_event(field._i, "focus", "")
            await app.settle()
            assert app.find("composer-draft") is field
            await app.session.dispatch_event(field._i, "click", "")
            await app.settle()
            await app.session.dispatch_event(field._i, "click", "")
            await app.settle()
            assert app.find("composer-draft") is field
            assert _shape(_ancestry(app, field)) == ancestry
            assert (field.value, field.selection, field.min_lines, field.max_lines) == props
            assert_only_editor_idle_patches(app, field, app.conn.sent[start:])
            assert session.store.draft == draft and session.current_term is term
        finally:
            await app.set_props_and_fire("test-unmount", {}, "click")

    run(scenario())


@pytest.mark.parametrize("direct", [False, True])
def test_compact_select_survives_keyboard_resize_and_live_output_until_done(
    direct: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        bridge = RouteBridge(width=390, height=844)
        app, _controller, sessions, pool, _now = await _render(bridge=bridge)
        try:
            await app.set_props_and_fire("agent-home-mac:term-1", {}, "click")
            await app.set_props_and_fire("agent-takeover-confirm", {}, "click")
            session = pool.foreground.session
            session.on_draft_change("retained selection draft")
            channel = _ProtocolMuxedDataChannel(83, app.conn)
            app.conn.data_channel_for = lambda number: channel
            await app.set_props_and_fire(
                "terminal-surface",
                {},
                "data_channel_open",
                {"channel_name": "terminal", "channel_id": 83},
            )
            if direct:
                await app.set_props_and_fire("agent-workflow-toggle", {}, "click")
            term, writer = session.current_term, session.controller
            binding = session._viewer_binding
            engine_shape = _shape(_ancestry(app, term))
            socket = sessions.streams.opened[0][2]
            text = "synthetic greeting\nsecond line\n"
            client = writer._client
            socket.queue(
                (
                    "bytes",
                    terminal_wire.encode_output(
                        client.epoch or "e1", client.next_offset, text.encode()
                    ),
                )
            )
            await app.settle()
            send_frame = app.conn.send_data_channel_frame

            def reply(number: int, packet: bytes) -> None:
                send_frame(number, packet)
                if packet[0] == protocol.REQUEST_SNAPSHOT:
                    req_id = struct.unpack(">I", packet[1:5])[0]
                    data = text.encode()
                    channel._deliver(protocol.encode_snapshot_chunk(req_id, len(data), 0, data))

            monkeypatch.setattr(app.conn, "send_data_channel_frame", reply)
            await app.set_props_and_fire("terminal-select-text", {}, "click")
            assert session.term_state.selection.open
            assert app.find("phone-select-field").value == text
            # Model a user placing/dragging the native caret before the keyboard closes.
            await app.set_props_and_fire("phone-select-field", {}, "focus")
            await app.set_props_and_fire(
                "phone-select-field",
                {"selection": {"base_offset": 0, "extent_offset": 9}},
                "selection_change",
                {"selected_text": "synthetic", "selection": {"base_offset": 0, "extent_offset": 9}},
            )
            bridge.set_size(390, 544)
            await app.settle()
            channel._deliver(protocol.encode_size_report(40, 12))
            await app.settle()
            bridge.set_size(390, 844)
            await app.settle()
            assert session.term_state.selection.open
            assert app.find("phone-select-field").value == text
            # An ordinary contiguous server output frame must not dismiss the frozen sheet.
            socket.queue(
                (
                    "bytes",
                    terminal_wire.encode_output(client.epoch, client.next_offset, b"more output\n"),
                )
            )
            await app.settle()
            assert session.term_state.selection.open
            assert app.find("phone-select-field").value == text
            await app.set_props_and_fire("phone-select-copy-selection", {}, "click")
            assert sessions.clipboard.writes == ["synthetic"]
            assert session.term_state.selection.open  # Copy does not implicitly dismiss
            assert session.current_term is term and session.controller is writer
            assert (
                session._viewer_binding == binding and _shape(_ancestry(app, term)) == engine_shape
            )
            assert session.store.draft == "retained selection draft"
            await app.set_props_and_fire("phone-select-done", {}, "click")
            assert not session.term_state.selection.open
            socket.queue(
                (
                    "bytes",
                    terminal_wire.encode_output(client.epoch, client.next_offset, b"after done\n"),
                )
            )
            await app.settle()
            assert not session.term_state.selection.open
        finally:
            await app.set_props_and_fire("test-unmount", {}, "click")

    run(scenario())
