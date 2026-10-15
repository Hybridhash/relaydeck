"""Herdr adapter and attach sessions.

Contract coverage for protocol framing, version handshake, typed
reads/actions and error mapping. Bootstrap convergence lives in
tests/integration/test_source_bootstrap.py."""

from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from relaydeck.connector.attach import AttachError, AttachSession, attach_argv
from relaydeck.connector.herdr import (
    HerdrAdapter,
    HerdrError,
    HerdrProtocolError,
    parse_event,
    resolve_socket,
)
from tests.support.gateway.herdr_adapter import _FakeReader, _FakeWriter


def _adapter(replies: list[bytes]) -> tuple[HerdrAdapter, _FakeWriter]:
    writer = _FakeWriter()
    reader = _FakeReader(replies)

    async def opener() -> tuple[_FakeReader, _FakeWriter]:
        return reader, writer

    return HerdrAdapter(opener=opener), writer


def _ok(rid: str, result: dict[str, Any]) -> bytes:
    return (json.dumps({"id": rid, "result": result}) + "\n").encode()


def _err(rid: str, code: str, message: str = "m") -> bytes:
    return (json.dumps({"id": rid, "error": {"code": code, "message": message}}) + "\n").encode()


def test_request_framing_and_reply() -> None:
    async def scenario() -> None:
        adapter, writer = _adapter([_ok("rd-1", {"type": "pong", "protocol": 22})])
        result = await adapter.call("ping", {})
        assert result.id == "rd-1"
        sent = json.loads(writer.written.decode())
        assert sent["method"] == "ping"
        assert sent["params"] == {}
        assert sent["id"] == "rd-1"
        assert writer.written.endswith(b"\n")
        assert writer.closed is True

    asyncio.run(scenario())


def test_error_mapping_and_bad_envelopes() -> None:
    async def scenario() -> None:
        adapter, _ = _adapter([_err("rd-1", "agent_blocked", "wait")])
        with pytest.raises(HerdrError) as exc:
            await adapter.call("agent.prompt", {"target": "t", "text": "x"})
        assert exc.value.code == "agent_blocked"

        adapter, _ = _adapter([b"not json\n"])
        with pytest.raises(HerdrProtocolError):
            await adapter.call("ping", {})

        adapter, _ = _adapter([_ok("other-id", {"type": "pong"})])
        with pytest.raises(HerdrProtocolError):
            await adapter.call("ping", {})

        adapter, _ = _adapter([b'{"id": "rd-1"}\n'])
        with pytest.raises(HerdrProtocolError):
            await adapter.call("ping", {})

        adapter, _ = _adapter([b""])
        with pytest.raises(HerdrProtocolError):
            await adapter.call("ping", {})

    asyncio.run(scenario())


def test_parse_event_lines() -> None:
    name, data = parse_event(b'{"event": "pane.exited", "data": {"pane_id": "p"}}\n')
    assert (name, data) == ("pane.exited", {"pane_id": "p"})
    with pytest.raises(HerdrProtocolError):
        parse_event(b'{"id": "x", "event": "pane.exited", "data": {}}\n')
    with pytest.raises(HerdrProtocolError):
        parse_event(b'{"nope": true}\n')


def test_ping_checks_protocol() -> None:
    async def scenario() -> None:
        adapter, _ = _adapter([_ok("rd-1", {"type": "pong", "protocol": 22})])
        assert (await adapter.ping())["protocol"] == 22
        adapter, _ = _adapter([_ok("rd-1", {"type": "pong", "protocol": 99})])
        with pytest.raises(HerdrProtocolError):
            await adapter.ping()

    asyncio.run(scenario())


def test_method_param_shapes() -> None:
    async def scenario() -> None:
        cases: list[tuple[str, dict[str, Any], Any]] = [
            ("session.snapshot", {}, lambda a: a.session_snapshot()),
            ("agent.list", {}, lambda a: a.agent_list()),
            ("agent.get", {"target": "t"}, lambda a: a.agent_get("t")),
            (
                "agent.prompt",
                {"target": "t", "text": "hi"},
                lambda a: a.agent_prompt("t", "hi"),
            ),
            (
                "agent.start",
                {"name": "n", "kind": "tool-a", "pane_id": "p"},
                lambda a: a.agent_start("n", "tool-a", "p"),
            ),
            ("pane.send_text", {"pane_id": "p", "text": "x"}, lambda a: a.pane_send_text("p", "x")),
            (
                "pane.send_keys",
                {"pane_id": "p", "keys": ["Enter"]},
                lambda a: a.pane_send_keys("p", ["Enter"]),
            ),
            (
                "pane.send_input",
                {"pane_id": "p", "text": "x"},
                lambda a: a.pane_send_input("p", text="x"),
            ),
            (
                "agent.send_keys",
                {"target": "t", "keys": ["ctrl", "c"]},
                lambda a: a.agent_send_keys("t", ["ctrl", "c"]),
            ),
            (
                "agent.read",
                {
                    "target": "t",
                    "source": "recent",
                    "lines": 50,
                    "format": "text",
                    "strip_ansi": True,
                },
                lambda a: a.agent_read("t", "recent", lines=50),
            ),
            (
                "pane.read",
                {
                    "pane_id": "p",
                    "source": "visible",
                    "lines": 200,
                    "format": "ansi",
                    "strip_ansi": False,
                },
                lambda a: a.pane_read("p", "visible", format="ansi", strip_ansi=False),
            ),
            ("tab.create", {"workspace_id": "w"}, lambda a: a.tab_create("w")),
            ("workspace.create", {}, lambda a: a.workspace_create()),
            (
                "workspace.create",
                {"cwd": "/synthetic/project", "label": "project"},
                lambda a: a.workspace_create(cwd="/synthetic/project", label="project"),
            ),
            ("tab.rename", {"tab_id": "t", "label": "shell"}, lambda a: a.tab_rename("t", "shell")),
            ("pane.close", {"pane_id": "p"}, lambda a: a.pane_close("p")),
            ("tab.close", {"tab_id": "t"}, lambda a: a.tab_close("t")),
            ("workspace.close", {"workspace_id": "w"}, lambda a: a.workspace_close("w")),
        ]
        for index, (method, params, invoke) in enumerate(cases, start=1):
            result_payload = (
                {"type": "ok"} if method != "agent.list" else {"type": "agent_list", "agents": []}
            )
            adapter, writer = _adapter([_ok(f"rd-{index}", result_payload)])
            adapter._ids = index - 1
            await invoke(adapter)
            sent = json.loads(writer.written.decode())
            assert sent["method"] == method, method
            assert sent["params"] == params, method

    asyncio.run(scenario())


