"""production Herdr source against a scripted Unix-socket daemon.

Handshake order, protocol gate, bootstrap buffering, snapshot/event
translation (golden upstream shapes -> gateway documents), dispatch
wire params, typed terminal relay, and link end-to-end over the
`LinkSource` port. The live user daemon is never contacted: every
Herdr byte here comes from the scripted daemon below.
"""

from __future__ import annotations

import asyncio
import base64
import itertools
import json
import os
import stat
import sys
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from relaydeck.connector.attach import AttachSession
from relaydeck.connector.fixture import FixtureAdapter
from relaydeck.connector.herdr import HerdrAdapter, HerdrProtocolError
from relaydeck.connector.journal import ConnectorDispatch
from relaydeck.connector.link import ConnectorLink, LinkConfig, TunnelTransport
from relaydeck.connector.source import (
    HerdrSource,
    TerminalRelay,
    translate_event,
    translate_snapshot,
)
from relaydeck.contracts.schemas import GATEWAY_PROTOCOL, validate
from relaydeck.gateway.app import GatewayState, create_app
from relaydeck.gateway.backend import prepare_event
from relaydeck.gateway.production import JournalBackend
from relaydeck.journal import CommandJournal, digest_for
from tests.support.agent_kinds import allow_kinds
from tests.support.live_server import LiveGateway

UPSTREAM = Path(__file__).resolve().parent.parent / "fixtures" / "upstream"
NOW = 1_700_000_000_000
SECRET = b"herdr-source-test-secret-32bytes!!"
ALL_CAPS = frozenset(
    {
        "sessions.read",
        "terminal.view",
        "terminal.control",
        "agents.prompt",
        "agents.start",
        "shells.create",
        "terminal.attach",
        "hosts.enroll",
    }
)


_sock_counter = itertools.count()


def _sock_path() -> Path:
    # macOS AF_UNIX paths cap at 104 chars: pytest tmp_path is too deep.
    return Path(f"/tmp/rd-src-{os.getpid()}-{next(_sock_counter)}.sock")


def _line(doc: dict[str, Any]) -> bytes:
    return (json.dumps(doc, separators=(",", ":")) + "\n").encode()


def _golden(name: str) -> dict[str, Any]:
    doc: dict[str, Any] = json.loads((UPSTREAM / f"{name}.json").read_text())
    return doc


