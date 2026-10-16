"""The terminal status dot is green only after terminal verification.

Metadata liveness alone never turns the dot green. A retained viewer whose native terminal is not
verified shows the checking text, and a terminal recovery notice is shown as the dot's text. Pages
without an open terminal are unaffected by the dot logic.

The verified cases use a real confirmed takeover (`_open`) and a real resume probe answered by the
synthetic gateway, so the native transport is verified without pinning any terminal property.
"""

from __future__ import annotations

import asyncio
import struct
from types import SimpleNamespace
from typing import Any

from relaydeck_terminal import protocol

from relaydeck.client.components.design_system import tokens as T
from relaydeck.client.features.inventory.routing import RouteBridge
from relaydeck.client.features.sync.transport import TransportError
from relaydeck.client.features.terminal.agent_session import AgentSession
from relaydeck.client.features.terminal.foreground_recovery import ForegroundRecovery
from relaydeck.client.features.terminal.models import NoticeModel
from relaydeck.client.resources import AppResources
from relaydeck.domain import reducers
from relaydeck.domain.models import ConnectionState
from relaydeck.gateway import terminal_wire
from tests.support.host_harness import run
from tests.support.terminal.resume import _hello, _MuxConn, _report
from tests.support.ui.instant_inventory import valid_inventory
from tests.support.ui.navigation import _StubTransport
from tests.support.ui.retained_root import _open, _render
from tests.support.ui.window_design import _missing

RECOVERY_NOTICE = "Viewer restarted; reattach explicitly. History may be incomplete."


def _verified(value: bool) -> Any:
    """The verified-native property, overridden for the rendering decision under test."""
    return property(lambda self: value)


def _terminal_state(monkeypatch: Any, status: str, notice: NoticeModel | None = None) -> None:
    """Drive the terminal's view state for the dot. The attach resets the stored state
    asynchronously, so the test pins what the root reads instead of racing that reset."""
    from relaydeck.client.features.terminal.models import TerminalViewState

    state = TerminalViewState(lease_mode="control", status=status, notice=notice)
    monkeypatch.setattr(AgentSession, "term_state", property(lambda self: state))


async def _open_live(width: int = 390) -> Any:  # phone: the dot carries the state
    app, controller, _sessions, pool, _now = await _render(bridge=RouteBridge(width=width))
    await app.set_props_and_fire("agent-home-mac:term-1", {}, "click")
    await app.settle()
    return app, controller, pool.foreground.session


async def _until(check: Any) -> None:
    async with asyncio.timeout(4):
        while not check():
            await asyncio.sleep(0.01)


def _resume_requests(conn: _MuxConn) -> list[tuple[int, int]]:
    return [
        (number, struct.unpack_from(">I", packet, 1)[0])
        for number, packet in conn.data_frames
        if packet[0] == protocol.RESUME_PROBE
    ]


def _resources(pool: Any) -> AppResources:
    """Only the retained pool and the command close are read by the resume path."""

    async def close() -> None:
        pass

    build: Any = AppResources  # synthetic fields: the constructor is typed for live objects
    resources: AppResources = build(
        props={},
        client=None,
        owns_client=False,
        retained=pool,
        bridge=None,
        controller=None,
        transport=None,
        commands=SimpleNamespace(close=close),
    )
    return resources


