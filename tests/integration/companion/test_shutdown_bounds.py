"""Real companion supervision, gateway tunnel and lifecycle lanes; no listener or live daemon."""

from __future__ import annotations

import asyncio
import json
import signal
import threading
import time
from dataclasses import replace

import httpx
import pytest
import uvicorn

from relaydeck.companion.config import UsageConfig, load_config
from relaydeck.companion.lifecycle import (
    AlreadyRunning,
    StateLease,
    request_stop,
    status,
    supervise,
)
from relaydeck.companion.runtime import CompanionRuntime, _route_first_server
from relaydeck.connector.attach import AttachSession
from relaydeck.connector.attach_lifecycle import AttachLifecycle
from relaydeck.connector.enrollment import ConnectorCreds, save_creds
from relaydeck.connector.fixture import FixtureAdapter
from relaydeck.gateway.hosts import HostStore
from tests.support.asgi_socket import AsgiSocket
from tests.support.companion.asgi_companion import AsgiServer, config_file


async def _until(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0.005)


@pytest.mark.parametrize("stop_kind", ["request", "sigterm", "cancel"])
def test_stop_ack_and_runtime_exit_with_busy_usage_tunnel_command_and_attach(tmp_path, stop_kind):
    async def scenario():
        config = replace(
            load_config(config_file(tmp_path, web_ui=False)), usage=UsageConfig("UTC", ())
        )
        config.state_dir.mkdir(mode=0o700)
        token = HostStore(path=config.state_dir / "hosts.json").enroll(
            config.host_id, account="owner"
        )
        save_creds(
            ConnectorCreds(config.advertised_origin, config.host_id, token),
            str(config.connector_credentials),
        )
        reap_entered, command_entered = threading.Event(), threading.Event()
        collector_entered, release = threading.Event(), threading.Event()
        release_ledger = threading.Event()
        effects = []

        class Process:
            stderr = None
            returncode = None

            def poll(self):
                return None

            def terminate(self):
                effects.append("terminate_attach_client")

            def wait(self, timeout):
                reap_entered.set()
                assert release.wait(5)
                self.returncode = 0
                effects.append("attach_settled")
                return 0

        class Source(FixtureAdapter):
            def __init__(self):
                super().__init__()
                self.lane = AttachLifecycle(lambda: None)
                self.attachment = AttachSession(["synthetic"], lifecycle=self.lane)
                self.attachment._proc = Process()

            async def close(self):
                self.attachment.close()
                await self.lane.close()

        runtime = CompanionRuntime(
            config, fixture=True, server_factory=AsgiServer, source_factory=Source
        )
        closing, closed = asyncio.Event(), asyncio.Event()
        close = runtime.close

        async def tracked_close():
            closing.set()
            try:
                await close()
            finally:
                closed.set()

        runtime.close = tracked_close
        tunnels = []

        async def opener():
            socket = await AsgiSocket(runtime.app, "/v1/connectors", token=token).open()
            tunnels.append(socket)
            return socket

        runtime.opener = opener
        stop = asyncio.Event()
        owner = asyncio.create_task(supervise(config.state_dir, lambda: runtime, stop))
        polls = []
        collector = None
        try:
            await _until(
                lambda: runtime.gateway is not None and runtime.report()["connector_ready"]
            )
            service, store = runtime.gateway.usage, runtime.usage_store
            assert store is not None
            service.request_wait_s = 0.05
            journal, backend = runtime.gateway.backend.journal, runtime.gateway.backend
            journal.submit(
                command_id="held",
                principal="owner",
                host_id=config.host_id,
                kind="agent.start",
                target={"workspace_id": "w1"},
                payload={"kind": "agent-a", "name": "synthetic"},
            )
            assert journal.mark_dispatched("held")
            note = backend.note_command_ack

            def held_ack(*args):
                command_entered.set()
                assert release.wait(5)
                effects.append("ack_settled")
                return note(*args)

            backend.note_command_ack = held_ack
            await tunnels[0].send_text(
                json.dumps(
                    {
                        "kind": "command.ack",
                        "host_id": config.host_id,
                        "command_id": "held",
                    }
                )
            )
            await _until(command_entered.is_set)
            runtime.source.attachment.close()
            await _until(reap_entered.is_set)

            def hold_ledger():
                with store._lock:
                    collector_entered.set()
                    assert release_ledger.wait(5)

            collector = threading.Thread(target=hold_ledger, daemon=True)
            collector.start()
            await _until(collector_entered.is_set)
            access, _ = runtime.gateway.sessions.create(
                "owner",
                {"sessions.read"},
                {config.host_id},
                runtime.gateway.now_ms(),
                device_id="phone",
            )
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=runtime.app),
                base_url=config.advertised_origin,
            ) as http:
                polls = [
                    asyncio.create_task(
                        http.get(
                            f"/v1/hosts/{config.host_id}/usage",
                            params={"days": "1"},
                            headers={"Authorization": f"Bearer {access}"},
                        )
                    )
                    for _ in range(100)
                ]
                await asyncio.sleep(0)
                started = asyncio.get_running_loop().time()
                if stop_kind in ("request", "cancel"):
                    assert await request_stop(config.state_dir, timeout=2)
                else:
                    server = _route_first_server(
                        uvicorn.Config(runtime.app), runtime._request_shutdown
                    )
                    server.handle_exit(signal.SIGTERM, None)  # Real signal callback, no OS signal.
                    await _until(lambda: status(config.state_dir)["state"] == "stopping")
                ack_seconds = asyncio.get_running_loop().time() - started
                assert ack_seconds < 2
                assert not owner.done() and not release.is_set()
                replies = await asyncio.wait_for(asyncio.gather(*polls), 1)
                assert all(reply.status_code == 503 for reply in replies), [
                    (reply.status_code, reply.text) for reply in replies[:3]
                ]
                assert service._waiters == 0
                await asyncio.wait_for(closing.wait(), 1)
                if stop_kind == "cancel":
                    heartbeat = status(config.state_dir)["heartbeat"]
                    for _ in range(2):
                        owner.cancel()
                        done, _ = await asyncio.wait({owner}, timeout=0.3)
                        assert not done and not closed.is_set()
                        observed = status(config.state_dir)
                        assert observed["state"] == "stopping"
                        assert observed["heartbeat"] > heartbeat
                        heartbeat = observed["heartbeat"]
                        with pytest.raises(AlreadyRunning), StateLease(config.state_dir):
                            pytest.fail("replacement acquired ownership before cleanup settled")
                    assert "ack_settled" not in effects and "attach_settled" not in effects
                release.set()
                if stop_kind == "cancel":
                    with pytest.raises(asyncio.CancelledError):
                        await asyncio.wait_for(owner, 5)
                else:
                    assert await asyncio.wait_for(owner, 5) == 0
                elapsed = asyncio.get_running_loop().time() - started
                assert elapsed < 5
                assert not release_ledger.is_set()  # A stuck ledger mutex cannot pin shutdown.
                assert runtime.server.closed
                assert "ack_settled" in effects and "attach_settled" in effects
                assert status(config.state_dir)["state"] == "stopped"
                assert closed.is_set()
                with StateLease(config.state_dir):
                    pass  # A new owner is admitted only after the old cleanup has settled.
                assert not runtime.source.calls  # No upstream command, takeover, resize or replay.
                assert all(socket.task.done() for socket in tunnels)
                assert all(
                    not task.get_name().startswith("companion-")
                    for task in asyncio.all_tasks()
                    if task is not asyncio.current_task()
                )
                print(
                    f"{stop_kind}: ack={ack_seconds:.3f}s exit={elapsed:.3f}s polls={len(replies)}"
                )
        finally:
            release.set()
            release_ledger.set()
            stop.set()
            await asyncio.gather(*polls, return_exceptions=True)
            await asyncio.wait_for(asyncio.gather(owner, return_exceptions=True), 8)
            if closing.is_set():
                await asyncio.wait_for(closed.wait(), 8)
            if collector is not None:
                collector.join(1)
            if runtime.usage_store is not None:
                runtime.usage_store.close()

    asyncio.run(scenario())