class _ScriptDaemon:
    """Scripted Herdr daemon: golden replies, recorded requests."""

    def __init__(
        self,
        *,
        protocol: int = 22,
        sub_events: list[bytes] | None = None,
        snapshot_delay: float = 0.0,
    ) -> None:
        self.protocol = protocol
        self.sub_events = sub_events or []
        self.snapshot_delay = snapshot_delay
        self.snapshot_inner: dict[str, Any] = dict(
            _golden("session_snapshot")["result"]["snapshot"]
        )
        self.agent_rows: list[dict[str, Any]] = list(_golden("agent_list")["result"]["agents"])
        self.methods: list[str] = []
        self.params: list[dict[str, Any]] = []
        self._ids = 0
        self._server: asyncio.AbstractServer | None = None
        self._sock: Path | None = None

    async def start(self, sock: Path) -> _ScriptDaemon:
        self._server = await asyncio.start_unix_server(self._handle, str(sock))
        self._sock = sock
        return self

    async def stop(self) -> None:
        assert self._server is not None
        self._server.close()
        await self._server.wait_closed()
        if self._sock is not None:
            try:
                self._sock.unlink(missing_ok=True)
            except OSError:
                pass

    def _next(self, prefix: str) -> str:
        self._ids += 1
        return f"{prefix}-{self._ids}"

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            raw = await reader.readline()
            if not raw:
                return
            doc = json.loads(raw)
            method = str(doc.get("method", ""))
            rid = str(doc.get("id", ""))
            params = doc.get("params", {})
            self.methods.append(method)
            self.params.append(params if isinstance(params, dict) else {})
            if method == "ping":
                writer.write(_line({"id": rid, "result": {"protocol": self.protocol}}))
                await writer.drain()
                return
            if method == "session.snapshot":
                if self.snapshot_delay:
                    await asyncio.sleep(self.snapshot_delay)
                writer.write(_line({"id": rid, "result": {"snapshot": self.snapshot_inner}}))
                await writer.drain()
                return
            if method == "agent.list":
                writer.write(_line({"id": rid, "result": {"agents": self.agent_rows}}))
                await writer.drain()
                return
            if method == "events.subscribe":
                writer.write(_line({"id": rid, "result": {"type": "subscription_started"}}))
                await writer.drain()
                for event in self.sub_events:
                    writer.write(event)
                    await writer.drain()
                while True:
                    chunk = await reader.read(1024)
                    if not chunk:
                        return
                return
            if method == "agent.prompt":
                target = str(params.get("target", ""))
                writer.write(
                    _line(
                        {
                            "id": rid,
                            "result": {
                                "type": "agent_prompted",
                                "agent": {
                                    "terminal_id": target,
                                    "agent_status": "working",
                                    "workspace_id": "w1",
                                    "tab_id": "w1:t1",
                                    "pane_id": "w1:p1",
                                    "focused": False,
                                    "revision": 44,
                                },
                            },
                        }
                    )
                )
                await writer.drain()
                return
            if method == "tab.create":
                tab_id, pane_id = self._next("tab"), self._next("pane")
                writer.write(
                    _line(
                        {
                            "id": rid,
                            "result": {
                                "type": "tab_created",
                                "tab": {
                                    "tab_id": tab_id,
                                    "workspace_id": str(params.get("workspace_id", "")),
                                    "number": 1,
                                    "label": str(params.get("label", "")),
                                    "focused": False,
                                    "pane_count": 1,
                                    "agent_status": "unknown",
                                },
                                "root_pane": {
                                    "pane_id": pane_id,
                                    "terminal_id": pane_id,
                                    "workspace_id": str(params.get("workspace_id", "")),
                                    "tab_id": tab_id,
                                    "focused": False,
                                    "agent_status": "unknown",
                                    "revision": 0,
                                },
                            },
                        }
                    )
                )
                await writer.drain()
                return
            if method == "agent.start":
                writer.write(
                    _line(
                        {
                            "id": rid,
                            "result": {
                                "type": "agent_started",
                                "agent": {
                                    "terminal_id": self._next("term"),
                                    "agent_status": "working",
                                    "workspace_id": "w1",
                                    "tab_id": "w1:t1",
                                    "pane_id": str(params.get("pane_id", "")),
                                    "focused": False,
                                    "revision": 1,
                                },
                                "argv": list(params.get("args", [])),
                            },
                        }
                    )
                )
                await writer.drain()
                return
            if method == "pane.close":
                writer.write(_line({"id": rid, "result": {"type": "ok"}}))
                await writer.drain()
                return
            writer.write(_line({"id": rid, "error": {"code": "unknown_method", "message": method}}))
            await writer.drain()
        finally:
            writer.close()


def _dispatch_msg(
    command_id: str, kind: str, target: dict[str, Any], payload: dict[str, Any]
) -> dict[str, Any]:
    return {
        "kind": "command.dispatch",
        "command_id": command_id,
        "principal": "acct-1",
        "host_id": "home-mac",
        "gateway_kind": kind,
        "target": target,
        "payload": payload,
        "digest": digest_for("home-mac", kind, target, payload),
        "generation": 0,
        "expires_ms": 0,
    }


def test_bootstrap_handshake_order_and_buffering(tmp_path: Path) -> None:
    async def scenario() -> None:
        sock = _sock_path()
        daemon = await _ScriptDaemon(
            snapshot_delay=0.2,
            sub_events=[
                _line(_golden("event_agent_status")),
                _line(_golden("event_workspace_updated")),
            ],
        ).start(sock)
        try:
            source = HerdrSource(
                HerdrAdapter(socket_path=str(sock)), clock=lambda: NOW, boot_tag="tag1"
            )
            try:
                data = await source.bootstrap()
            finally:
                await source.close()
            assert daemon.methods[0] == "ping"
            assert daemon.methods[1] == "events.subscribe"
            assert data.epoch == "herdr-22-1-tag1"
            assert source.epoch == "herdr-22-1-tag1"
            assert data.buffered_total == 2
            assert data.skipped_events == 0
            kinds = [(seed["type"], seed.get("resource_id")) for seed in data.seed_events]
            assert kinds == [("pane.upserted", "w1:p1"), ("workspace.upserted", "w1")]
            assert data.seed_events[0]["payload"] == {"agent_status": "blocked"}
            assert data.seed_events[0]["event_id"] == "herdr-1-1"
            assert data.seed_events[1]["event_id"] == "herdr-1-2"
        finally:
            await daemon.stop()

    asyncio.run(scenario())


