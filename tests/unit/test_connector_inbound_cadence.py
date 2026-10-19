"""Ordered keystrokes survive command waits, with a distinct bounded-admission failure."""

import asyncio
import base64
import json

import pytest

from relaydeck.connector.fixture import FixtureAdapter
from relaydeck.connector.link import ConnectorLink, LinkConfig, _ProtocolDrop
from relaydeck.tunnel_queue import BoundedInbox
from tests.support.creation.command_journal import _dispatch_msg
from tests.support.sync.scripted_transport import _ScriptedTransport


@pytest.mark.parametrize("full", [False, True])
def test_connector_key_intake_is_byte_bounded_and_does_not_drop_at_36_keys(
    tmp_path, monkeypatch, full
):
    async def scenario():
        inbound = asyncio.Queue()
        entered, release, pong = asyncio.Event(), asyncio.Event(), asyncio.Event()
        writes = []

        class Transport(_ScriptedTransport):
            reason = ""

            async def recv_text(self):
                raw = await inbound.get()
                if json.loads(raw)["kind"] == "pong":
                    pong.set()
                return raw

            async def close_queue_full(self):
                self.reason = "tunnel_queue_full"
                await self.close()

        class Source(FixtureAdapter):
            async def submit_terminal_input(self, terminal, epoch, kind, data):
                writes.append(data)

        if full:
            monkeypatch.setattr(
                "relaydeck.connector.link.BoundedInbox", lambda: BoundedInbox(byte_limit=1024)
            )
        transport = Transport([])
        link = ConnectorLink(
            LinkConfig("http://localhost", "home-mac", "synthetic", str(tmp_path / "c.db")),
            Source(),
        )
        link._last_epochs = {"term": "e1"}

        async def dispatch(message):
            entered.set()
            await release.wait()
            return {"kind": "command.result", "command_id": message["command_id"]}

        link._dispatch.dispatch = dispatch
        task = asyncio.create_task(link._serve_messages(transport, asyncio.Event()))
        try:
            command = _dispatch_msg("held", "shell.create", {"workspace_id": "w1"}, {})
            command["host_epoch"] = "e1"
            inbound.put_nowait(json.dumps(command))
            await asyncio.wait_for(entered.wait(), 2)
            count = 20 if full else 2800
            for seq in range(1, count + 1):
                inbound.put_nowait(
                    json.dumps(
                        {
                            "kind": "terminal.input",
                            "host_id": "home-mac",
                            "terminal_id": "term",
                            "epoch": "e1",
                            "lease_id": "lease",
                            "generation": 1,
                            "input_seq": seq,
                            "input_kind": "text",
                            "data_b64": base64.b64encode(b"a").decode(),
                        }
                    )
                )
            if full:
                with pytest.raises(_ProtocolDrop, match="tunnel_queue_full"):
                    await asyncio.wait_for(task, 2)
                assert transport.reason == "tunnel_queue_full" and writes == []
            else:
                inbound.put_nowait(json.dumps({"kind": "pong"}))
                await asyncio.wait_for(pong.wait(), 2)
                await asyncio.sleep(0.1)
                assert not task.done() and not writes
                release.set()
                async with asyncio.timeout(2):
                    while len(writes) < count:
                        await asyncio.sleep(0)
                assert not task.done() and writes == [b"a"] * count
        finally:
            release.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await link.close()

    asyncio.run(scenario())