def test_shutdown_interrupts_long_report_after_http_waiter_expires(tmp_path):
    async def scenario():
        config = replace(
            load_config(config_file(tmp_path, web_ui=False)), usage=UsageConfig("UTC", ())
        )
        config.state_dir.mkdir(mode=0o700)
        token = HostStore(path=config.state_dir / "hosts.json").enroll(
            config.host_id, account="owner"
        )
        save_creds(
            ConnectorCreds(config.advertised_origin, config.host_id, token),
            str(config.connector_credentials),
        )
        runtime = CompanionRuntime(
            config, fixture=True, server_factory=AsgiServer, source_factory=FixtureAdapter
        )

        async def opener():
            return await AsgiSocket(runtime.app, "/v1/connectors", token=token).open()

        runtime.opener = opener
        stop = asyncio.Event()
        owner = asyncio.create_task(supervise(config.state_dir, lambda: runtime, stop))
        try:
            await _until(
                lambda: runtime.gateway is not None and runtime.report()["connector_ready"]
            )
            service, store = runtime.gateway.usage, runtime.usage_store
            assert store is not None
            entered = threading.Event()
            failures = []
            original = service._report

            def slow_step(value):
                entered.set()
                time.sleep(0.005)  # Long SQL without a CPU-heavy synthetic flood.
                return value

            store._db.create_function("slow_usage_step", 1, slow_step)

            def long_report(query, now):
                try:
                    with store._transaction(write=False) as db:
                        db.execute(
                            "WITH RECURSIVE n(x) AS (VALUES(0) UNION ALL SELECT x+1 FROM n) "
                            "SELECT SUM(slow_usage_step(x)) FROM n"
                        ).fetchone()
                except Exception as exc:
                    failures.append(getattr(exc, "code", "unexpected"))
                    raise
                return original(query, now)

            service._report = long_report
            access, _ = runtime.gateway.sessions.create(
                "owner",
                {"sessions.read"},
                {config.host_id},
                runtime.gateway.now_ms(),
                device_id="phone",
            )
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=runtime.app),
                base_url=config.advertised_origin,
            ) as http:
                response = await asyncio.wait_for(
                    http.get(
                        f"/v1/hosts/{config.host_id}/usage",
                        params={"days": "7"},
                        headers={"Authorization": f"Bearer {access}"},
                    ),
                    1.5,
                )
                assert entered.is_set() and response.status_code == 503
                assert response.json() == {"error": "usage_busy"}
                assert response.headers["Retry-After"] == "2"
                assert len(service._workers) == 1 and len(service._inflight) == 1
                assert not next(iter(service._inflight.values())).done()
                began = asyncio.get_running_loop().time()
                assert await request_stop(config.state_dir, timeout=2)
                assert await asyncio.wait_for(owner, 2) == 0
                elapsed = asyncio.get_running_loop().time() - began
                assert elapsed < 2 and failures == ["busy"]
                assert not service._workers and not service._inflight and not service._reuse
                assert store._stopping.is_set() and store._closed and runtime.server.closed
                assert status(config.state_dir)["state"] == "stopped"
                print(f"long report: HTTP=503 shutdown={elapsed:.3f}s SQL interrupted")
        finally:
            stop.set()
            await asyncio.wait_for(asyncio.gather(owner, return_exceptions=True), 5)

    asyncio.run(scenario())