def test_bootstrap_protocol_drift_fails_closed(tmp_path: Path) -> None:
    async def scenario() -> None:
        sock = _sock_path()
        daemon = await _ScriptDaemon(protocol=21).start(sock)
        try:
            source = HerdrSource(HerdrAdapter(socket_path=str(sock)), clock=lambda: NOW)
            try:
                with pytest.raises(HerdrProtocolError):
                    await source.bootstrap()
                assert source.bootstrapped is False
                with pytest.raises(HerdrProtocolError):
                    source.snapshot_document("home-mac")
            finally:
                await source.close()
        finally:
            await daemon.stop()

    asyncio.run(scenario())


def test_snapshot_translation_validates_and_merges(tmp_path: Path) -> None:
    async def scenario() -> None:
        sock = _sock_path()
        daemon = await _ScriptDaemon().start(sock)
        try:
            source = HerdrSource(HerdrAdapter(socket_path=str(sock)), clock=lambda: NOW)
            try:
                await source.bootstrap()
                doc = source.snapshot_document("home-mac")
            finally:
                await source.close()
        finally:
            await daemon.stop()
        validate("relaydeck/gateway/v1/snapshot", doc)
        assert doc["host"]["protocol"] == 22
        assert doc["host"]["version"] == "0.9.1"
        assert doc["host"]["epoch"] == source.epoch
        assert doc["workspaces"][0]["id"] == "w1"
        assert doc["workspaces"][0]["label"] == "demo-project"
        assert doc["tabs"][0]["id"] == "w1:t1"
        assert doc["panes"][0]["id"] == "w1:p1"
        by_terminal = {agent["terminal_id"]: agent for agent in doc["agents"]}
        # agent.list wins (fresher): blocked/revision 43, not working/42.
        assert by_terminal["term_demo1"]["status"] == "blocked"
        assert by_terminal["term_demo1"]["revision"] == 43
        assert by_terminal["term_demo1"]["title"] == "demo task | demo-project"
        assert by_terminal["term_demo2"]["status"] == "done"

    asyncio.run(scenario())


def test_translate_snapshot_rejects_drift_and_skips_bad_rows() -> None:
    session = dict(_golden("session_snapshot")["result"]["snapshot"])
    agents = list(_golden("agent_list")["result"]["agents"])
    drifted = dict(session, protocol=21)
    with pytest.raises(ValueError, match="drift"):
        translate_snapshot("home-mac", "e1", drifted, agents)
    with pytest.raises(ValueError):
        translate_snapshot("home-mac", "e1", {"protocol": 22}, agents)
    messy = dict(session)
    messy["workspaces"] = [{"workspace_id": "w1"}, {"nope": 1}, "junk"]
    messy["tabs"] = [{"tab_id": "t1", "workspace_id": "w1"}]
    messy["panes"] = [{"pane_id": "p1", "workspace_id": "w1", "tab_id": "t1"}]
    messy["agents"] = [{"terminal_id": "a1", "workspace_id": "w1", "tab_id": "t1"}, {"nope": 1}]
    doc = translate_snapshot("home-mac", "e1", messy, [])
    validate("relaydeck/gateway/v1/snapshot", doc)
    assert [workspace["id"] for workspace in doc["workspaces"]] == ["w1"]
    assert doc["agents"] == []


