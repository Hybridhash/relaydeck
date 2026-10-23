"""Connector live relay against scripted sockets.

Post-bootstrap upstream events and PTY bytes end to end into a
loopback gateway; per-pane subscriptions; dead-attach reap/respawn;
epoch distribution intake; poison-snapshot guard + chunked send + push
breaker; CLI live-Herdr authorization. No real daemon, no host
mutation, no network providers: every Herdr byte comes from the
scripted daemon below, every gateway byte from a loopback server.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import itertools
import json
import logging
import os
import random
import selectors
import stat
import sys
import threading
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
import websockets.asyncio.client as ws_client

from relaydeck.connector.attach import AttachmentPool, AttachSession
from relaydeck.connector.cli import LIVE_HERDR_ENV
from relaydeck.connector.cli import main as connector_main
from relaydeck.connector.enrollment import ConnectorCreds, save_creds
from relaydeck.connector.fixture import FixtureAdapter
from relaydeck.connector.herdr import HerdrAdapter
from relaydeck.connector.journal import DispatchAdapter
from relaydeck.connector.link import ConnectorLink, LinkConfig, LinkError, TunnelTransport
from relaydeck.connector.source import (
    _PANE_SUB_RETRY_JITTER_MS,
    _PANE_SUB_RETRY_MS,
    HerdrSource,
    LiveSource,
)
from relaydeck.contracts.wire import terminal as terminal_wire
from relaydeck.gateway.app import GatewayState, create_app
from relaydeck.gateway.production import JournalBackend
from relaydeck.journal import CommandJournal
from tests.support.live_server import LiveGateway

UPSTREAM = Path(__file__).resolve().parent.parent / "fixtures" / "upstream"
NOW = 1_700_000_000_000
SECRET = b"herdr-live-relay-test-secret-32byte"
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
    return Path(f"/tmp/rd-live-{os.getpid()}-{next(_sock_counter)}.sock")


def _line(doc: dict[str, Any]) -> bytes:
    return (json.dumps(doc, separators=(",", ":")) + "\n").encode()


def _golden(name: str) -> dict[str, Any]:
    doc: dict[str, Any] = json.loads((UPSTREAM / f"{name}.json").read_text())
    return doc


class _LiveDaemon:
    """Scripted Herdr daemon with live injection hooks: golden replies,
    recorded requests/subscriptions, and post-bootstrap event writes +
    subscription close against the held connections."""

    def __init__(self, *, protocol: int = 22) -> None:
        self.protocol = protocol
        self.snapshot_inner: dict[str, Any] = dict(
            _golden("session_snapshot")["result"]["snapshot"]
        )
        self.agent_rows: list[dict[str, Any]] = list(_golden("agent_list")["result"]["agents"])
        self.methods: list[str] = []
        self.params: list[dict[str, Any]] = []
        self.subscribes: list[dict[str, Any]] = []
        self._sub_writers: list[asyncio.StreamWriter] = []
        self._ids = 0
        self._server: asyncio.AbstractServer | None = None
        self._sock: Path | None = None

    async def start(self, sock: Path) -> _LiveDaemon:
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

    @property
    def sub_count(self) -> int:
        return len(self._sub_writers)

    async def emit_global(self, doc: dict[str, Any]) -> None:
        await self.emit_raw_global(_line(doc))

    async def emit_raw_global(self, raw: bytes) -> None:
        writer = self._sub_writers[0]
        writer.write(raw)
        await writer.drain()

    async def close_global_sub(self) -> None:
        writer = self._sub_writers.pop(0)
        writer.close()
        try:
            await writer.wait_closed()
        except (OSError, asyncio.CancelledError):
            pass

    async def close_pane_sub(self) -> None:
        writer = self._sub_writers.pop(1)
        writer.close()
        try:
            await writer.wait_closed()
        except (OSError, asyncio.CancelledError):
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
                writer.write(_line({"id": rid, "result": {"snapshot": self.snapshot_inner}}))
                await writer.drain()
                return
            if method == "agent.list":
                writer.write(_line({"id": rid, "result": {"agents": self.agent_rows}}))
                await writer.drain()
                return
            if method == "events.subscribe":
                subs = params.get("subscriptions", []) if isinstance(params, dict) else []
                self.subscribes.append(params if isinstance(params, dict) else {})
                writer.write(_line({"id": rid, "result": {"type": "subscription_started"}}))
                await writer.drain()
                self._sub_writers.append(writer)
                _ = subs
                while True:
                    chunk = await reader.read(1024)
                    if not chunk:
                        return
                return
            if method == "tab.create":
                tab_id, pane_id = self._next("tab"), self._next("pane")
                writer.write(
                    _line(
                        {
                            "id": rid,
                            "result": {
                                "type": "tab_created",
                                "tab": {"tab_id": tab_id},
                                "root_pane": {"pane_id": pane_id},
                            },
                        }
                    )
                )
                await writer.drain()
                return
            writer.write(_line({"id": rid, "error": {"code": "unknown_method", "message": method}}))
            await writer.drain()
        finally:
            writer.close()


class _ScriptedTransport:
    """In-memory tunnel: scripted inbound, recorded outbound."""

    def __init__(self, inbound: list[str]) -> None:
        self.inbound = list(inbound)
        self.sent: list[str] = []
        self.closed = False

    async def send_text(self, text: str) -> None:
        self.sent.append(text)

    async def recv_text(self) -> str:
        if self.inbound:
            return self.inbound.pop(0)
        await asyncio.sleep(3600.0)
        raise AssertionError("unreachable")

    async def close(self) -> None:
        self.closed = True


class _FailingTransport(_ScriptedTransport):
    async def send_text(self, text: str) -> None:
        raise OSError("boom")


class _BigSnapshotSource:
    """Minimal LinkSource stub with an oversized snapshot document."""

    def __init__(self, snapshot: dict[str, Any]) -> None:
        self._snapshot = snapshot
        self._fixture = FixtureAdapter()

    @property
    def dispatch_adapter(self) -> DispatchAdapter:
        return self._fixture.dispatch_adapter

    def snapshot_document(self, host_id: str) -> dict[str, Any]:
        return self._snapshot

    def drain_outputs(self) -> list[tuple[str, str, bytes]]:
        return []


def _seed(tmp_path: Path, name: str = "gw.db") -> tuple[GatewayState, JournalBackend]:
    journal = CommandJournal(tmp_path / name, clock=lambda: NOW)
    backend = JournalBackend(journal, now_ms=lambda: NOW)
    state = GatewayState(backend=backend, cursor_secret=SECRET, now_ms=lambda: NOW)
    return state, backend


def _token(state: GatewayState) -> str:
    access, _ = state.sessions.create("acct-1", ALL_CAPS, frozenset({"home-mac"}), NOW)
    return access


def _live_ticket(state: GatewayState, lease_id: str) -> str:
    from relaydeck.gateway.identity import TicketGrant

    session = state.sessions.lookup(_token(state), NOW)
    assert session is not None
    return state.tickets.issue(
        TicketGrant(
            account=session.account,
            device_id=session.device_id,
            capabilities=session.capabilities,
            hosts=session.hosts,
            lease_id=lease_id,
            host_id="home-mac",
        ),
        NOW,
    )


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


def _stub_binary(tmp_path: Path) -> str:
    stub = tmp_path / "stub-herdr"
    stub.write_text(_STUB.format(python=sys.executable))
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    return str(stub)


_EXIT_STUB = """#!{python}
import sys
sys.stdout.write("final-banner\\n")
sys.stdout.flush()
"""


def _exit_stub_binary(tmp_path: Path) -> str:
    stub = tmp_path / "exit-herdr"
    stub.write_text(_EXIT_STUB.format(python=sys.executable))
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    return str(stub)


async def _wait_for(predicate: Any, timeout_s: float = 30.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if await predicate():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("timed out waiting for loopback condition")


async def _recv_until(
    viewer: Any, expected_epoch: str, markers: tuple[bytes, ...], timeout_s: float = 30.0
) -> bytes:
    """Receive viewer frames until every marker arrived. Deterministic:
    short socket waits inside one monotonic deadline (no sleeps-as-sync,
    no nested long timeouts); every frame must ride the expected epoch."""
    received = bytearray()
    deadline = time.monotonic() + timeout_s
    while not all(marker in received for marker in markers):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AssertionError(f"viewer missed markers: want={markers!r} got={bytes(received)!r}")
        try:
            raw_chunk = await asyncio.wait_for(viewer.recv(), timeout=min(remaining, 0.5))
        except TimeoutError:
            continue
        assert isinstance(raw_chunk, bytes)
        chunk = terminal_wire.decode_output(raw_chunk)
        assert chunk.epoch == expected_epoch
        assert chunk.offset == len(received), "viewer output was skipped or replayed"
        received += chunk.data
    return bytes(received)


def _sent_kinds(script: _ScriptedTransport) -> list[str]:
    return [json.loads(text)["kind"] for text in script.sent]


def _sent_snapshots(script: _ScriptedTransport) -> list[dict[str, Any]]:
    return [json.loads(text)["snapshot"] for text in script.sent if '"snapshot"' in text]


def test_live_event_reaches_loopback_gateway(tmp_path: Path) -> None:
    async def scenario() -> None:
        sock = _sock_path()
        daemon = await _LiveDaemon().start(sock)
        state, backend = _seed(tmp_path)
        source = HerdrSource(
            HerdrAdapter(socket_path=str(sock)), clock=lambda: NOW, boot_tag="live1"
        )
        try:
            bootstrapped = await source.bootstrap()
            assert isinstance(source, LiveSource)
            with LiveGateway(create_app(state)) as live:
                access = _token(state)
                host_token = state.hosts.enroll("home-mac", "Home Mac", "acct-1")
                link = ConnectorLink(
                    LinkConfig(
                        gateway_url=live.base_url,
                        host_id="home-mac",
                        host_token=host_token,
                        journal_path=str(tmp_path / "c.db"),
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
                            return bool(
                                response.status_code == 200
                                and response.json()["host"]["epoch"] == bootstrapped.epoch
                            )

                        await _wait_for(pushed)
                    # Post-bootstrap upstream event (never staged pre-connect).
                    await daemon.emit_global(
                        {
                            "event": "pane.agent_status_changed",
                            "data": {"pane_id": "w1:p1", "agent_status": "working"},
                        }
                    )

                    async def relayed() -> bool:
                        return any(
                            event.get("event_id") == "herdr-1-1"
                            for event in backend.event_logs.get("home-mac", [])
                        )

                    await _wait_for(relayed)
                    stored = backend.event_logs["home-mac"]
                    live_one = next(e for e in stored if e["event_id"] == "herdr-1-1")
                    assert live_one["type"] == "pane.upserted"
                    assert live_one["resource_id"] == "w1:p1"
                    assert live_one["payload"] == {"agent_status": "working"}
                    assert live_one["sequence"] >= 1
                    assert live_one["cursor"]
                finally:
                    stop.set()
                    outcome = await asyncio.wait_for(task, timeout=15.0)
                assert outcome.reason == "shutdown"
                assert outcome.sessions == 1
                assert outcome.pushed_events >= 1
        finally:
            await source.close()
            await daemon.stop()
            backend.close()

    asyncio.run(scenario())


def test_live_pty_bytes_reach_viewer(tmp_path: Path) -> None:
    async def scenario() -> None:
        sock = _sock_path()
        daemon = await _LiveDaemon().start(sock)
        state, backend = _seed(tmp_path)
        source = HerdrSource(
            HerdrAdapter(socket_path=str(sock)),
            clock=lambda: NOW,
            boot_tag="pty1",
            binary=_stub_binary(tmp_path),
        )
        try:
            bootstrapped = await source.bootstrap()
            attachment, _ = backend.create_attachment(
                "home-mac", "term-3", "shell", "acct-1", "dev-1", takeover=False
            )
            lease = backend.acquire_control(attachment.attachment_id, "acct-1", "dev-1")
            ticket = _live_ticket(state, lease.lease_id)
            # No tunnel reader exists yet, so this session is reliably unmapped.
            # Wait for actual buffered PTY bytes: an empty relay must not merely
            # mean the child has not produced its banner yet.
            session = source.attach_shell("term-3")
            assert source._epochs == {}
            with selectors.DefaultSelector() as polling:
                polling.register(session.fileno(), selectors.EVENT_READ)

                async def buffered() -> bool:
                    return bool(polling.select(0))

                await _wait_for(buffered)
                assert source.pump_live() == 0
                assert source.drain_outputs() == []
            with LiveGateway(create_app(state)) as live:
                host_token = state.hosts.enroll("home-mac", "Home Mac", "acct-1")
                link = ConnectorLink(
                    LinkConfig(
                        gateway_url=live.base_url,
                        host_id="home-mac",
                        host_token=host_token,
                        journal_path=str(tmp_path / "c.db"),
                        clock=lambda: NOW,
                    ),
                    source,
                )
                stop = asyncio.Event()
                task = asyncio.create_task(link.run(stop))
                try:
                    async with httpx.AsyncClient(timeout=10.0) as client:
                        headers = {"Authorization": f"Bearer {_token(state)}"}

                        async def pushed() -> bool:
                            response = await client.get(
                                f"{live.base_url}/v1/hosts/home-mac/snapshot", headers=headers
                            )
                            return bool(
                                response.status_code == 200
                                and response.json()["host"]["epoch"] == bootstrapped.epoch
                            )

                        await _wait_for(pushed)

                    # The real tunnel reader supplies the route; it may already
                    # have pumped the retained banner before the viewer opens.
                    async def mapped() -> bool:
                        return source._epochs.get("term-3") == attachment.epoch

                    await _wait_for(mapped)
                    assert source._attached["shell:term-3"][0] is session
                    viewer_url = (
                        f"ws://127.0.0.1:{live.port}/v1/terminal/{lease.lease_id}"
                        f"?ticket={ticket}&epoch={attachment.epoch}&offset=0"
                    )
                    async with ws_client.connect(viewer_url) as viewer:

                        async def viewing() -> bool:
                            return backend.rings.viewer_count(attachment.attachment_id) == 1

                        await _wait_for(viewing)
                        session.write(b"live-bytes\n")
                        received = await _recv_until(
                            viewer, attachment.epoch, (b"stub-ready\r\r\n", b"echo:live-bytes\r\n")
                        )
                    # The PTY echoes the input, then the stub prints its reply.
                    # Both the buffered banner and reply arrive exactly once.
                    assert received == b"stub-ready\r\r\nlive-bytes\r\necho:live-bytes\r\n"
                finally:
                    stop.set()
                    outcome = await asyncio.wait_for(task, timeout=15.0)
                assert outcome.reason == "shutdown"
                assert outcome.pushed_outputs >= 1
        finally:
            await source.close()
            await daemon.stop()
            backend.close()

    asyncio.run(scenario())


def test_dead_attach_reaped_and_respawned_on_next_epochs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """F1: a dead attach session is reaped loudly (epoch kept) and the
    next gateway epochs respawn it; output resumes on the same epoch
    and terminal input is never replayed during recovery."""
    writes: list[bytes] = []
    real_write = AttachSession.write

    def _spy(self: AttachSession, data: bytes) -> int:
        writes.append(bytes(data))
        return real_write(self, data)

    monkeypatch.setattr(AttachSession, "write", _spy)
    caplog.set_level(logging.WARNING, logger="relaydeck.connector.source")

    async def scenario() -> None:
        sock = _sock_path()
        daemon = await _LiveDaemon().start(sock)
        source = HerdrSource(
            HerdrAdapter(socket_path=str(sock)),
            clock=lambda: NOW,
            boot_tag="dead1",
            binary=_stub_binary(tmp_path),
        )
        try:
            await source.bootstrap()
            epochs_msg = json.dumps(
                {
                    "kind": "terminal.epochs",
                    "host_id": "home-mac",
                    "epochs": [{"terminal_id": "term-9", "epoch": "ring-e9", "kind": "shell"}],
                }
            )
            first = _ScriptedTransport(
                [json.dumps({"kind": "hello", "host_id": "home-mac"}), epochs_msg]
            )
            second = _ScriptedTransport([json.dumps({"kind": "hello", "host_id": "home-mac"})])
            scripts = [first, second]

            async def opener() -> TunnelTransport:
                return scripts.pop(0)

            link = ConnectorLink(
                LinkConfig(
                    gateway_url="http://127.0.0.1:9",
                    host_id="home-mac",
                    host_token="rdh_dead",
                    journal_path=str(tmp_path / "c.db"),
                    backoff_base_ms=5.0,
                    backoff_max_ms=10.0,
                    ping_interval_s=0,
                    clock=lambda: NOW,
                ),
                source,
                opener=opener,
            )
            stop = asyncio.Event()
            task = asyncio.create_task(link.run(stop))
            try:

                async def attached() -> bool:
                    return source.attached_terminals() == ("term-9",)

                def _outputs(script: _ScriptedTransport) -> list[dict[str, Any]]:
                    return [
                        json.loads(text)
                        for text in script.sent
                        if json.loads(text)["kind"] == "terminal.output"
                    ]

                await _wait_for(attached)

                async def output_pushed() -> bool:
                    return _outputs(first) != []

                await _wait_for(output_pushed)
                assert b"stub-ready" in base64.b64decode(_outputs(first)[0]["data_b64"])
                assert _outputs(first)[0]["epoch"] == "ring-e9"
                before = source._attached["shell:term-9"][0]
                pushed_before = len(_outputs(first))
                # Upstream death: kill the attach child outright (the
                # session object is left alone, exactly as in production).
                assert before._proc is not None
                before._proc.kill()
                before._proc.wait(timeout=5.0)

                async def reaped() -> bool:
                    return source.attached_terminals() == ()

                await _wait_for(reaped)
                assert source._epochs == {"term-9": "ring-e9"}
                assert any(
                    record.levelno >= logging.WARNING
                    and "attach session died" in record.getMessage()
                    and "term-9" in record.getMessage()
                    for record in caplog.records
                )
                # Next gateway epochs respawn a fresh session, same epoch.
                first.inbound.append(epochs_msg)
                await _wait_for(attached)
                after = source._attached["shell:term-9"][0]
                assert after is not before

                async def resumed() -> bool:
                    return len(_outputs(first)) > pushed_before

                await _wait_for(resumed)
                fresh = _outputs(first)[pushed_before:]
                assert all(frame["epoch"] == "ring-e9" for frame in fresh)
                assert b"stub-ready" in b"".join(
                    base64.b64decode(frame["data_b64"]) for frame in fresh
                )
                # Daemon death of the held subscription rebootstraps: the
                # sweep drops even live attaches (epochs persist), and the
                # next epochs respawn again.
                await daemon.close_global_sub()

                async def second_pushed() -> bool:
                    return _sent_snapshots(second) != []

                await _wait_for(second_pushed)
                assert _sent_snapshots(second)[0]["host"]["epoch"] == "herdr-22-2-dead1"
                assert source.attached_terminals() == ()
                assert source._epochs == {"term-9": "ring-e9"}
                assert any(
                    "upstream rebootstrap dropping 1 attach" in record.getMessage()
                    for record in caplog.records
                )
                second.inbound.append(epochs_msg)
                await _wait_for(attached)
                third = source._attached["shell:term-9"][0]
                assert third is not after

                async def resumed_again() -> bool:
                    return _outputs(second) != []

                await _wait_for(resumed_again)
                assert all(frame["epoch"] == "ring-e9" for frame in _outputs(second))
            finally:
                stop.set()
                outcome = await asyncio.wait_for(task, timeout=15.0)
            assert outcome.reason == "shutdown"
            assert outcome.sessions == 2
            assert writes == []
            kinds = _sent_kinds(first) + _sent_kinds(second)
            assert kinds and set(kinds) <= {
                "snapshot",
                "event",
                "terminal.output",
                "terminal.reset_required",
                "bye",
            }
            # The dead attach asks viewers to reset so the respawned screen is rebuilt cleanly.
            assert "terminal.reset_required" in _sent_kinds(first)
        finally:
            await source.close()
            await daemon.stop()

    asyncio.run(scenario())


def test_drain_dead_relays_buffered_bytes(tmp_path: Path) -> None:
    """F1: the dead-session drain relays exact buffered bytes under the
    gateway-provided epoch (readability-gated, no sleeps-as-sync)."""
    source = HerdrSource(
        HerdrAdapter(socket_path=str(tmp_path / "unused.sock")),
        clock=lambda: NOW,
        boot_tag="dead3",
        binary=_stub_binary(tmp_path),
    )
    try:
        session = source.attach_shell("t9")
        assert session._fd is not None
        with selectors.DefaultSelector() as polling:
            polling.register(session._fd, selectors.EVENT_READ)
            deadline = time.monotonic() + 5.0
            while not polling.select(0.1):
                assert time.monotonic() < deadline, "stub banner never arrived"
        source._drain_dead(session, "t9", "e9")
        outputs = source.drain_outputs()
        assert outputs and all(terminal == "t9" and epoch == "e9" for terminal, epoch, _ in outputs)
        assert b"stub-ready" in b"".join(data for _, _, data in outputs)
    finally:
        asyncio.run(source.close())


def test_note_epochs_replaces_dead_unmapped_session(tmp_path: Path) -> None:
    """F1: a session that died while epoch-unmapped is reaped and
    replaced by a fresh attach on the next epochs (epoch kept)."""
    source = HerdrSource(
        HerdrAdapter(socket_path=str(tmp_path / "unused.sock")),
        clock=lambda: NOW,
        boot_tag="dead2",
        binary=_exit_stub_binary(tmp_path),
    )
    try:
        session = source.attach_shell("t9")
        assert session._proc is not None
        assert session._proc.wait(timeout=5.0) == 0  # child self-exits: upstream death
        assert source.note_epochs({"t9": {"epoch": "e9", "kind": "shell"}}) == {}
        assert source.attached_terminals() == ("t9",)
        assert source._attached["shell:t9"][0] is not session
        assert source._epochs == {"t9": "e9"}
        outputs = source.drain_outputs()
        # Best-effort last bytes only: macOS discards the PTY buffer on
        # slave close, so the relay may be empty here; anything relayed
        # is exact (terminal, epoch) routing.
        assert all(terminal == "t9" and epoch == "e9" for terminal, epoch, _ in outputs)
    finally:
        asyncio.run(source.close())


def test_note_epochs_auto_attach_and_failures(tmp_path: Path) -> None:
    pool = AttachmentPool(slots=1)
    source = HerdrSource(
        HerdrAdapter(socket_path=str(tmp_path / "unused.sock")),
        binary=_stub_binary(tmp_path),
        pool=pool,
    )
    try:
        assert source.note_epochs({"t1": {"epoch": "e1", "kind": "shell"}}) == {}
        assert source.attached_terminals() == ("t1",)
        # Re-noting the same terminal is idempotent (no second attach).
        assert source.note_epochs({"t1": {"epoch": "e2", "kind": "shell"}}) == {}
        assert source.attached_terminals() == ("t1",)
        # Protect the only slot: the next attach must fail, not evict.
        pool.set_visible({"shell:t1"})
        failures = source.note_epochs(
            {
                "t1": {"epoch": "e1", "kind": "shell"},
                "t2": {"epoch": "e1", "kind": "agent"},
                "t3": {"epoch": "", "kind": "shell"},
                "t4": {"epoch": "e1", "kind": "bogus"},
            }
        )
        assert set(failures) == {"t2", "t3", "t4"}
        assert "PoolFullError" in failures["t2"]
        assert failures["t3"] == "empty terminal or epoch"
        assert "unknown kind" in failures["t4"]
    finally:
        asyncio.run(source.close())


def test_pane_subscriptions_post_snapshot_and_resub(tmp_path: Path) -> None:
    async def scenario() -> None:
        sock = _sock_path()
        daemon = await _LiveDaemon().start(sock)
        source = HerdrSource(HerdrAdapter(socket_path=str(sock)), clock=lambda: NOW)
        try:
            await source.bootstrap()
            assert len(daemon.subscribes) == 2
            assert daemon.subscribes[1] == {
                "subscriptions": [{"type": "pane.agent_status_changed", "pane_id": "w1:p1"}]
            }
            assert source.pane_subscribed == frozenset({"w1:p1"})
            # Live pane.created (top-level id): translated AND tracked.
            await daemon.emit_global(
                {
                    "event": "pane.created",
                    "data": {"pane_id": "w1:p9", "pane": {"pane_id": "w1:p9"}},
                }
            )
            partials = await source.collect_live(2.0)
            assert any(
                p["type"] == "pane.upserted" and p["resource_id"] == "w1:p9" for p in partials
            )
            assert source.pane_subscribed == frozenset({"w1:p1", "w1:p9"})
            assert len(daemon.subscribes) == 3
            assert {entry["pane_id"] for entry in daemon.subscribes[2]["subscriptions"]} == {
                "w1:p1",
                "w1:p9",
            }
            # Nested-only pane.created: Herdr's created events nest the id, so it translates
            # to an upsert and is tracked for subscription coverage.
            await daemon.emit_global(
                {"event": "pane.created", "data": {"pane": {"pane_id": "w1:p7"}}}
            )
            partials = await source.collect_live(2.0)
            assert [(p["type"], p["resource_id"], p.get("payload")) for p in partials] == [
                ("pane.upserted", "w1:p7", {})
            ]
            assert source.pane_subscribed == frozenset({"w1:p1", "w1:p9", "w1:p7"})
            # Live pane.closed prunes the subscription again.
            await daemon.emit_global(
                {"event": "pane.closed", "data": {"pane_id": "w1:p9", "workspace_id": "w1"}}
            )
            partials = await source.collect_live(2.0)
            assert any(
                p["type"] == "resource.removed" and p["resource_id"] == "w1:p9" for p in partials
            )
            assert source.pane_subscribed == frozenset({"w1:p1", "w1:p7"})
        finally:
            await source.close()
            await daemon.stop()

    asyncio.run(scenario())


def test_pane_resubscribe_backoff_no_churn(tmp_path: Path) -> None:
    """F2: per-pane re-subscribe honors the bounded, jittered backoff
    even with no subscription held (a sick daemon sees no hot spin),
    then exactly one retry past the bound restores coverage."""

    async def scenario() -> None:
        sock = _sock_path()
        daemon = await _LiveDaemon().start(sock)
        now = [NOW]
        source = HerdrSource(
            HerdrAdapter(socket_path=str(sock)),
            clock=lambda: now[0],
            boot_tag="panebo",
            pane_sub_seed=7,
        )
        try:
            await source.bootstrap()
            assert len(daemon.subscribes) == 2
            attempts = len(daemon.subscribes)
            await daemon.close_pane_sub()
            await source.collect_live(2.0)
            assert source.pane_subscribed == frozenset()
            gate_in_ms = source._pane_retry_after_ms - NOW
            assert (
                _PANE_SUB_RETRY_MS <= gate_in_ms <= (_PANE_SUB_RETRY_MS + _PANE_SUB_RETRY_JITTER_MS)
            )
            # Seeded schedule replays exactly (deterministic jitter).
            expected = _PANE_SUB_RETRY_MS + random.Random(7).randrange(  # noqa: S311 - seeded test data
                _PANE_SUB_RETRY_JITTER_MS + 1
            )
            assert gate_in_ms == expected
            # Frozen clock: many ticks, zero new attempts.
            for _ in range(5):
                await source.collect_live(0.2)
            assert len(daemon.subscribes) == attempts
            # +29s: still gated (backoff honored, no sub held).
            now[0] += 29_000
            await source.collect_live(0.2)
            assert len(daemon.subscribes) == attempts
            # Past the jittered bound: exactly one retry, coverage back.
            now[0] += _PANE_SUB_RETRY_JITTER_MS + 2_000
            await source.collect_live(0.2)
            assert len(daemon.subscribes) == attempts + 1
            assert source.pane_subscribed == frozenset({"w1:p1"})
            assert daemon.subscribes[-1] == {
                "subscriptions": [{"type": "pane.agent_status_changed", "pane_id": "w1:p1"}]
            }
        finally:
            await source.close()
            await daemon.stop()

    asyncio.run(scenario())


def test_collect_live_skips_corrupt_line(tmp_path: Path) -> None:
    async def scenario() -> None:
        sock = _sock_path()
        daemon = await _LiveDaemon().start(sock)
        source = HerdrSource(HerdrAdapter(socket_path=str(sock)), clock=lambda: NOW)
        try:
            await source.bootstrap()
            skipped_before = source.skipped_events
            await daemon.emit_raw_global(b"not-json{{{\n")
            await daemon.emit_global(
                {
                    "event": "pane.agent_status_changed",
                    "data": {"pane_id": "w1:p1", "agent_status": "done"},
                }
            )
            partials = await source.collect_live(2.0)
            assert len(partials) == 1
            assert partials[0]["payload"] == {"agent_status": "done"}
            assert source.skipped_events == skipped_before + 1
        finally:
            await source.close()
            await daemon.stop()

    asyncio.run(scenario())


def test_link_rebootstraps_after_upstream_eof(tmp_path: Path) -> None:
    async def scenario() -> None:
        sock = _sock_path()
        daemon = await _LiveDaemon().start(sock)
        source = HerdrSource(HerdrAdapter(socket_path=str(sock)), clock=lambda: NOW, boot_tag="re1")
        try:
            await source.bootstrap()
            first, second = (
                _ScriptedTransport([json.dumps({"kind": "hello", "host_id": "home-mac"})]),
                _ScriptedTransport([json.dumps({"kind": "hello", "host_id": "home-mac"})]),
            )
            scripts = [first, second]

            async def opener() -> TunnelTransport:
                return scripts.pop(0)

            link = ConnectorLink(
                LinkConfig(
                    gateway_url="http://127.0.0.1:9",
                    host_id="home-mac",
                    host_token="rdh_dead",
                    journal_path=str(tmp_path / "c.db"),
                    backoff_base_ms=5.0,
                    backoff_max_ms=10.0,
                    ping_interval_s=0,
                    clock=lambda: NOW,
                ),
                source,
                opener=opener,
            )
            stop = asyncio.Event()
            task = asyncio.create_task(link.run(stop))
            try:

                async def first_pushed() -> bool:
                    return _sent_snapshots(first) != []

                await _wait_for(first_pushed)
                assert _sent_snapshots(first)[0]["host"]["epoch"] == "herdr-22-1-re1"
                await daemon.close_global_sub()

                async def second_pushed() -> bool:
                    return _sent_snapshots(second) != []

                await _wait_for(second_pushed)
                assert _sent_snapshots(second)[0]["host"]["epoch"] == "herdr-22-2-re1"
                assert daemon.methods.count("ping") == 2
            finally:
                stop.set()
                outcome = await asyncio.wait_for(task, timeout=15.0)
            assert outcome.reason == "shutdown"
            assert outcome.sessions == 2
            assert first.closed and second.closed
        finally:
            await source.close()
            await daemon.stop()

    asyncio.run(scenario())


def test_link_resyncs_on_reconnect_same_epoch(tmp_path: Path) -> None:
    async def scenario() -> None:
        sock = _sock_path()
        daemon = await _LiveDaemon().start(sock)
        source = HerdrSource(HerdrAdapter(socket_path=str(sock)), clock=lambda: NOW, boot_tag="rs1")
        try:
            bootstrapped = await source.bootstrap()
            first = _ScriptedTransport(
                [
                    json.dumps({"kind": "hello", "host_id": "home-mac"}),
                    "not-json{{{",
                ]
            )
            second = _ScriptedTransport([json.dumps({"kind": "hello", "host_id": "home-mac"})])
            scripts = [first, second]

            async def opener() -> TunnelTransport:
                return scripts.pop(0)

            link = ConnectorLink(
                LinkConfig(
                    gateway_url="http://127.0.0.1:9",
                    host_id="home-mac",
                    host_token="rdh_dead",
                    journal_path=str(tmp_path / "c.db"),
                    backoff_base_ms=5.0,
                    backoff_max_ms=10.0,
                    ping_interval_s=0,
                    clock=lambda: NOW,
                ),
                source,
                opener=opener,
            )
            daemon.snapshot_inner["workspaces"] = [
                dict(daemon.snapshot_inner["workspaces"][0], label="renamed-project")
            ]
            stop = asyncio.Event()
            task = asyncio.create_task(link.run(stop))
            try:

                async def second_pushed() -> bool:
                    return _sent_snapshots(second) != []

                await _wait_for(second_pushed)
                pushed = _sent_snapshots(second)[0]
                assert pushed["workspaces"][0]["label"] == "renamed-project"
                assert pushed["host"]["epoch"] == bootstrapped.epoch
            finally:
                stop.set()
                outcome = await asyncio.wait_for(task, timeout=15.0)
            assert outcome.reason == "shutdown"
            assert outcome.sessions == 2
        finally:
            await source.close()
            await daemon.stop()

    asyncio.run(scenario())


def _big_snapshot(pad_chars: int) -> dict[str, Any]:
    return {
        "host": {
            "id": "home-mac",
            "label": "big",
            "epoch": "e-big",
            "reachable": True,
            "protocol": 22,
            "version": "0.9.1",
        },
        "cursor": "big-cursor",
        "workspaces": [],
        "tabs": [],
        "panes": [],
        "agents": [],
        "pad": "x" * pad_chars,
    }


def test_poison_snapshot_exits_without_churn(tmp_path: Path) -> None:
    async def scenario() -> None:
        script = _ScriptedTransport([json.dumps({"kind": "hello", "host_id": "home-mac"})])

        async def opener() -> TunnelTransport:
            return script

        link = ConnectorLink(
            LinkConfig(
                gateway_url="http://127.0.0.1:9",
                host_id="home-mac",
                host_token="rdh_dead",
                journal_path=str(tmp_path / "c.db"),
                max_retries=10,
                clock=lambda: NOW,
            ),
            _BigSnapshotSource(_big_snapshot(70_000)),
            opener=opener,
        )
        outcome = await asyncio.wait_for(link.run(asyncio.Event()), timeout=15.0)
        assert outcome.reason == "snapshot-too-large"
        assert outcome.sessions == 1
        assert outcome.pushed_snapshot is False
        assert script.sent == []
        assert script.closed

    asyncio.run(scenario())


def test_chunked_snapshot_send_when_advertised(tmp_path: Path) -> None:
    async def scenario() -> None:
        snapshot = _big_snapshot(100_000)
        script = _ScriptedTransport(
            [json.dumps({"kind": "hello", "host_id": "home-mac", "snapshot_chunks": True})]
        )

        async def opener() -> TunnelTransport:
            return script

        link = ConnectorLink(
            LinkConfig(
                gateway_url="http://127.0.0.1:9",
                host_id="home-mac",
                host_token="rdh_dead",
                journal_path=str(tmp_path / "c.db"),
                once=True,
                once_timeout_s=0.5,
                clock=lambda: NOW,
            ),
            _BigSnapshotSource(snapshot),
            opener=opener,
        )
        outcome = await asyncio.wait_for(link.run(asyncio.Event()), timeout=15.0)
        assert outcome.reason == "once-done"
        assert outcome.pushed_snapshot is True
        assert outcome.pushed_chunks >= 2
        kinds = _sent_kinds(script)
        assert kinds[0] == "snapshot.begin"
        assert kinds[-2] == "snapshot.end"
        assert kinds[-1] == "bye"  # --once farewell
        assert set(kinds[1:-2]) == {"snapshot.chunk"}
        for text in script.sent:
            assert len(text) <= 64 * 1024
        docs = [json.loads(text) for text in script.sent[:-1]]
        begin = docs[0]
        transfer = begin["transfer_id"]
        assert begin["total_chunks"] == outcome.pushed_chunks
        assert all(doc["transfer_id"] == transfer for doc in docs)
        assert [doc["seq"] for doc in docs[1:-1]] == list(range(begin["total_chunks"]))
        raw = b"".join(base64.b64decode(doc["data_b64"]) for doc in docs[1:-1])
        assert len(raw) == begin["total_bytes"]
        assert hashlib.sha256(raw).hexdigest() == begin["sha256"]
        assert json.loads(raw) == snapshot

    asyncio.run(scenario())


def test_initial_push_breaker_trips(tmp_path: Path) -> None:
    async def scenario() -> None:
        async def opener() -> TunnelTransport:
            return _FailingTransport([json.dumps({"kind": "hello", "host_id": "home-mac"})])

        link = ConnectorLink(
            LinkConfig(
                gateway_url="http://127.0.0.1:9",
                host_id="home-mac",
                host_token="rdh_dead",
                journal_path=str(tmp_path / "c.db"),
                max_retries=10,
                push_fail_limit=3,
                backoff_base_ms=5.0,
                backoff_max_ms=10.0,
                clock=lambda: NOW,
            ),
            FixtureAdapter(),
            opener=opener,
        )
        outcome = await asyncio.wait_for(link.run(asyncio.Event()), timeout=15.0)
        assert outcome.reason == "push-failed"
        assert outcome.sessions == 3
        assert outcome.pushed_snapshot is False

    asyncio.run(scenario())


def test_link_consumes_terminal_epochs_message(tmp_path: Path) -> None:
    async def scenario() -> None:
        sock = _sock_path()
        daemon = await _LiveDaemon().start(sock)
        source = HerdrSource(
            HerdrAdapter(socket_path=str(sock)),
            clock=lambda: NOW,
            binary=_stub_binary(tmp_path),
        )
        try:
            await source.bootstrap()
            script = _ScriptedTransport(
                [
                    json.dumps({"kind": "hello", "host_id": "home-mac"}),
                    json.dumps(
                        {
                            "kind": "terminal.epochs",
                            "host_id": "home-mac",
                            "epochs": [
                                {
                                    "terminal_id": "term-9",
                                    "epoch": "ring-e9",
                                    "kind": "shell",
                                }
                            ],
                        }
                    ),
                ]
            )

            async def opener() -> TunnelTransport:
                return script

            link = ConnectorLink(
                LinkConfig(
                    gateway_url="http://127.0.0.1:9",
                    host_id="home-mac",
                    host_token="rdh_dead",
                    journal_path=str(tmp_path / "c.db"),
                    ping_interval_s=0,
                    clock=lambda: NOW,
                ),
                source,
                opener=opener,
            )
            stop = asyncio.Event()
            task = asyncio.create_task(link.run(stop))
            try:

                async def attached() -> bool:
                    return source.attached_terminals() == ("term-9",)

                await _wait_for(attached)

                async def output_pushed() -> bool:
                    return "terminal.output" in _sent_kinds(script)

                await _wait_for(output_pushed)
                frames = [
                    json.loads(text)
                    for text in script.sent
                    if json.loads(text)["kind"] == "terminal.output"
                ]
                assert frames[0]["terminal_id"] == "term-9"
                assert frames[0]["epoch"] == "ring-e9"
                assert b"stub-ready" in base64.b64decode(frames[0]["data_b64"])
            finally:
                stop.set()
                outcome = await asyncio.wait_for(task, timeout=15.0)
            assert outcome.reason == "shutdown"
            # Malformed epoch docs fail closed (unit-level, same handler).
            with pytest.raises(LinkError, match="epochs"):
                link._handle_epochs({"kind": "terminal.epochs", "host_id": "nope"})
            with pytest.raises(LinkError, match="epochs"):
                link._handle_epochs(
                    {
                        "kind": "terminal.epochs",
                        "host_id": "home-mac",
                        "epochs": [{"terminal_id": "t", "epoch": "", "kind": "shell"}],
                    }
                )
        finally:
            await source.close()
            await daemon.stop()

    asyncio.run(scenario())


def test_link_applies_hello_epochs(tmp_path: Path) -> None:
    async def scenario() -> None:
        sock = _sock_path()
        daemon = await _LiveDaemon().start(sock)
        source = HerdrSource(
            HerdrAdapter(socket_path=str(sock)),
            clock=lambda: NOW,
            binary=_stub_binary(tmp_path),
        )
        try:
            await source.bootstrap()
            script = _ScriptedTransport(
                [
                    json.dumps(
                        {
                            "kind": "hello",
                            "host_id": "home-mac",
                            "epochs": [
                                {
                                    "terminal_id": "term-7",
                                    "epoch": "ring-e7",
                                    "kind": "shell",
                                },
                                {"terminal_id": "bad", "epoch": "", "kind": "shell"},
                            ],
                        }
                    )
                ]
            )

            async def opener() -> TunnelTransport:
                return script

            link = ConnectorLink(
                LinkConfig(
                    gateway_url="http://127.0.0.1:9",
                    host_id="home-mac",
                    host_token="rdh_dead",
                    journal_path=str(tmp_path / "c.db"),
                    ping_interval_s=0,
                    clock=lambda: NOW,
                ),
                source,
                opener=opener,
            )
            stop = asyncio.Event()
            task = asyncio.create_task(link.run(stop))
            try:

                async def attached() -> bool:
                    return source.attached_terminals() == ("term-7",)

                await _wait_for(attached)
                # The malformed hello entry was skipped loudly, not fatal.
                assert _sent_snapshots(script) != []
            finally:
                stop.set()
                outcome = await asyncio.wait_for(task, timeout=15.0)
            assert outcome.reason == "shutdown"
        finally:
            await source.close()
            await daemon.stop()

    asyncio.run(scenario())


def test_cli_live_herdr_refusals(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv(LIVE_HERDR_ENV, raising=False)
    assert connector_main(["run"]) == 2
    assert connector_main(["run", "--live-herdr"]) == 2
    assert connector_main(["run", "--fixture", "--live-herdr"]) == 2
    # Authorized but the daemon socket is dead: honest failure, exit 1.
    monkeypatch.setenv(LIVE_HERDR_ENV, "1")
    save_creds(ConnectorCreds("http://127.0.0.1:9", "home-mac", "rdh_dead"))
    assert (
        connector_main(
            [
                "run",
                "--live-herdr",
                "--herdr-socket",
                str(tmp_path / "no.sock"),
                "--journal-path",
                str(tmp_path / "c.db"),
            ]
        )
        == 1
    )


def test_cli_live_herdr_once_loopback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        sock = _sock_path()
        daemon = await _LiveDaemon().start(sock)
        state, backend = _seed(tmp_path)
        try:
            host_token = state.hosts.enroll("home-mac", "Home Mac", "acct-1")
            with LiveGateway(create_app(state)) as live:
                save_creds(ConnectorCreds(live.base_url, "home-mac", host_token))
                argv = [
                    "run",
                    "--live-herdr",
                    "--herdr-socket",
                    str(sock),
                    "--once",
                    "--once-timeout",
                    "3",
                    "--journal-path",
                    str(tmp_path / "cli.db"),
                ]
                result: dict[str, int] = {}

                def target() -> None:
                    result["rc"] = connector_main(argv)

                thread = threading.Thread(target=target, daemon=True)
                thread.start()
                deadline = time.monotonic() + 30.0
                while thread.is_alive() and time.monotonic() < deadline:
                    await asyncio.sleep(0.05)
                assert not thread.is_alive(), "CLI live run hung"
                assert result["rc"] == 0
                assert backend.snapshot("home-mac")["host"]["epoch"].startswith("herdr-22-1-")
        finally:
            await daemon.stop()
            backend.close()

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv(LIVE_HERDR_ENV, "1")
    asyncio.run(scenario())
