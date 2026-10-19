"""Queued host work keeps bounded reservations and checks authority at the wire."""

import asyncio

import pytest

from relaydeck.gateway.backend import BackendError
from relaydeck.gateway.production import JournalBackend
from relaydeck.gateway.sockets import SocketRegistry
from relaydeck.journal import CommandJournal
from relaydeck.tunnel_queue import TunnelWriter, cancel_tasks


def test_host_input_rechecks_authority_after_waiting_for_writer():
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        allowed = [True]
        sent = []

        class Socket:
            async def send_text(self, text):
                entered.set()
                await release.wait()
                sent.append(text)

        async def current():
            return allowed[0]

        socket, registry = Socket(), SocketRegistry()
        registry.register_host("host", socket)
        writer = TunnelWriter(socket.send_text, lambda: registry.host_current("host", socket))
        registry.bind_writer(socket, writer)
        runner = asyncio.create_task(writer.run())
        held = writer.post(("held",))
        await entered.wait()
        queued = asyncio.create_task(registry.notify_host("host", "input", before=current))
        await asyncio.sleep(0)
        allowed[0] = False
        release.set()
        assert await held and await queued == 0
        assert sent == ["held"]
        registry.unregister(socket)
        await cancel_tasks([runner])

    asyncio.run(scenario())


def test_taken_commands_keep_their_outbox_slot_until_restored_or_consumed(tmp_path, monkeypatch):
    monkeypatch.setattr("relaydeck.gateway.production.MAX_OUTBOX_PER_HOST", 1)
    backend = JournalBackend(CommandJournal(tmp_path / "commands.db"), now_ms=lambda: 100)
    row = {
        "status": "accepted",
        "host_id": "host",
        "principal": "account",
        "kind": "shell.create",
        "host_epoch": "e1",
        "target": {"workspace_id": "space"},
        "payload": {},
        "digest": "d",
        "generation": 0,
        "expires_ms": 200,
    }
    try:
        backend.enqueue_record("command", row)
        taken = backend.take_outbox("host", reserve=True)
        assert not backend._outbox.get("host")
        backend.enqueue_record("command", row)  # accepted resubmission is not a second intent
        assert not backend._outbox.get("host")
        with pytest.raises(BackendError, match="outbox full"):
            backend.prepare_command({"host_id": "host"}, None)
        backend.restore_outbox("host", taken, taken)
        assert len(backend._outbox["host"]) == 1 and not backend._reserved_outbox
        assert backend.take_outbox("host") == taken  # public drain does not reserve
        backend.prepare_command({"host_id": "host"}, None)
        assert not backend._reserved_outbox
    finally:
        backend.close()
