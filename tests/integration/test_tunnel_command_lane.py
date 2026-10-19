"""Real connector handler: journal isolation, reply order and retirement fences."""

from __future__ import annotations

import asyncio
import base64
import json
import threading
from contextlib import suppress

import pytest

from relaydeck.gateway import app as gateway_module
from relaydeck.gateway.app import GatewayState, create_app
from relaydeck.gateway.production import JournalBackend
from relaydeck.journal import CommandJournal
from relaydeck.tunnel_queue import BoundedInbox
from tests.support.asgi_socket import AsgiSocket

NOW = 1_000_000


def _seed(tmp_path, ids=("held",)):
    journal = CommandJournal(tmp_path / "commands.db", clock=lambda: NOW)
    backend = JournalBackend(journal, now_ms=lambda: NOW)
    for command_id in ids:
        journal.submit(
            command_id=command_id,
            principal="acct-1",
            host_id="home-mac",
            kind="agent.start",
            target={"workspace_id": "w1"},
            payload={"kind": "tool-a", "name": "test"},
            now=NOW,
        )
        assert journal.mark_dispatched(command_id, now=NOW)
    state = GatewayState(backend=backend, cursor_secret=b"x" * 32, now_ms=lambda: NOW)
    return state, backend, journal


def _reply(kind="command.ack", command_id="held", host="home-mac"):
    return {
        "kind": kind,
        "host_id": host,
        "command_id": command_id,
        **({"status": "succeeded"} if kind == "command.result" else {}),
    }


async def _send(socket, doc):
    # Do not retain the load generator's whole traffic history in AsgiSocket.sent.
    await socket.inbound.put({"type": "websocket.receive", "text": json.dumps(doc)})


async def _open(state, token):
    socket = await AsgiSocket(create_app(state), "/v1/connectors", token=token).open()
    assert json.loads(await socket.recv_text())["kind"] == "hello"
    return socket


def _hold(monkeypatch, backend, operation, entered, release):
    loop = asyncio.get_running_loop()
    original = getattr(backend, operation)

    def held(*args):
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(30), "test did not release the journal worker"
        return original(*args)

    monkeypatch.setattr(backend, operation, held)


@pytest.mark.parametrize("payload_size", [8192, 16384, 32768])
@pytest.mark.parametrize(
    ("operation", "wait_s"),
    [("command_owner", 0.1), ("note_command_ack", 1.0), ("note_command_result", 5.0)],
)
def test_output_and_keepalive_progress_during_real_journal_wait(
    monkeypatch, tmp_path, payload_size, operation, wait_s
):
    async def scenario():
        state, backend, journal = _seed(tmp_path)
        token = state.hosts.enroll("home-mac", account="acct-1")
        entered, release = asyncio.Event(), threading.Event()
        _hold(monkeypatch, backend, operation, entered, release)
        frames = []
        attachments = []
        for agent in range(14):
            terminal_id = f"agent-{agent}"
            attachment, _ = backend.ownership.attach(
                host_id="home-mac",
                terminal_id=terminal_id,
                kind="agent",
                account="acct-1",
                device_id="device",
                takeover=True,
                owner_label="Test",
                now_ms=NOW,
            )
            backend.attach_index[("home-mac", terminal_id)] = attachment.attachment_id
            attachments.append(attachment)
            frames.append(
                json.dumps(
                    {
                        "kind": "terminal.output",
                        "host_id": "home-mac",
                        "terminal_id": terminal_id,
                        "epoch": attachment.epoch,
                        "data_b64": base64.b64encode(bytes([65 + agent]) * payload_size).decode(),
                    }
                )
            )
        socket = await _open(state, token)
        previous_offsets = [0] * len(attachments)
        try:
            kind = "command.result" if operation == "note_command_result" else "command.ack"
            await _send(socket, _reply(kind))
            await asyncio.wait_for(entered.wait(), 1)
            ticks = round(wait_s / 0.005)
            loop = asyncio.get_running_loop()
            started = loop.time()
            for tick in range(ticks):
                for frame in frames:
                    await socket.inbound.put({"type": "websocket.receive", "text": frame})
                await asyncio.sleep(max(0, started + (tick + 1) * 0.005 - loop.time()))
                if tick % 10 == 0 or tick == ticks - 1:
                    await _send(socket, {"kind": "ping"})
                    assert json.loads((await socket.receive())["text"]) == {"kind": "pong"}
                    # Actual ring advancement while the real SQL worker is held;
                    # a pong alone is not an ingestion barrier.
                    for index, attachment in enumerate(attachments):
                        fanout = backend.rings.get(attachment.attachment_id)
                        assert fanout is not None
                        assert fanout.ring.end_offset > previous_offsets[index]
                        previous_offsets[index] = fanout.ring.end_offset
                assert not release.is_set() and not socket.task.done()
            async with asyncio.timeout(1):
                while any(
                    backend.rings.get(att.attachment_id).ring.end_offset != ticks * payload_size
                    for att in attachments
                ):
                    await asyncio.sleep(0)
            assert journal.record("held")["status"] == "dispatched"
        finally:
            release.set()
            await socket.close()
            journal.close()

    asyncio.run(scenario())


