"""Keepalive and authority changes pass blocked ordered work without input replay."""

import asyncio
import base64
import json

import pytest

from relaydeck.connector.fixture import FixtureAdapter
from relaydeck.connector.link import ConnectorLink, LinkConfig
from relaydeck.journal import digest_for
from tests.support.sync.scripted_transport import _ScriptedTransport


@pytest.mark.parametrize("held", ["dispatch", "input"])
def test_reader_processes_pong_and_fences_queued_input_while_worker_waits(tmp_path, held):
    async def scenario():
        inbound = asyncio.Queue()
        entered, release, received = asyncio.Event(), asyncio.Event(), asyncio.Event()
        writes = []

        class Transport(_ScriptedTransport):
            async def recv_text(self):
                message = await inbound.get()
                if json.loads(message).get("kind") == "pong":
                    received.set()
                return message

        class Source(FixtureAdapter):
            async def submit_terminal_input(self, terminal, epoch, kind, data):
                writes.append(data)
                entered.set()
                await release.wait()

        source, transport = Source(), Transport([])
        link = ConnectorLink(
            LinkConfig("http://localhost", "h1", "synthetic", str(tmp_path / "c.db")), source
        )
        link._last_epochs = {"term": "e1"}

        async def dispatch(message):
            entered.set()
            await release.wait()
            return {"kind": "command.result", "command_id": message["command_id"]}

        link._dispatch.dispatch = dispatch
        input_doc = {
            "kind": "terminal.input",
            "host_id": "h1",
            "terminal_id": "term",
            "epoch": "e1",
            "lease_id": "lease",
            "generation": 1,
            "input_seq": 1,
            "input_kind": "text",
            "data_b64": base64.b64encode(b"once").decode(),
        }
        command = {
            "kind": "command.dispatch",
            "principal": "acct",
            "host_epoch": "e1",
            "generation": 0,
            "expires_ms": 0,
            "command_id": "cmd",
            "host_id": "h1",
            "gateway_kind": "shell.create",
            "target": {"workspace_id": "w1"},
            "payload": {},
            "digest": digest_for("h1", "shell.create", {"workspace_id": "w1"}, {}),
        }
        task = asyncio.create_task(link._serve_messages(transport, asyncio.Event()))
        try:
            inbound.put_nowait(json.dumps(command if held == "dispatch" else input_doc))
            await asyncio.wait_for(entered.wait(), 1)
            inbound.put_nowait(json.dumps(input_doc))
            inbound.put_nowait(
                json.dumps(
                    {
                        "kind": "terminal.epochs",
                        "host_id": "h1",
                        "epochs": [
                            {
                                "terminal_id": "term",
                                "epoch": "e2",
                                "kind": "shell",
                                "mode": "observe",
                            }
                        ],
                    }
                )
            )
            inbound.put_nowait(json.dumps({"kind": "pong"}))
            await asyncio.wait_for(received.wait(), 1)
            assert link._last_epochs == {"term": "e2"}
            assert link._last_application_pong_at is not None and not release.is_set()
            release.set()
            async with asyncio.timeout(1):
                while not any(
                    json.loads(t).get("kind") == "terminal.input_failed" for t in transport.sent
                ):
                    await asyncio.sleep(0)
            assert not task.done()
            assert writes == ([b"once"] if held == "input" else [])
            kinds = [json.loads(text)["kind"] for text in transport.sent]
            if held == "dispatch":
                assert kinds[:2] == ["command.ack", "command.result"]
                assert "terminal.input_failed" in kinds[2:]
            else:
                assert kinds == ["terminal.input_failed"]
        finally:
            release.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await link.close()

    asyncio.run(scenario())


def test_partial_pty_write_cannot_continue_into_replacement_descriptor(monkeypatch):
    from types import SimpleNamespace

    from relaydeck.connector.attach import AttachError, AttachSession

    async def scenario():
        session = AttachSession(["synthetic"])
        session._fd, session._proc = 7, SimpleNamespace(poll=lambda: None)
        entered = asyncio.Event()
        writes = []

        def write(fd, data):
            writes.append((fd, bytes(data)))
            if len(writes) == 1:
                return 1
            entered.set()
            raise BlockingIOError

        monkeypatch.setattr("relaydeck.connector.attach.os.write", write)
        task = asyncio.create_task(session.write_async(b"abc"))
        await entered.wait()
        session._fd = 8
        session._lifecycle_revision += 1
        with pytest.raises(AttachError, match="attachment replaced"):
            await task
        assert writes == [(7, b"abc"), (7, b"bc")]

    asyncio.run(scenario())