def test_event_translation_matrix() -> None:
    status = translate_event(
        "pane.agent_status_changed",
        dict(_golden("event_agent_status")["data"]),
        epoch="e9",
        event_id="ev-1",
        observed_at="2026-10-04T00:00:00Z",
    )
    assert status is not None
    assert (status["type"], status["resource_id"]) == ("pane.upserted", "w1:p1")
    assert status["host_epoch"] == "e9"
    assert status["payload"] == {"agent_status": "blocked"}
    # Gateway-side acceptance: the partial stamps into a valid envelope.
    stored = prepare_event("home-mac", status, 1, b"s" * 32, NOW)
    assert stored["sequence"] == 1
    assert stored["cursor"]

    closed = translate_event(
        "tab.closed", {"tab_id": "w1:t1"}, epoch="e", event_id="e", observed_at="t"
    )
    assert closed == {
        "event_id": "e",
        "host_epoch": "e",
        "type": "resource.removed",
        "resource_id": "w1:t1",
        "observed_at": "t",
    }
    # Unmapped or identity-missing events are skipped, never invented.
    matched = translate_event(
        "pane.output_matched", {"pane_id": "p"}, epoch="e", event_id="e", observed_at="t"
    )
    assert matched is None
    assert translate_event("layout.updated", {}, epoch="e", event_id="e", observed_at="t") is None
    assert translate_event("bogus.event", {}, epoch="e", event_id="e", observed_at="t") is None
    assert translate_event("pane.closed", {}, epoch="e", event_id="e", observed_at="t") is None


