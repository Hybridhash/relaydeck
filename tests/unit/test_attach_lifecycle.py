"""A blocked spawn/reap cannot block the loop or publish a retired attachment."""

import asyncio
import threading

import pytest

from relaydeck.connector.attach import AttachSession
from relaydeck.connector.attach_lifecycle import AttachLifecycle


@pytest.mark.parametrize("retire_during_spawn", [False, True])
def test_spawn_isolated_and_retired_result_never_published(monkeypatch, retire_during_spawn):
    async def scenario():
        started, release = threading.Event(), threading.Event()
        calls = []

        class Process:
            stderr = None
            returncode = None

            def poll(self):
                return self.returncode

            def terminate(self):
                calls.append("terminate")
                self.returncode = 0

            def wait(self, timeout):
                calls.append("wait")
                return self.returncode

        def spawn(*args, **kwargs):
            with pytest.raises(RuntimeError, match="no running event loop"):
                asyncio.get_running_loop()
            calls.append("spawn")
            started.set()
            assert release.wait(2)
            return Process()

        monkeypatch.setattr("relaydeck.connector.attach.subprocess.Popen", spawn)
        wakes = []
        lane = AttachLifecycle(lambda: wakes.append(True))
        session = AttachSession(["synthetic"], lifecycle=lane)
        try:
            session.spawn()
            assert session.spawn_pending and session._proc is None
            assert await asyncio.to_thread(started.wait, 1)
            # This continuation executes while Popen is still blocked in its worker.
            assert session._fd is None and calls == ["spawn"]
            if retire_during_spawn:
                session.close()
            release.set()
            await asyncio.wait_for(lane.idle(), 2)
            assert not session.spawn_pending
            if retire_during_spawn:
                assert session._proc is None and session._fd is None
                assert calls == ["spawn", "terminate", "wait"]
            else:
                assert session._proc is not None and session.fileno() >= 0
                session.close()
                await lane.idle()
                assert calls == ["spawn", "terminate", "wait"]
            assert wakes
        finally:
            release.set()
            session.close()
            await lane.close()

    asyncio.run(scenario())


def test_ready_target_input_does_not_wait_for_another_terminal_reap(monkeypatch):
    from types import SimpleNamespace

    from relaydeck.connector.source import HerdrSource

    async def scenario():
        entered, release = threading.Event(), threading.Event()
        lane = AttachLifecycle(lambda: None)
        writes = []
        source = HerdrSource(SimpleNamespace())
        ready = AttachSession(["ready"], lifecycle=lane)
        ready._proc = SimpleNamespace(poll=lambda: None)
        source._epochs = {"a": "e1"}
        source._epoch_kinds = {"a": "shell"}
        source._attached = {"shell:a": (ready, "a", "shell")}

        async def write(self, data):
            writes.append(data)

        def reap():
            entered.set()
            assert release.wait(2)

        monkeypatch.setattr(AttachSession, "write_async", write)

        async def close_other():
            from relaydeck.offload import run_blocking

            await run_blocking(reap)

        lane._admit(close_other, close=True)
        try:
            assert await asyncio.to_thread(entered.wait, 1)
            await asyncio.wait_for(source.wait_attachments("a", "e1"), 0.2)
            assert await source.submit_terminal_input("a", "e1", "text", b"once") is None
            assert writes == [b"once"] and not release.is_set()
            ready.spawn_pending, ready._spawn_done = True, asyncio.Event()
            pending = asyncio.create_task(source.wait_attachments("a", "e1"))
            await asyncio.sleep(0)
            assert not pending.done()
            ready._spawn_done.set()
            await pending
        finally:
            release.set()
            await lane.close()

    asyncio.run(scenario())


def test_reap_completes_before_replacement_spawn_without_blocking_loop(monkeypatch):
    async def scenario():
        entered, release = threading.Event(), threading.Event()
        calls = []

        def spawn(session):
            if session.lifecycle is not None:
                session.lifecycle.spawn(session)
                return
            calls.append("spawn")

        class Process:
            stderr = None
            returncode = None

            def poll(self):
                return None

            def terminate(self):
                calls.append("terminate")

            def wait(self, timeout):
                entered.set()
                assert release.wait(2)
                calls.append("wait")
                self.returncode = 0

        monkeypatch.setattr(AttachSession, "spawn", spawn)
        lane = AttachLifecycle(lambda: None)
        old = AttachSession(["synthetic"], lifecycle=lane)
        old._proc = Process()
        replacement = AttachSession(["synthetic"], lifecycle=lane)
        try:
            old.close()
            replacement.spawn()
            assert old._proc is None
            assert await asyncio.to_thread(entered.wait, 1)
            assert calls == ["terminate"] and replacement.spawn_pending
            release.set()
            await lane.idle()
            assert calls == ["terminate", "wait", "spawn"]
        finally:
            release.set()
            await lane.close()

    asyncio.run(scenario())