async def _verified_root(monkeypatch: Any) -> tuple[Any, Any, Any]:
    """A root with a confirmed takeover on a valid inventory and a verified native transport."""
    monkeypatch.setattr(_StubTransport, "snapshot", valid_inventory)

    async def unavailable(*args: Any, **kwargs: Any) -> Any:
        raise TransportError("usage_disabled", "synthetic unavailable", False)

    monkeypatch.setattr(_StubTransport, "usage", unavailable, raising=False)
    app, controller, scripted, pool, _now = await _render(bridge=RouteBridge(width=390, height=844))
    await _open(app, "home-mac:term-1")  # confirms the takeover: the agent is in control
    session = pool.foreground.session
    conn = _MuxConn()
    app.session.attach_connection(conn)
    term = session.current_term
    await app.session.dispatch_event(
        term._i, "data_channel_open", {"channel_name": "terminal", "channel_id": 7}
    )
    conn.channels[7].deliver(_hello(instance=123))
    await app.settle()
    resources = _resources(pool)
    resources.resume_terminals(transport=object(), current=lambda: True, closed=asyncio.Event())
    await _until(lambda: _resume_requests(conn))
    applied_at_probe = term._sent_bytes
    socket = next(
        sock for lease, _, sock in scripted.streams.opened if lease == session.lease.lease_id
    )
    baseline = b"baseline"
    socket.queue(("bytes", terminal_wire.encode_output(session.lease.epoch, 0, baseline)))
    await _until(lambda: session.term_state.applied_offset == len(baseline))
    await _until(lambda: _resume_requests(conn)[-1] == (7, term._resume_next_id))
    conn.channels[7].deliver(
        _report(
            term._resume_next_id, instance=123, applied=applied_at_probe, resets=term._reset_count
        )
    )
    await _until(lambda: term.transport_verified)
    conn.channels[7].deliver(protocol.encode_ack(len(baseline)))
    conn.channels[7].deliver(
        protocol.encode_view_report(protocol.ViewState(alt=False, at_bottom=True, mouse=False))
    )
    conn.channels[7].deliver(protocol.encode_size_report(40, 20))
    await app.settle()
    controller.push(reducers.set_connection(controller.state, ConnectionState.LIVE))
    await app.settle()
    return app, controller, session


def _dot(app: Any) -> Any:
    return app.find("terminal-connection-dot")


def test_metadata_live_without_a_verified_terminal_is_not_green(monkeypatch: Any) -> None:
    async def scenario() -> None:
        monkeypatch.setattr(AgentSession, "terminal_live", _verified(False))
        app, controller, session = await _open_live()
        try:
            controller.push(reducers.set_connection(controller.state, ConnectionState.LIVE))
            _terminal_state(monkeypatch, "live")
            await app.settle()
            dot = _dot(app)
            assert dot.content.content.bgcolor != T.DONE  # never green without verification
            assert dot.tooltip != "Live"
        finally:
            await app.set_props_and_fire("test-unmount", {}, "click")

    run(scenario())


def test_a_verified_terminal_with_live_metadata_is_green(monkeypatch: Any) -> None:
    async def scenario() -> None:
        app, _controller, session = await _verified_root(monkeypatch)
        try:
            assert session.terminal_live
            dot = _dot(app)
            assert dot.content.content.bgcolor == T.DONE
            assert dot.tooltip == "live"  # the actual lowercase status text
        finally:
            await app.set_props_and_fire("test-unmount", {}, "click")

    run(scenario())


def test_a_terminal_recovery_notice_is_the_dot_text_and_never_green(monkeypatch: Any) -> None:
    # The notice is the state before any automatic reconnect; that has its own tests.
    monkeypatch.setattr(ForegroundRecovery, "note", lambda self, entry: None)

    async def scenario() -> None:
        app, _controller, session = await _verified_root(monkeypatch)
        try:
            assert _dot(app).content.content.bgcolor == T.DONE
            session.controller.viewer_reopened()  # real recovery: fenced, notice set, not live
            await app.settle()
            assert not session.terminal_live
            dot = _dot(app)
            assert dot.tooltip == RECOVERY_NOTICE
            assert dot.content.content.bgcolor != T.DONE
        finally:
            await app.set_props_and_fire("test-unmount", {}, "click")

    run(scenario())


def test_a_connecting_terminal_reads_terminal_connecting_not_live(monkeypatch: Any) -> None:
    async def scenario() -> None:
        monkeypatch.setattr(AgentSession, "terminal_live", _verified(False))
        app, controller, session = await _open_live()
        try:
            controller.push(reducers.set_connection(controller.state, ConnectionState.LIVE))
            _terminal_state(monkeypatch, "connecting")
            await app.settle()
            assert _dot(app).tooltip == "Terminal connecting"
        finally:
            await app.set_props_and_fire("test-unmount", {}, "click")

    run(scenario())


def test_pages_without_an_open_terminal_show_no_dot_and_no_checking_text() -> None:
    async def scenario() -> None:
        app, controller, _sessions, _pool, _now = await _render(bridge=RouteBridge(width=390))
        try:
            controller.push(reducers.set_connection(controller.state, ConnectionState.LIVE))
            await app.settle()
            assert _missing(app, "terminal-connection-dot")
        finally:
            await app.set_props_and_fire("test-unmount", {}, "click")

    run(scenario())
