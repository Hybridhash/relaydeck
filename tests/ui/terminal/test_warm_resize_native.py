"""Post-input layout reports over the real retained Root/terminal bridge."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from relaydeck_terminal import protocol

from relaydeck.client.features.a11y.focus import iter_controls
from relaydeck.client.features.inventory.routing import RouteBridge
from relaydeck.client.features.settings.prefs import PREFS_KEY
from relaydeck.client.features.terminal.input_modes import WORKFLOW_AGENT_DIRECT
from relaydeck.client.features.terminal.surfaces import agent_hooks
from tests.support.host_harness import run
from tests.support.terminal.resize import wire_sizes
from tests.support.terminal.resume import _MuxConn
from tests.support.terminal.retained_transport import capture, until
from tests.support.ui.instant_open import jobs
from tests.support.ui.packaged import Preferences
from tests.support.ui.retained_root import _active, _render

pytestmark = [pytest.mark.open_hold, pytest.mark.auto_takeover]


@pytest.mark.parametrize(
    "width,height,touch", [(1280, 900, False), (390, 844, True), (820, 1180, True)]
)
def test_post_input_enable_returning_grid_is_held_until_the_late_frame(
    monkeypatch: pytest.MonkeyPatch, width: int, height: int, touch: bool
) -> None:
    async def scenario() -> None:
        app, _, scripted, pool, _ = await _render(
            bridge=RouteBridge(width=width, height=height),
            touch=touch,
            preferences=Preferences(
                {PREFS_KEY: json.dumps({"agents_open_in": WORKFLOW_AGENT_DIRECT})}
            ),
        )
        release = asyncio.Event()
        try:
            await app.set_props_and_fire("agent-home-mac:term-1", {}, "click")
            session = pool.foreground.session
            session.on_draft_change("keep unsent")
            term = session.current_term
            conn = _MuxConn()
            app.session.attach_connection(conn)
            channel = await capture(app, term, conn, 71)
            reported: list[tuple[int, int]] = []
            term.add_size_listener(lambda cols, rows: reported.append((cols, rows)))
            forwarded: list[tuple[int, int]] = []
            resize = session.resize

            async def record(cols: int, rows: int) -> bool:
                forwarded.append((cols, rows))
                return await resize(cols, rows)

            monkeypatch.setattr(session, "resize", record)
            session._notify()
            await app.settle()
            channel.deliver(protocol.encode_size_report(166, 37))
            await asyncio.sleep(0.7)
            await app.settle()
            assert forwarded == [(166, 37)]
            await app.set_props_and_fire("agent-home-mac:term-2", {}, "click")
            await jobs(pool)
            entered = asyncio.Event()
            acquire = scripted.ops.acquire_lease

            async def blocked(attachment: str, mode: str, **kwargs: Any) -> Any:
                if mode == "control":
                    entered.set()
                    await release.wait()
                return await acquire(attachment, mode, **kwargs)

            monkeypatch.setattr(scripted.ops, "acquire_lease", blocked)
            await app.set_props_and_fire("agent-home-mac:term-1", {}, "click")
            await asyncio.wait_for(entered.wait(), 2)
            before = len(forwarded)
            channel.deliver(protocol.encode_view_report(protocol.ViewState(False, True, False)))
            release.set()
            await until(lambda: session.keys_enabled and not session.control_pending)
            await app.settle(rounds=2)
            assert term.input_enabled
            chrome = [control.key for control in _active(app, "agent-detail").controls]
            body = _active(app, "terminal-live-body").content
            flow = [control.key for control in body.controls]
            # Real native dispatch, after input is enabled, matching the live
            # report timing. These are injected grids, not pixel measurements.
            native_before = len(reported)
            channel.deliver(protocol.encode_size_report(166, 37))
            channel.deliver(protocol.encode_view_report(protocol.ViewState(True, True, False)))
            await app.settle(rounds=1)  # scroll_input_enabled flips on next render
            channel.deliver(protocol.encode_size_report(166, 35))
            await asyncio.sleep(0.36)
            channel.deliver(protocol.encode_size_report(166, 37))
            await asyncio.sleep(0.45)
            await app.settle(rounds=2)
            assert reported[native_before:] == [(166, 37), (166, 35), (166, 37)]
            assert forwarded[before:] == [(166, 37)]
            socket = next(
                sock
                for lease, _, sock in reversed(scripted.streams.opened)
                if lease == session.lease.lease_id
            )
            assert wire_sizes(socket) == [(166, 37)]
            assert [control.key for control in _active(app, "agent-detail").controls] == chrome
            assert [
                control.key for control in _active(app, "terminal-live-body").content.controls
            ] == flow
            assert session.current_term is term and session.store.draft == "keep unsent"
            # A real change and later same-size redraw each settle briefly.
            for size in [(150, 31), (150, 31)]:
                count = len(forwarded)
                channel.deliver(protocol.encode_size_report(*size))
                await asyncio.sleep(0)
                await asyncio.sleep(0)
                assert forwarded[count:] == []
                await asyncio.sleep(agent_hooks.RESIZE_SETTLE_S + 0.03)
                assert forwarded[count:] == [size]
            assert wire_sizes(socket) == [(166, 37), (150, 31), (150, 31)]
            before = len(forwarded)
            for index, rows in enumerate([37, 39, 39, 40, 41, 34, 39]):
                if index:
                    await asyncio.sleep(0.175 / 6)
                channel.deliver(protocol.encode_size_report(225, rows))
                assert forwarded[before:] == []
            await asyncio.sleep(agent_hooks.RESIZE_SETTLE_S + 0.03)
            assert forwarded[before:] == [(225, 39)]
            assert wire_sizes(socket) == [(166, 37), (150, 31), (150, 31), (225, 39)]
            connecting = next(
                control
                for control in iter_controls([_active(app, "terminal-viewport-stack")])
                if control.key == "terminal-connecting"
            )
            assert not connecting.visible
            assert session.current_term is term and session.store.draft == "keep unsent"
        finally:
            release.set()
            await app.set_props_and_fire("test-unmount", {}, "click")

    run(scenario())
