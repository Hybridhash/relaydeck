"""Dispatched Root open, native snapshot bridge and safe preconnect ordering."""

import asyncio
import struct
from typing import Any

import pytest
from flet.data_channel import _ProtocolMuxedDataChannel
from relaydeck_terminal import protocol

from relaydeck.client.adapters.fake import FakeCredentialStore
from relaydeck.client.features.inventory.routing import RouteBridge
from relaydeck.gateway import terminal_wire
from tests.support.host_harness import run
from tests.support.sync.transport_fixtures import _wait
from tests.support.ui.instant_open import jobs
from tests.support.ui.retained_root import _active, _open, _render


@pytest.mark.parametrize("width", [390, 1280])
@pytest.mark.open_hold
def test_actual_snapshot_cached_open_and_evicted_reopen_replaced_on_live_frame(
    width: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("relaydeck.client.features.terminal.surfaces.agent_hooks.OPEN_HOLD_S", 10)
    monkeypatch.setattr("relaydeck.client.features.terminal.surfaces.agent_hooks.OPEN_SETTLE_S", 10)

    async def scenario() -> None:
        app, _controller, scripted, pool, _now = await _render(
            bridge=RouteBridge(width=width, height=844),
        )
        try:
            await _open(app, "home-mac:term-1")
            first = pool.foreground
            session, term = first.session, first.session.current_term
            channel = _ProtocolMuxedDataChannel(83, app.conn)
            app.conn.data_channel_for = lambda _: channel
            await app.set_props_and_fire(
                "terminal-surface",
                {},
                "data_channel_open",
                {"channel_name": "terminal", "channel_id": 83},
            )
            requests: list[int] = []
            original_send = app.conn.send_data_channel_frame

            def snapshot_reply(number: int, packet: bytes) -> None:
                original_send(number, packet)
                if packet[0] == protocol.REQUEST_SNAPSHOT:
                    request = struct.unpack_from(">I", packet, 1)[0]
                    requests.append(request)
                    text = b"last rendered screen"
                    channel._deliver(protocol.encode_snapshot_chunk(request, len(text), 0, text))

            monkeypatch.setattr(app.conn, "send_data_channel_frame", snapshot_reply)
            session.on_draft_change("retained draft")
            await _open(app, "home-mac:term-2")
            await jobs(pool)
            assert requests and len(pool.screens) == 1
            await app.set_props_and_fire("agent-home-mac:term-1", {}, "click")
            await app.settle()
            assert pool.foreground is first and session.current_term is term
            assert session.lease.mode == "view" and len(scripted.ops.attaches) == 2
            assert _active(app, "agent-takeover") is not None
            assert _active(app, "terminal-cache-text").value == "last rendered screen"
            assert _active(app, "terminal-cache-updating").value == "Updating…"
            assert _active(app, "terminal-cached-screen").visible
            # The open-size hold must not paint an opaque cover over the preview.
            channel._deliver(protocol.encode_size_report(80, 24))
            await app.settle()
            assert _active(app, "terminal-connecting").bgcolor is None
            assert session.store.draft == "retained draft"
            socket = next(
                sock
                for lease, _, sock in scripted.streams.opened
                if lease == session.lease.lease_id
            )
            socket.queue(("bytes", terminal_wire.encode_output(session.lease.epoch, 0, b"live")))
            await _wait(lambda: session.cached_screen == "")
            await app.settle()
            assert not app.find("terminal-cached-screen").visible
            assert session.current_term is term
            # An evicted native view can still use its memory-only snapshot and
            # known attachment, through a new authenticated VIEW lease only.
            await app.set_props_and_fire("agent-home-mac:term-2", {}, "click")
            await jobs(pool)
            await pool.discard(first)
            await app.settle()
            await app.set_props_and_fire("agent-home-mac:term-1", {}, "click")
            await app.settle()
            replacement = pool.foreground.session
            assert replacement is not session and replacement.lease.mode == "view"
            assert replacement.cached_screen == "last rendered screen"
            assert len(scripted.ops.attaches) == 2 and not scripted.ops.closes
        finally:
            await app.set_props_and_fire("test-unmount", {}, "click")
        assert not pool.entries() and len(pool.screens) == 0

    run(scenario())


@pytest.mark.parametrize("width", [390, 1280])
def test_terminal_publishes_before_secure_draft_restore_input_stays_gated(width: int) -> None:
    async def scenario() -> None:
        app, _controller, scripted, pool, _now = await _render(
            bridge=RouteBridge(width=width, height=844),
        )
        entered, finish = asyncio.Event(), asyncio.Event()

        class SlowStore(FakeCredentialStore):
            async def get(self, key: str) -> str | None:
                entered.set()
                await finish.wait()
                return None

        scripted.factory.draft_store = SlowStore()
        try:
            await app.set_props_and_fire("agent-home-mac:term-1", {}, "click")
            await _wait(entered.is_set)
            await app.settle()
            session = pool.foreground.session
            assert session.current_term is not None and session.draft_loading
            assert app.find("agent-input-dock").disabled
            if width == 390:
                assert not app.find("composer-draft").can_request_focus
            session.composer_callbacks()["on_draft_change"]("must not overwrite saved draft")
            assert session.store.draft == "" and not scripted.ops.attaches
            finish.set()
            await _wait(lambda: not session.draft_loading)
            await app.set_props_and_fire("agent-takeover-confirm", {}, "click")
            if width == 390:
                await app.set_props_and_fire("agent-compose-toggle", {}, "click")
            await app.settle()
            assert _active(app, "composer-draft").can_request_focus
        finally:
            finish.set()
            await app.set_props_and_fire("test-unmount", {}, "click")

    run(scenario())


def test_shell_row_starts_attachment_while_transition_is_rendered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        app, _controller, scripted, pool, _now = await _render()
        entered, finish = asyncio.Event(), asyncio.Event()
        attach = scripted.ops.attach

        async def delayed_attach(*args: Any, **kwargs: Any) -> Any:
            entered.set()
            await finish.wait()
            return await attach(*args, **kwargs)

        monkeypatch.setattr(scripted.ops, "attach", delayed_attach)
        try:
            await app.set_props_and_fire("console-rail-terminals", {}, "click")
            await app.set_props_and_fire("shell-home-mac:tab-1", {}, "click")
            await _wait(entered.is_set)
            await app.settle()
            assert pool.foreground.kind == "shell"
            assert pool.foreground.session.current_term is not None
            assert not scripted.ops.leases  # open screen does not await remote attach
            finish.set()
            await _wait(lambda: pool.foreground.session.phase == "live")
            assert scripted.ops.attaches[0]["takeover"] is False
            await app.set_props_and_fire("console-rail-console", {}, "click")
            await app.set_props_and_fire("console-rail-terminals", {}, "click")
            await app.set_props_and_fire("shell-home-mac:tab-1", {}, "click")
            await app.settle()
            assert pool.foreground.session.lease.mode == "view"
            assert _active(app, "shell-takeover") is not None
            assert len(scripted.ops.attaches) == 1
        finally:
            finish.set()
            await app.set_props_and_fire("test-unmount", {}, "click")

    run(scenario())
