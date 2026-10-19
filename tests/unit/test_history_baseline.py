"""Live-edge resets cannot bypass continuity, native acknowledgement or retry bounds."""

import asyncio
import json

import pytest

from relaydeck.client.features.terminal.controller import TerminalController
from tests.support.scripted_sessions import ScriptedTerminalSocket


@pytest.mark.parametrize("kind", ["missing", "epoch", "unsolicited"])
def test_bad_baseline_never_changes_cursor_or_releases_input(kind):
    async def scenario():
        socket = ScriptedTerminalSocket()
        controller = TerminalController(lambda _: socket, "lease", 1, "control", lambda _: None)
        await controller.start("ticket", "e1", 10)
        controller.fence_ui_input(True)
        controller._client.reset_to_live = kind != "unsolicited"
        doc = {"kind": "history_reset", "epoch": "e1", "offset": 100, "incomplete": True}
        if kind == "missing":
            del doc["incomplete"]
        elif kind == "epoch":
            doc["epoch"] = "foreign"
        socket.queue(("text", json.dumps(doc)))
        await asyncio.wait_for(controller.run(), 1)
        assert controller._client.next_offset == 10 and controller.ui_input_fenced
        assert not socket.sent
        await controller.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("mode", ["control", "view"])
def test_live_edge_budget_refills_only_after_thirty_seconds_stable_output(monkeypatch, mode):
    async def scenario():
        now = [100.0]
        monkeypatch.setattr(
            "relaydeck.client.features.terminal.controller.time.monotonic", lambda: now[0]
        )
        socket = ScriptedTerminalSocket()
        controller = TerminalController(lambda _: socket, "lease", 1, mode, lambda _: None)
        controller.on_history_reset = lambda: asyncio.sleep(0, result=True)
        await controller.start("ticket", "e1", 0)
        for offset in range(3):
            controller.on_viewer_overflow()
            assert controller.begin_live_edge_recovery()
            assert await controller._handle_baseline(
                {"epoch": "e1", "offset": offset, "incomplete": True}
            )
        assert controller._overflow_attempts == 3
        controller._client.epoch = "e1"
        controller._handle_output(2, b"a")
        now[0] += 29.9
        controller._handle_output(3, b"b")
        assert controller._overflow_attempts == 3
        now[0] += 0.1
        controller._handle_output(4, b"c")
        assert controller._overflow_attempts == 0
        controller.on_viewer_overflow()
        assert controller.begin_live_edge_recovery()
        assert controller.gap_recovery.attempt == 0 and controller.gap_recovery.detail != "overflow"
        assert not socket.sent
        await controller.close()

    asyncio.run(scenario())


def test_repeated_live_edge_recovery_is_finite_and_explicit_retry_resets_budget():
    async def scenario():
        socket = ScriptedTerminalSocket()
        controller = TerminalController(lambda _: socket, "lease", 1, "view", lambda _: None)

        async def acknowledged():
            return True

        controller.on_history_reset = acknowledged
        await controller.start("ticket", "e1", 0)
        for offset in range(3):
            controller.on_viewer_overflow()
            assert controller.live_edge_pending and controller.ui_input_fenced
            assert controller.begin_live_edge_recovery()
            assert not controller.begin_live_edge_recovery()
            assert await controller._handle_baseline(
                {"epoch": "e1", "offset": offset, "incomplete": True}
            )
        controller.on_viewer_overflow()
        assert controller.live_edge_pending and controller.ui_input_fenced
        assert "Retry explicitly" in controller.state.notice.text
        controller.reset_gap_recovery()
        controller.on_viewer_overflow()
        assert controller.begin_live_edge_recovery()
        assert controller.mode == "view" and not socket.sent
        await controller.close()

    asyncio.run(scenario())
