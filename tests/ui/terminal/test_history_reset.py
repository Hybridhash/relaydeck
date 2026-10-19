"""Current native reset acknowledgement gates the preserved lease's input."""

import asyncio

import pytest
from relaydeck_terminal import protocol

from tests.support.host_harness import run
from tests.support.scripted_sessions import make_scripted_sessions
from tests.support.terminal.resume import _hello, _open_term, _report


@pytest.mark.parametrize("kind", ["agent", "shell"])
@pytest.mark.parametrize("invalidate", [False, True])
def test_history_reset_waits_for_real_bridge_ack_without_new_terminal_or_ownership(
    kind, invalidate
):
    async def scenario():
        scripted = make_scripted_sessions()
        if kind == "agent":
            session = scripted.factory.make_agent(
                host_id="mac", host_epoch="e1", agent_id="a", target="a"
            )
            session.open()
            await session.confirm_takeover()
            session.on_draft_change("unsent exact draft")
        else:
            session = scripted.factory.make_shell(host_id="mac")
            await session.open("shell")
        app, term, conn, channel = await _open_term()
        channel.deliver(_hello())
        await app.settle()
        session.bind_view(term)
        session._handle_bytes(b"old history")
        controller, lease, attachment = session.controller, session.lease, session.attachment
        before = len(scripted.ops.attaches), len(scripted.ops.leases)
        controller.on_viewer_overflow()
        task = asyncio.create_task(
            controller._handle_baseline(
                {
                    "kind": "history_reset",
                    "epoch": "e1",
                    "offset": 100,
                    "incomplete": True,
                }
            )
        )
        try:
            async with asyncio.timeout(2):
                while not any(p[0] == protocol.RESUME_PROBE for _, p in conn.data_frames):
                    await asyncio.sleep(0)
            assert controller.ui_input_fenced and controller.key_generation() is None
            assert term._reset_count == 1 and app.find("r1b-term") is term
            if invalidate:
                controller.invalidate_writer("lease_expired")
            channel.deliver(_report(1, resets=1, applied=0))
            assert await task is (not invalidate)
            assert bytes(session.output_log) == (b"old history" if invalidate else b"")
            assert controller.ui_input_fenced is invalidate
            assert session.controller is controller and session.lease is lease
            assert session.attachment is attachment and app.find("r1b-term") is term
            assert (len(scripted.ops.attaches), len(scripted.ops.leases)) == before
            if kind == "agent":
                assert session.store.draft == "unsent exact draft"
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await session.dispose()

    run(scenario())
