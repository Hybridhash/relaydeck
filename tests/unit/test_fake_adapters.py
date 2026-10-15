"""fake adapter behaviors."""

from __future__ import annotations

import os

import pytest

from relaydeck.devtools.fake_gateway import (
    CommandIntent,
    FakeGatewayAdapter,
)
from relaydeck.devtools.fake_host import (
    FakeHostAdapter,
    HostOfflineError,
)
from relaydeck.devtools.fake_terminal import (
    FakeTerminalStream,
    StreamDisconnectedError,
    TakeoverConflictError,
)
from relaydeck.devtools.scenarios import (
    build_scenario,
    describe_scenarios,
    fixture_selector_enabled,
)
from relaydeck.domain import reducers
from relaydeck.domain.models import (
    AgentStatus,
    AppState,
    CommandStatus,
    EventType,
)


def test_demo_snapshot_shapes() -> None:
    host = FakeHostAdapter()
    snap = host.snapshot()
    assert snap.host_id == "fake-host-1"
    statuses = {a.terminal_id: a.status for a in snap.agents}
    assert statuses == {
        "term-working": AgentStatus.WORKING,
        "term-blocked": AgentStatus.BLOCKED,
        "term-done": AgentStatus.DONE,
    }
    shells = [t for t in snap.tabs if t.label == "shell"]
    assert len(shells) == 2


def test_host_poll_and_offline() -> None:
    host = FakeHostAdapter()
    cursor, events = host.poll("")
    assert events == ()
    host.emit(EventType.AGENT_UPSERTED, "term-working", 11, {"status": "done"})
    cursor, events = host.poll(cursor)
    assert len(events) == 1 and cursor == "seq-1"
    cursor, events = host.poll(cursor)
    assert events == ()
    host.offline = True
    with pytest.raises(HostOfflineError):
        host.snapshot()
    with pytest.raises(HostOfflineError):
        host.poll(cursor)


def test_host_epoch_rotation_invalidates() -> None:
    host = FakeHostAdapter()
    state = reducers.apply_snapshot(AppState(), host.snapshot())
    host.rotate_epoch()
    event = host.emit(EventType.AGENT_UPSERTED, "term-working", 1, {})
    state, action = reducers.apply_event(state, event)
    assert action == "stale_epoch"
    assert state.needs_resync is True


def test_gateway_scripted_outcomes() -> None:
    gateway = FakeGatewayAdapter()
    gateway.script("cmd-ok", CommandStatus.DISPATCHED, CommandStatus.SUCCEEDED)
    gateway.script(
        "cmd-fail",
        CommandStatus.DISPATCHED,
        CommandStatus.FAILED,
        error_code="agent_blocked",
    )
    gateway.script("cmd-unknown", CommandStatus.UNKNOWN)
    for cid in ("cmd-ok", "cmd-fail", "cmd-unknown"):
        gateway.submit(CommandIntent(cid, "h1", "agent.prompt", {"agent_id": "term-1"}, cid))
    for _ in range(3):
        gateway.advance()
    ok = gateway.get("cmd-ok")
    assert ok is not None and ok.status == CommandStatus.SUCCEEDED
    failed = gateway.get("cmd-fail")
    assert failed is not None and failed.status == CommandStatus.FAILED
    assert failed.error_code == "agent_blocked"
    unknown = gateway.get("cmd-unknown")
    assert unknown is not None and unknown.status == CommandStatus.UNKNOWN
    # Unknown never resolves on its own.
    gateway.advance()
    unknown = gateway.get("cmd-unknown")
    assert unknown is not None and unknown.status == CommandStatus.UNKNOWN


def test_gateway_capability_check() -> None:
    gateway = FakeGatewayAdapter()
    assert gateway.check("agent.prompt", "agents.prompt") is None
    gateway.revoke_capability("agents.prompt")
    assert gateway.check("agent.prompt", "agents.prompt") is not None


def test_terminal_stream_ownership_and_generations() -> None:
    stream = FakeTerminalStream(terminal_id="t1")
    stream.load(b"hello")
    stream.attach("me", 1)
    assert stream.read(2) == b"he"
    stream.write_input(1, b"x")
    with pytest.raises(TakeoverConflictError):
        stream.attach("other", 2)
    previous = stream.take_over("other", 2)
    assert previous == "me"
    with pytest.raises(ValueError, match="stale writer"):
        stream.write_input(1, b"late")
    stream.write_input(2, b"y")
    assert stream.input_log == [(1, b"x"), (2, b"y")]


def test_terminal_stream_slow_stall_disconnect() -> None:
    stream = FakeTerminalStream(terminal_id="t1", chunk_size=2)
    stream.load(b"abcd")
    assert stream.read() == b"ab"
    stream.stalled = True
    assert stream.read() == b""
    stream.stalled = False
    assert stream.read() == b"cd"
    assert stream.read() == b""
    stream.disconnected = True
    with pytest.raises(StreamDisconnectedError):
        stream.read()


def test_scenarios_build() -> None:
    assert build_scenario("steady").host.snapshot().host_id == "fake-host-1"
    world = build_scenario("takeover")
    with pytest.raises(TakeoverConflictError):
        world.terminal("term-working").attach("me", 2)
    world = build_scenario("slow-output")
    assert world.terminal("term-working").read() == b"x" * 64
    world = build_scenario("duplicates")
    _, events = world.host.poll("")
    assert len(events) == 2
    with pytest.raises(HostOfflineError):
        build_scenario("host-offline").host.snapshot()
    world = build_scenario("unknown-command")
    world.gateway.submit(CommandIntent("cmd-unknown", "h", "k", {"agent_id": "t"}))
    world.gateway.advance()
    unknown = world.gateway.get("cmd-unknown")
    assert unknown is not None and unknown.status == CommandStatus.UNKNOWN
    with pytest.raises(ValueError):
        build_scenario("nope")


def test_fixture_gate() -> None:
    old_f, old_p = os.environ.get("RELAYDECK_FIXTURES"), os.environ.get("RELAYDECK_PRODUCTION")
    try:
        os.environ.pop("RELAYDECK_FIXTURES", None)
        os.environ.pop("RELAYDECK_PRODUCTION", None)
        assert fixture_selector_enabled() is False
        assert describe_scenarios() == ()
        os.environ["RELAYDECK_FIXTURES"] = "1"
        assert fixture_selector_enabled() is True
        assert len(describe_scenarios()) == 6
        os.environ["RELAYDECK_PRODUCTION"] = "1"
        assert fixture_selector_enabled() is False
    finally:
        if old_f is None:
            os.environ.pop("RELAYDECK_FIXTURES", None)
        else:
            os.environ["RELAYDECK_FIXTURES"] = old_f
        if old_p is None:
            os.environ.pop("RELAYDECK_PRODUCTION", None)
        else:
            os.environ["RELAYDECK_PRODUCTION"] = old_p
