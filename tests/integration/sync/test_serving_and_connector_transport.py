"""Serving/connector integration with ASGI sockets and synthetic PTYs.

Runs real handlers, journals, Link and HerdrSource input/epoch logic. The
attached PTY and upstream subscription are explicit doubles, not live Herdr.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
from pathlib import Path
from typing import Any

import httpx
import pytest

from relaydeck.client.serve import build_web_app
from relaydeck.connector.attach import AttachSession
from relaydeck.connector.link import ConnectorLink, LinkConfig, TunnelTransport
from relaydeck.connector.source import TerminalRelay
from relaydeck.contracts.tunnel import SnapshotIntake, parse_terminal_input
from relaydeck.gateway import terminal_wire
from relaydeck.gateway.app import create_app
from relaydeck.gateway.hosts import write_revoke_request
from relaydeck.gateway.identity import TicketGrant
from relaydeck.gateway.sockets import SocketRegistry
from tests.security.helpers import ALL_CAPS, NOW, auth, mint_token
from tests.support.asgi_socket import AsgiSocket
from tests.support.sync.transport_fixtures import _state, _wait


async def _next_kind(socket: AsgiSocket, kind: str) -> dict[str, Any]:
    while True:
        message = await socket.receive()
        if message["type"] == "websocket.close":
            return message
        doc: dict[str, Any] = json.loads(message["text"])
        if doc.get("kind") == kind:
            return doc


def test_input_resize_output_and_authoritative_epoch_cycle(tmp_path: Path) -> None:
    async def scenario() -> None:
        state, backend, source = _state(tmp_path)
        token = mint_token(state)
        host_token = state.hosts.enroll("home-mac", account="acct-1")
        app = create_app(state)
        tunnel = AsgiSocket(app, "/v1/connectors", token=host_token)

        async def opener() -> TunnelTransport:
            return await tunnel.open()

        link = ConnectorLink(
            LinkConfig(
                "http://synthetic",
                "home-mac",
                host_token,
                journal_path=str(tmp_path / "c.db"),
                max_retries=0,
            ),
            source,
            opener=opener,
        )
        stop = asyncio.Event()
        task = asyncio.create_task(link.run(stop))
        viewer: AsgiSocket | None = None
        try:
            await _wait(lambda: bool(link._pushed_snapshot))
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://synthetic",
                headers=auth(token),
            ) as http:
                response = await http.post(
                    "/v1/hosts/home-mac/terminals/term-1/attachments",
                    json={"kind": "shell", "takeover": False},
                )
                assert response.status_code == 201, response.text
                attachment = response.json()
                await _wait(lambda: bool(source.created))
                pty = source.created[-1]
                response = await http.post(
                    f"/v1/terminal-attachments/{attachment['attachment_id']}/leases",
                    json={"mode": "control"},
                )
                lease = response.json()
                response = await http.post(
                    "/v1/terminal-tickets", json={"lease_id": lease["lease_id"]}
                )
                assert response.status_code == 200, response.text
                viewer = await AsgiSocket(
                    app, f"/v1/terminal/{lease['lease_id']}?ticket=" + response.json()["ticket"]
                ).open()
                exact = b"draft \xe4\xb8\xad\xe6\x96\x87\x1b[A\x03\t\r"
                frame = terminal_wire.encode_text(lease["lease_id"], lease["generation"], 1, exact)
                await viewer.send_bytes(frame)
                await _wait(lambda: bool(pty.writes))
                await viewer.send_bytes(frame)  # same sequence is never delivered twice
                await viewer.send_bytes(
                    terminal_wire.encode_resize(lease["lease_id"], lease["generation"], 2, 120, 40)
                )
                await _wait(lambda: bool(pty.sizes))
                assert pty.writes == [exact] and pty.sizes == [(120, 40)]
                output = b"\x1b[31moutput\x1b[0m" + b"x" * (64 * 1024)
                pty.output.append(output)
                received = bytearray()
                offsets = []
                while len(received) < len(output):
                    chunk = terminal_wire.decode_output((await viewer.receive())["bytes"])
                    offsets.append(chunk.offset)
                    assert chunk.offset == len(received)
                    received.extend(chunk.data)
                assert received == output and offsets[0] == 0
                assert all(len(text.encode()) <= 64 * 1024 for text in tunnel.sent)

                pty.fail_write = True
                await viewer.send_bytes(
                    terminal_wire.encode_text(
                        lease["lease_id"], lease["generation"], 3, b"uncertain"
                    )
                )
                assert (await _next_kind(viewer, "gap"))["detail"] == "input_uncertain"
                await viewer.send_bytes(
                    terminal_wire.encode_text(
                        lease["lease_id"], lease["generation"], 3, b"uncertain"
                    )
                )
                await asyncio.sleep(0.06)
                assert pty.writes == [exact, b"uncertain"]

                takeover = await http.post(
                    "/v1/hosts/home-mac/terminals/term-1/attachments",
                    json={"kind": "shell", "takeover": True},
                )
                assert takeover.status_code == 200
                assert (await _next_kind(viewer, "reset"))["detail"] == "stale_epoch"
                await _wait(lambda: len(source.created) == 2)
                assert not pty.active and source.created[1].writes == []
                await http.delete(f"/v1/terminal-attachments/{attachment['attachment_id']}")
                await _wait(lambda: not source.attached_terminals())
                assert source._epochs == {} and not source.created[1].active
        finally:
            if viewer is not None:
                await viewer.close()
            stop.set()
            await asyncio.wait_for(task, 2)
            await source.close()
            backend.close()
        assert not state.sockets._sockets
        assert not state.sockets._send_locks

    asyncio.run(scenario())


def test_connector_disconnect_gives_gap_and_never_replays_input(tmp_path: Path) -> None:
    async def scenario() -> None:
        state, backend, source = _state(tmp_path)
        token = mint_token(state)
        host_token = state.hosts.enroll("home-mac", account="acct-1")
        attachment, _ = backend.create_attachment(
            "home-mac", "term-1", "shell", "acct-1", "dev", takeover=False
        )
        lease = backend.acquire_control(attachment.attachment_id, "acct-1", "dev")
        ticket = state.tickets.issue(
            TicketGrant(
                "acct-1", "dev", ALL_CAPS, frozenset({"home-mac"}), lease.lease_id, "home-mac"
            ),
            NOW,
        )
        app = create_app(state)
        viewer = await AsgiSocket(app, f"/v1/terminal/{lease.lease_id}?ticket={ticket}").open()
        try:
            frame = terminal_wire.encode_text(lease.lease_id, lease.generation, 1, b"do not replay")
            await viewer.send_bytes(frame)
            assert (await _next_kind(viewer, "gap"))["detail"] == "host_offline"
            # A gap retires this reader. Later frames on it cannot repair the
            # connection or reach a returned connector; recovery needs a new reader.
            assert viewer.task is not None
            await asyncio.wait_for(viewer.task, 2)
            recorded = list(backend.input_logs.get(lease.lease_id, []))
            tunnel = await AsgiSocket(app, "/v1/connectors", token=host_token).open()
            try:
                assert (await _next_kind(tunnel, "hello"))["epochs"]
                await viewer.send_bytes(frame)
                await viewer.send_bytes(
                    terminal_wire.encode_resize(lease.lease_id, lease.generation, 2, 90, 30)
                )
                await tunnel.send_text(json.dumps({"kind": "ping"}))
                while True:
                    doc = json.loads((await tunnel.receive())["text"])
                    assert doc["kind"] not in ("terminal.input", "terminal.resize")
                    if doc["kind"] == "pong":
                        break
                assert backend.input_logs.get(lease.lease_id, []) == recorded
                assert backend.resize_log == []
                assert backend.drain_outbox("home-mac") == []
            finally:
                await tunnel.close()
        finally:
            await viewer.close()
            await source.close()
            backend.close()
        assert token  # authorization setup remained synthetic

    asyncio.run(scenario())


def _transfer(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    raw = json.dumps(snapshot).encode()
    pieces = [raw[at : at + 32768] for at in range(0, len(raw), 32768)]
    common = {"host_id": "home-mac", "transfer_id": "synthetic-transfer"}
    return [
        dict(
            common,
            kind="snapshot.begin",
            total_bytes=len(raw),
            total_chunks=len(pieces),
            sha256=hashlib.sha256(raw).hexdigest(),
        ),
        *(
            dict(common, kind="snapshot.chunk", seq=seq, data_b64=base64.b64encode(piece).decode())
            for seq, piece in enumerate(pieces)
        ),
        dict(common, kind="snapshot.end"),
    ]


def test_large_snapshot_link_round_trip_and_upstream_continuity_rotation(tmp_path: Path) -> None:
    async def scenario() -> None:
        state, backend, source = _state(tmp_path)
        assert source._snapshot is not None
        source._snapshot["workspaces"] = [
            dict(source._snapshot["workspaces"][0], workspace_id=f"w{n}", label="x" * 128)
            for n in range(600)
        ]
        attachment, _ = backend.create_attachment(
            "home-mac", "term-1", "shell", "acct-1", "dev", takeover=False
        )
        lease = backend.acquire_control(attachment.attachment_id, "acct-1", "dev")
        host_token = state.hosts.enroll("home-mac", account="acct-1")
        app = create_app(state)
        tunnel = AsgiSocket(app, "/v1/connectors", token=host_token)

        async def opener() -> TunnelTransport:
            return await tunnel.open()

        link = ConnectorLink(
            LinkConfig(
                "http://synthetic",
                "home-mac",
                host_token,
                journal_path=str(tmp_path / "c.db"),
                max_retries=0,
            ),
            source,
            opener=opener,
        )
        stop = asyncio.Event()
        task = asyncio.create_task(link.run(stop))
        try:
            await _wait(
                lambda: len(backend.snapshots["home-mac"]["workspaces"]) == 600
                and link._pushed_chunks > 1
            )
            assert link._pushed_chunks > 1
            assert all(len(text.encode()) <= 64 * 1024 for text in tunnel.sent)
            previous = source.created[0]
            # Real tunnel intake receives a new upstream bootstrap epoch.
            snapshot = source.snapshot_document("home-mac")
            snapshot["host"]["epoch"] = "new-upstream-continuity"
            for doc in _transfer(snapshot):
                await tunnel.send_text(json.dumps(doc))
            await _wait(lambda: backend.open_lease(lease.lease_id) is None)
            await _wait(lambda: source._epochs.get("term-1") == "e2")
            assert not previous.active and len(source.created) == 2
            assert source.created[1].writes == []
            rotated = backend.get_attachment(attachment.attachment_id)
            assert rotated is not None and rotated.epoch == "e2"
        finally:
            stop.set()
            if not task.done():
                await asyncio.wait_for(task, 2)
            await source.close()
            backend.close()
        assert lease.epoch == "e1"

    asyncio.run(scenario())


@pytest.mark.parametrize("fault", ["digest", "sequence", "bytes", "host", "content", "second"])
def test_poison_transfer_fails_closed_without_replacing_snapshot(
    tmp_path: Path, fault: str
) -> None:
    async def scenario() -> None:
        state, backend, source = _state(tmp_path)
        before = backend.snapshot("home-mac")
        docs = _transfer(before)
        if fault == "digest":
            docs[0]["sha256"] = "0" * 64
        elif fault == "sequence":
            docs[1]["seq"] = 1
        elif fault == "bytes":
            docs[0]["total_bytes"] = 1024 * 1024 + 1
        elif fault == "host":
            docs[1]["host_id"] = "other-host"
        elif fault == "content":
            docs = _transfer({"host": {"id": "home-mac"}})
        elif fault == "second":
            docs.insert(1, dict(docs[0]))
        host_token = state.hosts.enroll("home-mac", account="acct-1")
        socket = await AsgiSocket(create_app(state), "/v1/connectors", token=host_token).open()
        try:
            assert (await _next_kind(socket, "hello"))["snapshot_chunks"] is True
            for doc in docs:
                await socket.send_text(json.dumps(doc))
            close = await socket.receive()
            assert close["type"] == "websocket.close" and close["code"] == 1008
            assert backend.snapshot("home-mac") is before
        finally:
            await socket.close()
            await source.close()
            backend.close()

    asyncio.run(scenario())


def test_snapshot_deadline_and_relay_capacity_are_explicit() -> None:
    intake = SnapshotIntake()
    transfer = _transfer({"small": "snapshot"})
    intake.accept(transfer[0], "home-mac", 1)
    with pytest.raises(ValueError, match="expired"):
        intake.accept(transfer[1], "home-mac", 11)
    relay = TerminalRelay()
    relay.script("term-1", "e1", b"x" * (1024 * 1024))
    # Over capacity is per terminal and never fatal: nothing is staged, the terminal is marked
    # for a resync, and the staged frames of that terminal are dropped.
    assert relay.script("term-2", "e2", b"extra") == 0
    assert relay.overflowed == {"term-2": "e2"}
    assert sum(len(frame.data) for frame in relay.drain()) == 1024 * 1024
    assert relay.script("term-1", "e1", b"x" * (1024 * 1024)) == 16
    assert relay.script("term-1", "e1", b"more") == 0  # held until its resync is requested
    assert relay.drain() == [] and "term-1" in relay.overflowed


def test_close_reopen_never_reuses_epoch_or_accepts_delayed_output(tmp_path: Path) -> None:
    state, backend, source = _state(tmp_path)
    try:
        first, _ = backend.create_attachment(
            "home-mac", "term-1", "shell", "acct-1", "dev", takeover=False
        )
        backend.close_attachment(first.attachment_id, "acct-1")
        second, _ = backend.create_attachment(
            "home-mac", "term-1", "shell", "acct-1", "dev", takeover=False
        )
        assert second.epoch != first.epoch and second.generation > first.generation
        assert backend.ingest_terminal_output(
            "home-mac", "term-1", first.epoch, b"delayed old bytes", NOW
        ) == {"error": "stale_epoch"}
        assert backend.ingest_terminal_output(
            "home-mac", "term-1", second.epoch, b"fresh bytes", NOW
        ) == {"ok": [0, 11]}
    finally:
        backend.close()


def test_dead_pty_requests_reset_and_recovers_without_replaying_input(tmp_path: Path) -> None:
    async def scenario() -> None:
        state, backend, source = _state(tmp_path)
        first, _ = backend.create_attachment(
            "home-mac", "term-1", "shell", "acct-1", "dev", takeover=False
        )
        sibling, _ = backend.create_attachment(
            "home-mac", "term-2", "shell", "acct-1", "dev", takeover=False
        )
        lease = backend.acquire_control(first.attachment_id, "acct-1", "dev")
        host_token = state.hosts.enroll("home-mac", account="acct-1")
        tunnel = AsgiSocket(create_app(state), "/v1/connectors", token=host_token)

        async def opener() -> TunnelTransport:
            return await tunnel.open()

        link = ConnectorLink(
            LinkConfig(
                "http://synthetic",
                "home-mac",
                host_token,
                journal_path=str(tmp_path / "c.db"),
                max_retries=0,
            ),
            source,
            opener=opener,
        )
        stop = asyncio.Event()
        task = asyncio.create_task(link.run(stop))
        try:
            await _wait(lambda: len(source.created) == 2)
            dead = source.created[0]
            surviving = source.created[1]
            dead.active = False
            await _wait(lambda: len(source.created) == 3)
            assert source._epochs == {"term-1": "e2", "term-2": "e1"}
            assert backend.open_lease(lease.lease_id) is None
            assert source.created[2].writes == [] and surviving.active
            assert backend.get_attachment(sibling.attachment_id) == sibling
            assert any(
                json.loads(text).get("kind") == "terminal.reset_required" for text in tunnel.sent
            )
        finally:
            stop.set()
            await asyncio.wait_for(task, 2)
            await source.close()
            backend.close()

    asyncio.run(scenario())


def test_spool_is_consumed_by_lifespan_and_preserves_sibling(tmp_path: Path) -> None:
    async def scenario() -> None:
        state, backend, source = _state(tmp_path)
        home_token = state.hosts.enroll("home-mac", account="acct-1")
        office_token = state.hosts.enroll("office-mac", account="acct-1")
        state.revoke_spool, state.revoke_poll_s = str(tmp_path / "spool"), 0.01
        app = create_app(state)
        try:
            async with app.router.lifespan_context(app):
                home = await AsgiSocket(app, "/v1/connectors", token=home_token).open()
                office = await AsgiSocket(app, "/v1/connectors", token=office_token).open()
                try:
                    await _next_kind(home, "hello")
                    await _next_kind(office, "hello")
                    write_revoke_request(state.revoke_spool, "home-mac", NOW)
                    close = await home.receive()
                    assert close["code"] == 1008 and close["reason"] == "unauthenticated"
                    assert state.hosts.lookup(home_token) is None
                    assert state.hosts.lookup(office_token) is not None
                    await office.send_text(json.dumps({"kind": "ping"}))
                    assert (await _next_kind(office, "pong"))["kind"] == "pong"
                finally:
                    await home.close()
                    await office.close()
        finally:
            await source.close()
            backend.close()
        assert not state.sockets._sockets

    asyncio.run(scenario())


def test_combined_web_lifespan_runs_gateway_consumer(tmp_path: Path) -> None:
    async def scenario() -> None:
        state, backend, source = _state(tmp_path)
        token = state.hosts.enroll("home-mac", account="acct-1")
        state.revoke_spool, state.revoke_poll_s = str(tmp_path / "spool"), 0.01
        app, _ = build_web_app(
            gateway_state=state,
            gateway_app=create_app(state),
            flet_main=lambda page: None,
            public_origin="https://relaydeck.example.ts.net",
            cookie_secret=b"c2-test-cookie" * 3,
        )
        try:
            async with app.router.lifespan_context(app):
                write_revoke_request(state.revoke_spool, "home-mac", NOW)
                await _wait(lambda: state.hosts.lookup(token) is None)
        finally:
            await source.close()
            backend.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "field,value",
    [
        ("generation", True),
        ("input_seq", False),
        ("generation", 0),
        ("cols", 1025),
        ("epoch", ""),
        ("host_id", "other-host"),
    ],
)
def test_typed_resize_rejects_coercion_and_foreign_identity(field: str, value: Any) -> None:
    doc = {
        "kind": "terminal.resize",
        "host_id": "home-mac",
        "terminal_id": "term-1",
        "epoch": "e1",
        "lease_id": "lease-1",
        "generation": 1,
        "input_seq": 1,
        "cols": 80,
        "rows": 24,
    }
    doc[field] = value
    with pytest.raises(ValueError):
        parse_terminal_input(doc, "home-mac")


def test_host_rotation_fences_old_tunnel_and_foreign_dispatch(tmp_path: Path) -> None:
    async def scenario() -> None:
        state, backend, source = _state(tmp_path)
        old_token = state.hosts.enroll("home-mac", account="acct-1")
        app = create_app(state)
        old = await AsgiSocket(app, "/v1/connectors", token=old_token).open()
        fresh: AsgiSocket | None = None
        try:
            await _next_kind(old, "hello")
            new_token = state.hosts.enroll("home-mac", account="acct-2")
            record = state.hosts.lookup(new_token)
            assert record is not None
            assert await state.sockets.notify_host("home-mac", "must not leak", binding=record) == 0
            backend.submit_command(
                {
                    "protocol": "relaydeck.gateway.v1",
                    "command_id": "old-owner-command",
                    "host_id": "home-mac",
                    "host_epoch": source.epoch,
                    "kind": "agent.prompt",
                    "target": {"agent_id": "term-1"},
                    "payload": {"text": "private draft"},
                    "principal": "acct-1",
                    "device_id": "dev",
                    "expires_ms": NOW + 120000,
                }
            )
            fresh = await AsgiSocket(app, "/v1/connectors", token=new_token).open()
            assert (await _next_kind(fresh, "hello"))["epochs"] == []
            await fresh.send_text(json.dumps({"kind": "ping"}))
            assert (await _next_kind(fresh, "pong"))["kind"] == "pong"
            # Keepalive intake is independent of the journal/outbox lane.
            await _wait(lambda: not backend._outbox.get("home-mac"))
            status = backend.command_status("old-owner-command")
            assert status is not None and status["status"] == "accepted"
            assert backend.drain_outbox("home-mac") == []
        finally:
            await old.close()
            if fresh is not None:
                await fresh.close()
            await source.close()
            backend.close()

    asyncio.run(scenario())


def test_socket_send_and_close_deadlines_release_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    import relaydeck.gateway.sockets as sockets

    monkeypatch.setattr(sockets, "SEND_TIMEOUT_S", 0.02)

    class StalledSocket:
        async def send_text(self, text: str) -> None:
            await asyncio.Event().wait()

        async def close(self, code: int = 1000, reason: str = "") -> None:
            await asyncio.Event().wait()

    async def scenario() -> None:
        registry = SocketRegistry()
        socket = StalledSocket()
        registry.register_host("stalled-host", socket)
        async with asyncio.timeout(0.2):
            assert await registry.notify_host("stalled-host", "one attempt") == 0
            assert await registry.close_host("stalled-host", "closed") == 1
        assert not registry._sockets and not registry._send_locks

    asyncio.run(scenario())


@pytest.mark.parametrize("mode,generation", [("view", 1), ("control", 0)])
def test_view_and_stale_generation_never_reach_connector(
    tmp_path: Path, mode: str, generation: int
) -> None:
    async def scenario() -> None:
        state, backend, source = _state(tmp_path)
        attachment, _ = backend.create_attachment(
            "home-mac", "term-1", "shell", "acct-1", "dev", takeover=False
        )
        acquire = backend.issue_view if mode == "view" else backend.acquire_control
        lease = acquire(attachment.attachment_id, "acct-1", "dev")
        ticket = state.tickets.issue(
            TicketGrant(
                "acct-1", "dev", ALL_CAPS, frozenset({"home-mac"}), lease.lease_id, "home-mac"
            ),
            NOW,
        )
        host_token = state.hosts.enroll("home-mac", account="acct-1")
        app = create_app(state)
        host = await AsgiSocket(app, "/v1/connectors", token=host_token).open()
        viewer = await AsgiSocket(app, f"/v1/terminal/{lease.lease_id}?ticket={ticket}").open()
        try:
            await _next_kind(host, "hello")
            await viewer.send_bytes(
                terminal_wire.encode_resize(lease.lease_id, generation, 1, 80, 24)
            )
            failure = await viewer.receive()
            if mode == "view":
                assert json.loads(failure["text"])["kind"] == "bye"
            else:
                assert json.loads(failure["text"])["detail"] == "stale_revision"
            assert backend.input_logs == {} and backend.resize_log == []
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(host.outbound.get(), 0.06)
        finally:
            await viewer.close()
            await host.close()
            await source.close()
            backend.close()

    asyncio.run(scenario())


def test_async_pty_write_preserves_partial_bytes_without_blocking_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(AttachSession, "running", property(lambda session: True))

    async def scenario() -> None:
        read_fd, write_fd = os.pipe()
        os.set_blocking(read_fd, False)
        os.set_blocking(write_fd, False)
        session = AttachSession(argv=[], _fd=write_fd)
        payload = b"x" * (16 * 1024)
        received = bytearray()

        async def drain() -> None:
            while len(received) < len(payload):
                try:
                    received.extend(os.read(read_fd, 1024))
                except BlockingIOError:
                    pass
                await asyncio.sleep(0.001)

        reader = asyncio.create_task(drain())
        try:
            async with asyncio.timeout(1):
                assert await session.write_async(payload) == len(payload)
                await reader
            assert received == payload
        finally:
            reader.cancel()
            os.close(read_fd)
            os.close(write_fd)
            session._fd = None

    asyncio.run(scenario())
