"""disposable child/PTY, stderr isolation and secret-free exit log."""

import asyncio
import json
import logging
import os
import signal
import sys

import pytest

from relaydeck.connector.attach import AttachSession
from relaydeck.connector.herdr import HerdrAdapter
from relaydeck.connector.source import HerdrSource


def source_for(session):
    source = HerdrSource(HerdrAdapter(socket_path="/private/tmp/unused-stuck.sock"))
    source._epochs = {"synthetic": "e1"}
    source._epoch_kinds = {"synthetic": "agent"}
    source._epoch_modes = {"synthetic": "control"}
    source._attached["agent:synthetic"] = (session, "synthetic", "")
    source._pool.acquire("agent:synthetic", lambda: session, source._now_ms())
    return source


@pytest.mark.parametrize(
    "code,category", [("terminal_owned", "terminal_owned"), ("unknown", "diagnostic_output")]
)
def test_child_stderr_is_not_output_and_death_log_is_secret_free(caplog, code, category):
    async def scenario():
        marker = "BEARER_GRANT_COOKIE_PRIVATE_PATH"
        diagnostic = json.dumps({"error": {"code": code, "message": marker}})
        script = (
            "import os; os.write(1,b'output'); "
            f"os.write(2,{diagnostic.encode()!r}); raise SystemExit(1)"
        )
        session = AttachSession([sys.executable, "-c", script], capture_stderr=True)
        session.spawn()
        source = source_for(session)
        try:
            async with asyncio.timeout(2):
                while source._attached:
                    await source.wait_output_ready()
                    source.pump_attached(lambda _: "e1")
            assert source.drain_terminal_resets() == {"synthetic": "e1"}
            assert session.exit_status == 1 and session._fd is None
            assert session.diagnostic_fileno() is None
            assert len(session._stderr_prefix) <= 4096
            frames = source._relay.drain()
            assert b"".join(frame.data for frame in frames) == b"output"
            assert marker not in caplog.text and diagnostic not in caplog.text
            assert f"exit_code=1 signal=None stderr_category={category}" in caplog.text
            assert "mode=control recovery=explicit_takeover" in caplog.text
        finally:
            await source.close()
            session.close()

    with caplog.at_level(logging.WARNING):
        asyncio.run(scenario())


def test_stderr_only_flood_wakes_drains_without_blocking_or_becoming_output():
    async def scenario():
        script = "import os; os.write(2,b'x'*262144); os.write(1,b'finished')"
        session = AttachSession([sys.executable, "-c", script], capture_stderr=True)
        session.spawn()
        source = source_for(session)
        try:
            async with asyncio.timeout(2):
                while source._attached:
                    await source.wait_output_ready()
                    source.pump_attached(lambda _: "e1")
            assert session.exit_status == 0 and len(session._stderr_prefix) == 4096
            assert session.collect_diagnostics() == "diagnostic_output"
            assert b"".join(frame.data for frame in source._relay.drain()) == b"finished"
        finally:
            await source.close()
            session.close()

    asyncio.run(scenario())


def test_empty_closed_stderr_does_not_spin_readiness_and_signal_is_numeric(caplog):
    async def scenario():
        session = AttachSession(
            [sys.executable, "-c", "import os,time; os.close(2); time.sleep(60)"],
            capture_stderr=True,
        )
        session.spawn()
        source = source_for(session)
        try:
            await asyncio.wait_for(source.wait_output_ready(), 2)
            source.pump_attached(lambda _: "e1")
            assert session.diagnostic_fileno() is None
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(source.wait_output_ready(), 0.03)
            # Signal only this disposable child; no Herdr/service process.
            os.kill(session._proc.pid, signal.SIGTERM)
            async with asyncio.timeout(2):
                while source._attached:
                    await source.wait_output_ready()
                    source.pump_attached(lambda _: "e1")
            assert (
                f"exit_code=None signal={int(signal.SIGTERM)} stderr_category=empty" in caplog.text
            )
        finally:
            await source.close()
            session.close()

    with caplog.at_level(logging.WARNING):
        asyncio.run(scenario())