def test_dispatch_through_source_records_wire_params(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    allow_kinds(tmp_path, monkeypatch, "agent-a")

    async def scenario() -> None:
        sock = _sock_path()
        daemon = await _ScriptDaemon().start(sock)
        try:
            source = HerdrSource(
                HerdrAdapter(socket_path=str(sock)),
                clock=lambda: NOW,
                agent_kinds=("agent-a",),
            )
            try:
                journal = CommandJournal(tmp_path / "c.db", clock=lambda: NOW)
                try:
                    dispatcher = ConnectorDispatch(
                        journal, source.dispatch_adapter, host_id="home-mac", clock=lambda: NOW
                    )
                    shell = await dispatcher.dispatch(
                        _dispatch_msg(
                            "hs-1", "shell.create", {"workspace_id": "w1"}, {"label": "ops"}
                        )
                    )
                    assert shell["status"] == "succeeded"
                    assert shell["result"]["type"] == "tab_created"
                    started = await dispatcher.dispatch(
                        _dispatch_msg(
                            "hs-2",
                            "agent.start",
                            {"workspace_id": "w1"},
                            {"kind": "agent-a", "name": "web", "argv": ["--x"]},
                        )
                    )
                    assert started["status"] == "succeeded"
                    # identity from terminal_id (no "id" key upstream).
                    assert started["result"]["agent_id"].startswith("term-")
                    assert started["result"]["agent_id"] != ""
                    prompted = await dispatcher.dispatch(
                        _dispatch_msg(
                            "hs-3",
                            "agent.prompt",
                            {"agent_id": started["result"]["agent_id"]},
                            {"text": "hi"},
                        )
                    )
                    assert prompted["status"] == "succeeded"
                    closed = await dispatcher.dispatch(
                        _dispatch_msg("hs-4", "session.close", {"terminal_id": "w1:p9"}, {})
                    )
                    assert closed["status"] == "succeeded"
                finally:
                    journal.close()
            finally:
                await source.close()
        finally:
            await daemon.stop()
        seen = dict(zip(daemon.methods, daemon.params, strict=False))
        creates = [
            params
            for method, params in zip(daemon.methods, daemon.params, strict=True)
            if method == "tab.create"
        ]
        assert len(creates) == 2
        assert creates[0] == {"workspace_id": "w1", "label": "ops"}
        # The requested display name is persisted on the agent's tab at creation.
        assert creates[1] == {"workspace_id": "w1", "label": "web"}
        assert seen["agent.start"]["pane_id"].startswith("pane-")
        assert seen["agent.start"]["args"] == ["--x"]
        assert seen["agent.prompt"] == {"target": started["result"]["agent_id"], "text": "hi"}
        assert seen["pane.close"] == {"pane_id": "w1:p9"}

    asyncio.run(scenario())


def test_refresh_updates_without_new_epoch(tmp_path: Path) -> None:
    async def scenario() -> None:
        sock = _sock_path()
        daemon = await _ScriptDaemon().start(sock)
        try:
            source = HerdrSource(HerdrAdapter(socket_path=str(sock)), clock=lambda: NOW)
            try:
                first = await source.bootstrap()
                daemon.snapshot_inner["workspaces"] = [
                    dict(
                        daemon.snapshot_inner["workspaces"][0],
                        label="renamed-project",
                    )
                ]
                await source.refresh()
                doc = source.snapshot_document("home-mac")
            finally:
                await source.close()
        finally:
            await daemon.stop()
        assert doc["workspaces"][0]["label"] == "renamed-project"
        assert doc["host"]["epoch"] == first.epoch

    asyncio.run(scenario())


def test_relay_frames_chunk_encode_and_drain() -> None:
    relay = TerminalRelay()
    assert relay.script("term-1", "ring-e1", b"x" * (64 * 1024 * 2 + 7)) == 3
    assert len(relay) == 3
    frames = relay.drain()
    assert [len(frame.data) for frame in frames] == [65536, 65536, 7]
    assert relay.drain() == []
    doc = TerminalRelay.encode_tunnel("home-mac", frames[0])
    assert doc["kind"] == "terminal.output"
    assert (doc["terminal_id"], doc["epoch"]) == ("term-1", "ring-e1")
    assert base64.b64decode(doc["data_b64"]) == b"x" * 65536
    with pytest.raises(ValueError):
        relay.script("", "e", b"x")
    with pytest.raises(ValueError):
        relay.script("t", "e", b"")


_STUB = """#!{python}
import sys
sys.stdout.write("stub-ready\\r\\n")
sys.stdout.flush()
while True:
    data = sys.stdin.readline()
    if not data:
        break
    sys.stdout.write("echo:" + data)
    sys.stdout.flush()
"""


def test_pump_collects_stub_pty_bytes(tmp_path: Path) -> None:
    stub = tmp_path / "stub-herdr"
    stub.write_text(_STUB.format(python=sys.executable))
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    source = HerdrSource(HerdrAdapter(socket_path=str(tmp_path / "unused.sock")), binary=str(stub))
    try:
        session = source.attach_shell("term-9")
        assert isinstance(session, AttachSession)
        session.write(b"hello\n")
        deadline = time.monotonic() + 10.0
        collected = 0
        received = bytearray()
        typed_ok = True
        while time.monotonic() < deadline:
            collected += source.pump_attached(lambda _terminal: "ring-e9", timeout=0.5)
            for terminal, epoch, data in source.drain_outputs():
                typed_ok = typed_ok and terminal == "term-9" and epoch == "ring-e9"
                received += data
            if b"echo:hello" in received:
                break
        assert collected > 0
        assert b"echo:hello" in received, "pump collected no stub bytes in 10s"
        assert typed_ok
    finally:
        source._pool.close_all()


def test_link_with_herdr_source_end_to_end(tmp_path: Path) -> None:
    async def scenario() -> None:
        sock = _sock_path()
        daemon = await _ScriptDaemon().start(sock)
        journal = CommandJournal(tmp_path / "gw.db", clock=lambda: NOW)
        backend = JournalBackend(journal, now_ms=lambda: NOW)
        state = GatewayState(backend=backend, cursor_secret=SECRET, now_ms=lambda: NOW)
        source = HerdrSource(
            HerdrAdapter(socket_path=str(sock)), clock=lambda: NOW, boot_tag="live1"
        )
        try:
            bootstrapped = await source.bootstrap()
            with LiveGateway(create_app(state)) as live:
                access, _ = state.sessions.create("acct-1", ALL_CAPS, frozenset({"home-mac"}), NOW)
                host_token = state.hosts.enroll("home-mac", "Home Mac", "acct-1")
                link = ConnectorLink(
                    LinkConfig(
                        gateway_url=live.base_url,
                        host_id="home-mac",
                        host_token=host_token,
                        journal_path=str(tmp_path / "c.db"),
                        events=tuple(source.seed_events()),
                        clock=lambda: NOW,
                    ),
                    source,
                )
                stop = asyncio.Event()
                task = asyncio.create_task(link.run(stop))
                try:
                    async with httpx.AsyncClient(timeout=10.0) as client:
                        headers = {"Authorization": f"Bearer {access}"}

                        async def pushed() -> bool:
                            response = await client.get(
                                f"{live.base_url}/v1/hosts/home-mac/snapshot", headers=headers
                            )
                            body = response.json()
                            return bool(
                                response.status_code == 200
                                and body["host"]["epoch"] == bootstrapped.epoch
                            )

                        deadline = time.monotonic() + 15.0
                        while time.monotonic() < deadline:
                            if await pushed():
                                break
                            await asyncio.sleep(0.05)
                        else:
                            raise AssertionError("translated snapshot never pushed")
                        from datetime import UTC, datetime

                        expires = datetime.fromtimestamp((NOW + 120_000) / 1000, UTC).strftime(
                            "%Y-%m-%dT%H:%M:%SZ"
                        )
                        submitted = await client.post(
                            f"{live.base_url}/v1/hosts/home-mac/commands",
                            headers=headers,
                            json={
                                "protocol": GATEWAY_PROTOCOL,
                                "command_id": "hs-link-1",
                                "host_id": "home-mac",
                                "host_epoch": bootstrapped.epoch,
                                "kind": "shell.create",
                                "target": {"workspace_id": "w1"},
                                "payload": {"label": "prod"},
                            }
                            | ({"expires_at": expires}),
                        )
                        assert submitted.status_code == 202, submitted.text

                        async def succeeded() -> bool:
                            response = await client.get(
                                f"{live.base_url}/v1/hosts/home-mac/commands/hs-link-1",
                                headers=headers,
                            )
                            return bool(response.json()["status"] == "succeeded")

                        deadline = time.monotonic() + 15.0
                        while time.monotonic() < deadline:
                            if await succeeded():
                                break
                            await asyncio.sleep(0.05)
                        else:
                            raise AssertionError("dispatch never succeeded")
                finally:
                    stop.set()
                    outcome = await asyncio.wait_for(task, timeout=15.0)
                assert outcome.reason == "shutdown"
                assert outcome.results == 1
                assert "tab.create" in daemon.methods
                params = daemon.params[daemon.methods.index("tab.create")]
                assert params["label"] == "prod"
        finally:
            await source.close()
            await daemon.stop()
            backend.close()

    asyncio.run(scenario())


class _HelloOnce:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_text(self, text: str) -> None:
        self.sent.append(text)

    async def recv_text(self) -> str:
        return json.dumps({"kind": "hello", "host_id": "home-mac"})

    async def close(self) -> None:
        pass


def test_link_source_failed_on_unbootstrapped(tmp_path: Path) -> None:
    async def scenario() -> None:
        script = _HelloOnce()

        async def opener() -> TunnelTransport:
            return script

        source = HerdrSource(HerdrAdapter(socket_path=str(tmp_path / "unused.sock")))
        link = ConnectorLink(
            LinkConfig(
                gateway_url="http://127.0.0.1:9",
                host_id="home-mac",
                host_token="rdh_dead",
                journal_path=str(tmp_path / "c.db"),
                clock=lambda: NOW,
            ),
            source,
            opener=opener,
        )
        outcome = await asyncio.wait_for(link.run(asyncio.Event()), timeout=15.0)
        assert outcome.reason == "source-failed"
        assert outcome.sessions == 1
        assert outcome.pushed_snapshot is False
        assert script.sent == []

    asyncio.run(scenario())


def test_fixture_and_herdr_share_the_link_port(tmp_path: Path) -> None:
    fixture = FixtureAdapter()
    assert fixture.snapshot_document("home-mac")["host"]["epoch"] == "epoch-fixture-1"
    assert fixture.drain_outputs() == []
    fixture.script_output("term-1", "e1", b"bytes")
    assert fixture.drain_outputs() == [("term-1", "e1", b"bytes")]
    assert fixture.drain_outputs() == []
    assert fixture.dispatch_adapter is fixture
    source = HerdrSource(HerdrAdapter(socket_path=str(tmp_path / "unused.sock")))
    assert source.dispatch_adapter is source
    assert source.drain_outputs() == []