def test_subscribe_streams_events() -> None:
    async def scenario() -> None:
        replies = [
            _ok("sub-1", {"type": "subscription_started"}),
            b'{"event": "workspace.updated", "data": {"workspace_id": "w"}}\n',
            b'{"event": "pane.exited", "data": {"pane_id": "p"}}\n',
        ]
        adapter, writer = _adapter(replies)
        seen = []
        async for name, data in adapter.subscribe([{"type": "workspace.updated"}]):
            seen.append((name, data))
        assert seen == [
            ("workspace.updated", {"workspace_id": "w"}),
            ("pane.exited", {"pane_id": "p"}),
        ]
        sent = json.loads(writer.written.decode())
        assert sent["method"] == "events.subscribe"

    asyncio.run(scenario())


def test_subscribe_bad_ack_rejected() -> None:
    async def scenario() -> None:
        adapter, _ = _adapter([_err("sub-1", "pane_not_found")])
        with pytest.raises(HerdrError):
            async for _ in adapter.subscribe([{"type": "x", "pane_id": "p"}]):
                pass

    asyncio.run(scenario())


def test_resolve_socket() -> None:
    assert resolve_socket("/tmp/x.sock") == "/tmp/x.sock"
    assert resolve_socket("~/.config/herdr/herdr.sock") == os.path.expanduser(
        "~/.config/herdr/herdr.sock"
    )
    with pytest.raises(HerdrProtocolError):
        resolve_socket("~/a/~/b")


def test_attach_argv() -> None:
    assert attach_argv("herdr", target="w1:p1") == ["herdr", "agent", "attach", "w1:p1"]
    assert attach_argv("herdr", target="w1:p1", takeover=True) == [
        "herdr",
        "agent",
        "attach",
        "w1:p1",
        "--takeover",
    ]
    assert attach_argv("herdr", terminal_id="term-1") == [
        "herdr",
        "terminal",
        "attach",
        "term-1",
    ]
    with pytest.raises(AttachError):
        attach_argv("herdr")
    with pytest.raises(AttachError):
        attach_argv("herdr", target="a", terminal_id="b")


_STUB = """#!{python}
import sys, os
sys.stdout.write("stub-ready\\r\\n")
sys.stdout.flush()
# echo stdin lines back until EOF
while True:
    data = sys.stdin.readline()
    if not data:
        break
    sys.stdout.write("echo:" + data)
    sys.stdout.flush()
"""


def _stub_binary(tmp_path: Path) -> str:
    path = tmp_path / "stub-herdr"
    path.write_text(_STUB.format(python=sys.executable))
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


def _read_until(session: AttachSession, marker: bytes, timeout: float = 5.0) -> bytes:
    """Accumulate single-chunk reads until marker or deadline.

    The PTY echo can arrive as its own chunk before the stub reply, so
    one read() is not a complete assertion unit.
    """

    deadline = time.monotonic() + timeout
    chunks = bytearray()
    while time.monotonic() < deadline:
        chunks += session.read(timeout=max(0.1, deadline - time.monotonic()))
        if marker in chunks:
            break
    return bytes(chunks)


def test_attach_session_round_trip(tmp_path: Path) -> None:
    binary = _stub_binary(tmp_path)
    session = AttachSession(argv=[binary], cols=80, rows=24)
    try:
        session.spawn()
        assert session.running is True
        assert b"stub-ready" in _read_until(session, b"stub-ready")
        session.resize(100, 30)
        assert (session.cols, session.rows) == (100, 30)
        session.write(b"hello\n")
        assert b"echo:hello" in _read_until(session, b"echo:hello")
    finally:
        status = session.close()
    assert status is not None
    assert session.running is False


def test_attach_close_idempotent_without_spawn() -> None:
    session = AttachSession(argv=["true"])
    assert session.close() is None


@pytest.mark.skipif(
    os.environ.get("RELAYDECK_LIVE") != "1",
    reason="live Herdr requires RELAYDECK_LIVE=1",
)
def test_live_ping_and_list_shapes() -> None:
    async def scenario() -> None:
        adapter = HerdrAdapter()
        pong = await adapter.ping()
        assert pong["protocol"] == 22
        agents = await adapter.agent_list()
        assert isinstance(agents, list)

    asyncio.run(scenario())


def test_method_allowlist_and_param_guards() -> None:
    async def scenario() -> None:
        adapter, writer = _adapter([])
        with pytest.raises(HerdrProtocolError):
            await adapter.call("agent.approve", {})
        assert writer.written == bytearray()
        with pytest.raises(HerdrProtocolError):
            await adapter.agent_read("t", "bogus")
        with pytest.raises(HerdrProtocolError):
            await adapter.pane_send_input("p")
        with pytest.raises(HerdrProtocolError):
            await adapter.agent_start("n", "tool-a", "p", timeout_ms=100)

    asyncio.run(scenario())
