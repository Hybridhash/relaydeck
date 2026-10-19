"""Actual queued gateway input reports the refusal without inventing a host outage."""

import asyncio
import json

import pytest

from relaydeck.tunnel_queue import TunnelWriter, cancel_tasks
from tests.support.terminal.reader_handoff import _actor, _wait


@pytest.mark.parametrize("reason", ["stale_revision", "input_failed"])
def test_wire_refusal_keeps_actual_identity_or_handoff_reason(monkeypatch, reason):
    async def scenario():
        async with _actor("shell") as ctx:
            state, lease = ctx.state, ctx.lease
            state.hosts.enroll("home-mac", account="acct-1")
            record = state.hosts.record_for_host("home-mac")
            entered, release = asyncio.Event(), asyncio.Event()
            sent, received = [], []
            failures = []
            ctx.actor.controller.on_input_failure = lambda generation, seq: failures.append(
                (generation, seq)
            )

            class Host:
                async def send_text(self, text):
                    if text == "held":
                        entered.set()
                        await release.wait()
                    sent.append(text)

            host = Host()
            state.sockets.register_host("home-mac", host, record)
            writer = TunnelWriter(
                host.send_text, lambda: state.sockets.host_current("home-mac", host)
            )
            state.sockets.bind_writer(host, writer)
            runner = asyncio.create_task(writer.run())
            original = state.sockets.send_text

            async def observe(socket, text):
                doc = json.loads(text)
                if doc.get("kind") in ("gap", "bye"):
                    received.append(doc)
                await original(socket, text)

            monkeypatch.setattr(state.sockets, "send_text", observe)
            held = writer.post(("held",))
            try:
                await entered.wait()
                await ctx.actor.controller._client.send_input(b"withheld")
                await _wait(lambda: writer.inbox.count == 2)
                if reason == "stale_revision":
                    ctx.backend.close_lease(lease.lease_id)
                else:
                    assert ctx.backend.begin_reader_handoff(
                        lease.lease_id, "acct-1", "dev-1", lease.generation
                    )
                release.set()
                assert await held
                await _wait(lambda: bool(received))
                assert received[0]["kind"] == "gap" and received[0]["detail"] == reason
                if reason == "input_failed":
                    assert (
                        received[0]["generation"] == lease.generation
                        and received[0]["input_seq"] == 1
                    )
                    await _wait(lambda: bool(failures))
                    assert failures == [(lease.generation, 1)]
                    assert ctx.actor.controller.gap_recovery.detail == "input_failed"
                assert sent == ["held"]  # No queued input crosses the refused wire attempt.
                assert all(d.get("detail") != "host_offline" for d in received)
            finally:
                release.set()
                state.sockets.unregister(host)
                await cancel_tasks([runner])

    asyncio.run(scenario())
