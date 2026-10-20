"""Warm-open layout and native size reports through the retained Root."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any

import pytest
from relaydeck_terminal import protocol

from relaydeck.client.features.inventory.routing import RouteBridge
from relaydeck.client.features.settings.prefs import PREFS_KEY
from relaydeck.client.features.terminal.input_modes import (
    WORKFLOW_AGENT_COMPOSER,
    WORKFLOW_AGENT_DIRECT,
)
from relaydeck.client.features.terminal.surfaces import agent_hooks
from tests.support.host_harness import run
from tests.support.terminal.resize import wire_sizes
from tests.support.terminal.resume import _MuxConn
from tests.support.terminal.retained_transport import capture
from tests.support.ui.hook_clock import HookClock
from tests.support.ui.instant_open import jobs
from tests.support.ui.packaged import Preferences
from tests.support.ui.retained_root import _active, _render

pytestmark = [pytest.mark.open_hold, pytest.mark.auto_takeover]


@dataclass(frozen=True)
class Layout:
    name: str
    width: int
    height: int
    size: tuple[int, int]
    touch: bool = False
    keyboard_height: int | None = None


LAYOUTS = [
    Layout("desktop", 1280, 900, (166, 37)),
    Layout("phone", 390, 844, (48, 30), touch=True),
    Layout("phone-keyboard", 390, 844, (48, 16), touch=True, keyboard_height=500),
    Layout("ipad-portrait", 820, 1180, (98, 45), touch=True),
    Layout("ipad-split-view", 600, 1024, (70, 38), touch=True),
    Layout("ipad-typing-split", 1376, 1032, (92, 12), touch=True, keyboard_height=396),
]


async def _resize_viewport(app: Any, bridge: RouteBridge, width: int, height: int) -> None:
    app.page.on_resize = lambda e: bridge.set_size(app.page.width, app.page.height)
    app.session.apply_patch(app.page._i, {"width": width, "height": height})
    await app.session.dispatch_event(app.page._i, "resize", {"width": width, "height": height})
    await app.settle(rounds=2)


async def _choose_mode(app: Any, layout: Layout, mode: str) -> None:
    """Phone tier: tools button, then the mode segment. Wider tiers keep their toggles."""
    if layout.width < 600:
        await app.session.dispatch_event(_active(app, "terminal-tools-button")._i, "click", "")
        await app.settle(rounds=1)
        await app.session.dispatch_event(_active(app, f"tools-mode-{mode}")._i, "click", "")
    else:
        toggle = "agent-compose-toggle" if mode == "compose" else "agent-workflow-toggle"
        await app.session.dispatch_event(_active(app, toggle)._i, "click", "")
    await app.settle(rounds=1)


async def _keyboard_layout(app: Any, layout: Layout, workflow: str) -> None:
    if layout.keyboard_height is None:
        return
    if workflow == WORKFLOW_AGENT_COMPOSER:
        try:
            _active(app, "composer-draft")
        except KeyError:
            await _choose_mode(app, layout, "compose")
        field = _active(app, "composer-draft")
        assert field.can_request_focus and not field.disabled
        await app.session.dispatch_event(field._i, "focus", "")
        await app.settle(rounds=1)
    elif layout.width < 1024:
        try:
            _active(app, "agent-workflow-direct-typing")
        except KeyError:
            await _choose_mode(app, layout, "direct")
        assert _active(app, "agent-workflow-direct-typing").can_request_focus
        assert _active(app, "agent-workflow-toggle").selected
    if layout.name == "ipad-typing-split":
        assert app.find("rail-slot").visible is (workflow != WORKFLOW_AGENT_COMPOSER)


def _real_changes(layout: Layout) -> list[tuple[str, int, int, tuple[int, int]]]:
    if not layout.touch:
        return [
            ("browser-window", 1152, 780, (144, 32)),
            ("other-display", 1920, 1080, (220, 50)),
            ("display-scale-same-viewport", 1920, 1080, (200, 45)),
            ("browser-zoom", 1536, 864, (168, 36)),
        ]
    if layout.width < 600:
        return [
            ("phone-rotation-landscape", 844, 390, (100, 10)),
            ("phone-rotation-portrait", 390, 844, (48, 30)),
            ("phone-keyboard-show", 390, 500, (48, 16)),
            ("phone-keyboard-hide", 390, 844, (48, 30)),
        ]
    return [
        ("ipad-split-resize", 720, 1024, (85, 38)),
        ("ipad-full-width", 1376, 1032, (160, 40)),
        ("ipad-keyboard-show", 1376, 396, (92, 12)),
        ("ipad-keyboard-hide", 1376, 1032, (160, 40)),
    ]


@pytest.mark.parametrize("layout", LAYOUTS, ids=lambda layout: layout.name)
@pytest.mark.parametrize("resize_while_held", [False, True], ids=["steady", "resize-held"])
@pytest.mark.parametrize("workflow", [WORKFLOW_AGENT_COMPOSER, WORKFLOW_AGENT_DIRECT])
def test_warm_terminal_keeps_final_chrome_and_forwards_one_settled_resize(
    monkeypatch: pytest.MonkeyPatch,
    workflow: str,
    layout: Layout,
    resize_while_held: bool,
) -> None:
    async def scenario() -> None:
        clock = HookClock(monkeypatch, agent_hooks.__name__)
        bridge = RouteBridge(width=layout.width, height=layout.height)
        app, _, scripted, pool, _ = await _render(
            bridge=bridge,
            touch=layout.touch,
            preferences=Preferences({PREFS_KEY: json.dumps({"agents_open_in": workflow})}),
        )
        release = asyncio.Event()
        try:
            await app.set_props_and_fire("agent-home-mac:term-1", {}, "click")
            session = pool.foreground.session
            session.on_draft_change("Unsent draft stays here")
            term = session.current_term
            conn = _MuxConn()
            app.session.attach_connection(conn)
            channel = await capture(app, term, conn, 71)
            reported: list[tuple[int, int]] = []
            forwarded: list[tuple[int, int]] = []
            term.add_size_listener(lambda cols, rows: reported.append((cols, rows)))
            resize = session.resize

            async def record(cols: int, rows: int) -> bool:
                forwarded.append((cols, rows))
                return await resize(cols, rows)

            monkeypatch.setattr(session, "resize", record)
            session._notify()
            await app.settle()
            if layout.keyboard_height is not None:
                await _resize_viewport(app, bridge, layout.width, layout.keyboard_height)
            await _keyboard_layout(app, layout, workflow)
            channel.deliver(protocol.encode_size_report(*layout.size))
            await app.settle(rounds=1)
            clock.advance_by(agent_hooks.OPEN_HOLD_S)
            await app.settle()
            assert forwarded == [layout.size]  # Cold open still forwards once.
            final_chrome = [c.key for c in _active(app, "agent-detail").controls]
            assert "agent-takeover-frame" not in final_chrome
            await app.set_props_and_fire("agent-home-mac:term-2", {}, "click")
            await jobs(pool)
            await app.settle()
            assert session.lease.mode == "view"
            entered = asyncio.Event()
            original = scripted.ops.acquire_lease

            async def acquire(attachment: str, mode: str, **kwargs: Any):
                if mode == "control":
                    entered.set()
                    await release.wait()
                return await original(attachment, mode, **kwargs)

            monkeypatch.setattr(scripted.ops, "acquire_lease", acquire)
            await app.set_props_and_fire("agent-home-mac:term-1", {}, "click")
            await asyncio.wait_for(entered.wait(), 2)
            await app.settle()
            # A closed old reader may project disconnected during the new control flight.
            assert [c.key for c in _active(app, "agent-detail").controls] == final_chrome
            assert session.control_pending
            before = len(forwarded)
            native_before = len(reported)
            # Native parser/listeners receive the observed frame sequence. This is
            # a protocol/lifecycle test, not a Flutter pixel measurement.
            cols, rows = layout.size
            channel.deliver(protocol.encode_size_report(cols, rows))
            await app.settle(rounds=1)
            channel.deliver(protocol.encode_size_report(cols, rows - 3))
            await app.settle(rounds=1)
            clock.advance_by(agent_hooks.OPEN_HOLD_S * 2)
            await app.settle(rounds=1)  # Slow control outlives the ordinary hold cap.
            assert len(forwarded) == before
            release.set()
            await app.settle(rounds=2)
            session.controller.fence_ui_input(True)
            session._notify()
            await app.settle(rounds=2)
            final_size = (cols - 6, rows - 2) if resize_while_held else layout.size
            if resize_while_held:
                # A real viewport change during opening replaces the pending geometry.
                await _resize_viewport(
                    app, bridge, layout.width - 30, (layout.keyboard_height or layout.height) - 40
                )
            channel.deliver(protocol.encode_size_report(*final_size))
            await app.settle(rounds=1)
            clock.advance_by(agent_hooks.OPEN_SETTLE_S / 2)
            await app.settle(rounds=1)
            channel.deliver(protocol.encode_size_report(*final_size))
            await app.settle(rounds=1)
            clock.advance_by(agent_hooks.OPEN_HOLD_S * 2)
            await app.settle(rounds=1)
            assert len(forwarded) == before
            session.controller.fence_ui_input(False)
            session._notify()
            await app.settle(rounds=1)
            await _keyboard_layout(app, layout, workflow)
            clock.advance_by(agent_hooks.OPEN_HOLD_S)
            await app.settle(rounds=1)
            assert len(forwarded) == before  # No pre-readiness geometry may be replayed.
            channel.deliver(protocol.encode_size_report(*final_size))
            await app.settle(rounds=1)
            clock.advance_by(agent_hooks.RESIZE_SETTLE_S)
            await app.settle()
            assert reported[native_before:] == [
                layout.size,
                (cols, rows - 3),
                final_size,
                final_size,
                final_size,
            ]
            assert forwarded[before:] == [final_size]
            assert [c.key for c in _active(app, "agent-detail").controls] == final_chrome
            assert session.current_term is term
            assert session.store.draft == "Unsent draft stays here"
            assert session.keys_enabled and not session.controller.ui_input_fenced
            socket = next(
                sock
                for lease, _, sock in reversed(scripted.streams.opened)
                if lease == session.lease.lease_id
            )
            assert wire_sizes(socket) == [final_size]
            # Identical later reports still repaint; there is no size dedupe.
            channel.deliver(protocol.encode_size_report(*final_size))
            await app.settle(rounds=1)
            clock.advance_by(agent_hooks.RESIZE_SETTLE_S)
            await app.settle()
            assert forwarded[before:] == [final_size, final_size]
            assert wire_sizes(socket) == [final_size, final_size]
            expected_sizes = [final_size, final_size]
            for cause, width, height, size in _real_changes(layout):
                await _resize_viewport(app, bridge, width, height)
                count = len(forwarded)
                channel.deliver(protocol.encode_size_report(*size))
                await app.settle(rounds=1)
                # The opening hold never restarts; live geometry settles briefly.
                await asyncio.sleep(0)
                await asyncio.sleep(0)
                assert forwarded[count:] == [], cause
                clock.advance_by(agent_hooks.RESIZE_SETTLE_S)
                await app.settle(rounds=1)
                assert forwarded[count:] == [size], cause
                expected_sizes.append(size)
                assert wire_sizes(socket) == expected_sizes, cause
                assert (
                    session.current_term is term
                    and session.store.draft == "Unsent draft stays here"
                )
        finally:
            release.set()
            await app.set_props_and_fire("test-unmount", {}, "click")

    run(scenario())