def test_replies_remain_fifo_and_graceful_bye_drains_results(monkeypatch, tmp_path):
    async def scenario():
        state, backend, journal = _seed(tmp_path, ("first", "second"))
        token = state.hosts.enroll("home-mac", account="acct-1")
        entered, release = asyncio.Event(), threading.Event()
        _hold(monkeypatch, backend, "command_owner", entered, release)
        observed = []
        for operation in ("note_command_ack", "note_command_result"):
            original = getattr(backend, operation)

            def recording(*args, original=original, operation=operation):
                observed.append((operation, args[0]))
                return original(*args)

            monkeypatch.setattr(backend, operation, recording)
        socket = await _open(state, token)
        try:
            for command_id in ("first", "second"):
                for kind in ("command.ack", "command.result"):
                    await _send(socket, _reply(kind, command_id))
            await asyncio.wait_for(entered.wait(), 1)
            await _send(socket, {"kind": "bye"})
            await asyncio.sleep(0)
            assert not socket.task.done() and not observed
            release.set()
            await asyncio.wait_for(socket.task, 2)
            assert observed == [
                (operation, command_id)
                for command_id in ("first", "second")
                for operation in ("note_command_ack", "note_command_result")
            ]
            assert [journal.record(cid)["status"] for cid in ("first", "second")] == [
                "succeeded",
                "succeeded",
            ]
        finally:
            release.set()
            await socket.close()
            journal.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("invalidate", ["replace", "revoke"])
def test_owner_wait_cannot_mutate_after_captured_tunnel_is_invalidated(
    monkeypatch, tmp_path, invalidate
):
    async def scenario():
        state, backend, journal = _seed(tmp_path)
        token = state.hosts.enroll("home-mac", account="acct-1")
        entered, release = asyncio.Event(), threading.Event()
        _hold(monkeypatch, backend, "command_owner", entered, release)
        old = await _open(state, token)
        replacement = None
        try:
            await _send(old, _reply())
            await asyncio.wait_for(entered.wait(), 1)
            if invalidate == "replace":
                replacement = await _open(state, token)
                await _send(replacement, {"kind": "ping"})
                assert json.loads((await replacement.receive())["text"]) == {"kind": "pong"}
            else:
                state.hosts.revoke("home-mac")
                await _send(old, {"kind": "ping"})
            release.set()
            await asyncio.wait_for(old.task, 2)
            assert journal.record("held")["status"] == "dispatched"
            if replacement:
                await _send(replacement, {"kind": "ping"})
                assert json.loads((await replacement.receive())["text"]) == {"kind": "pong"}
        finally:
            release.set()
            await old.close()
            if replacement:
                await replacement.close()
            journal.close()

    asyncio.run(scenario())


def test_cancelled_tunnel_settles_started_ack_and_drops_unstarted_result(monkeypatch, tmp_path):
    async def scenario():
        state, backend, journal = _seed(tmp_path)
        token = state.hosts.enroll("home-mac", account="acct-1")
        entered, release = asyncio.Event(), threading.Event()
        _hold(monkeypatch, backend, "note_command_ack", entered, release)
        socket = await _open(state, token)
        try:
            await _send(socket, _reply())
            await asyncio.wait_for(entered.wait(), 1)
            await _send(socket, _reply("command.result"))
            socket.task.cancel()
            async with asyncio.timeout(1):
                while state.sockets.host_count("home-mac"):
                    await asyncio.sleep(0)
            assert not socket.task.done()
            release.set()
            with suppress(asyncio.CancelledError):
                await asyncio.wait_for(socket.task, 2)
            assert journal.record("held")["status"] == "acked"
        finally:
            release.set()
            await socket.close()
            journal.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("foreign", ["host", "account"])
def test_reply_authorization_is_captured_not_account_wide(monkeypatch, tmp_path, foreign):
    async def scenario():
        state, backend, journal = _seed(tmp_path)
        token = state.hosts.enroll("home-mac", account="acct-1")
        if foreign == "account":
            monkeypatch.setattr(backend, "command_owner", lambda _: ("other", "home-mac"))
        socket = await _open(state, token)
        try:
            await _send(socket, _reply(host="other" if foreign == "host" else "home-mac"))
            assert (await socket.receive())["reason"] == "forbidden"
            assert journal.record("held")["status"] == "dispatched"
        finally:
            await socket.close()
            journal.close()

    asyncio.run(scenario())


def test_command_and_snapshot_admissions_share_encoded_byte_budget(monkeypatch, tmp_path):
    async def scenario():
        state, backend, journal = _seed(tmp_path)
        token = state.hosts.enroll("home-mac", account="acct-1")
        entered, release = asyncio.Event(), threading.Event()
        _hold(monkeypatch, backend, "command_owner", entered, release)
        monkeypatch.setattr(gateway_module, "BoundedInbox", lambda: BoundedInbox(byte_limit=1024))
        socket = await _open(state, token)
        try:
            await _send(socket, {**_reply(), "padding": "x" * 700})
            await asyncio.wait_for(entered.wait(), 1)
            await _send(
                socket,
                {
                    "kind": "snapshot.begin",
                    "host_id": "home-mac",
                    "transfer_id": "test",
                    "total_bytes": 100,
                    "total_chunks": 1,
                    "sha256": "a" * 64,
                    "padding": "x" * 200,
                },
            )
            assert (await socket.receive())["reason"] == "tunnel_queue_full"
        finally:
            release.set()
            await socket.close()
            journal.close()

    asyncio.run(scenario())


def test_command_lane_does_not_bypass_snapshot_interleaving_guard(tmp_path):
    async def scenario():
        state, _, journal = _seed(tmp_path)
        token = state.hosts.enroll("home-mac", account="acct-1")
        socket = await _open(state, token)
        try:
            await _send(
                socket,
                {
                    "kind": "snapshot.begin",
                    "host_id": "home-mac",
                    "transfer_id": "test",
                    "total_bytes": 100,
                    "total_chunks": 1,
                    "sha256": "a" * 64,
                },
            )
            await _send(socket, _reply())
            assert (await socket.receive())["reason"] == "forbidden"
            assert journal.record("held")["status"] == "dispatched"
        finally:
            await socket.close()
            journal.close()

    asyncio.run(scenario())
