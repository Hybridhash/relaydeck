"""Comparative loop latency with real synthetic stores and injected slow-disk contention."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from relaydeck.connector.journal import ConnectorDispatch
from relaydeck.gateway.app import create_app
from relaydeck.gateway.command_io import command_io
from relaydeck.gateway.production import JournalBackend
from relaydeck.gateway.usage import UsageService
from relaydeck.journal import CommandJournal
from relaydeck.usage.contracts import UsageQuery
from relaydeck.usage.store import SqliteUsageStore
from tests.support.pairing import foundation, proof


async def _load(path: Path, offloaded: bool, contended: bool) -> dict[str, float | int]:
    state, clock, body = foundation(path)

    def now():
        return clock[0]

    journal = CommandJournal(path / "gateway.db", clock=now)
    connector_journal = CommandJournal(path / "connector.db", clock=now)
    backend = JournalBackend(journal, now_ms=now)
    state.backend = backend
    store = SqliteUsageStore(path / "usage.db", "UTC", clock=now)
    service = UsageService(
        store,
        SimpleNamespace(sources=(), timezone="UTC", inferred_linking=True),
        "home-mac",
        path,
        now,
    )
    query = UsageQuery("home-mac", "1970-01-01", "1970-01-01")
    assert (await service.page(query, "owner", "phone")).status_code == 200
    state.pairing.open_window(
        state.hosts.record_for_host("home-mac"), state.public_origin, now(), 300
    )
    requested = state.pairing.request(body, state.hosts.record_for_host("home-mac"), now())
    attachments = [
        backend.create_attachment(
            "home-mac", f"agent-{i}", "agent", "owner", "phone", takeover=True
        )[0]
        for i in range(14)
    ]
    for target in (journal, connector_journal):
        commit = target._commit

        def slow_commit(commit=commit):
            if contended:
                time.sleep(0.025)  # Injected fsync delay, not a measurement of this disk.
            commit()

        target._commit = slow_commit
    if not offloaded:

        async def inline(call, *args, **kwargs):
            return call(*args, **kwargs)

        connector_journal.run = inline  # Inline dispatch path, the baseline.

    async def prompt(target, text):
        return {"type": "agent_prompted"}

    dispatch = ConnectorDispatch(
        connector_journal, SimpleNamespace(agent_prompt=prompt), host_id="home-mac", clock=now
    )
    stop = asyncio.Event()
    lags = []
    frames = 0

    async def output_and_probe():
        nonlocal frames
        while not stop.is_set():
            before = time.monotonic()
            await asyncio.sleep(0.005)
            lags.append(max(0, time.monotonic() - before - 0.005))
            for att in attachments:
                backend.publish_output(att.attachment_id, att.epoch, b"synthetic output\r\n" * 256)
                frames += 1

    async def call(operation, *args):
        if offloaded:
            return await command_io(backend, operation, *args)
        return getattr(backend, operation)(*args)

    async def commands():
        for i in range(14):
            await call(
                "submit_command",
                {
                    "command_id": f"c-{i}",
                    "host_id": "home-mac",
                    "principal": "owner",
                    "kind": "agent.prompt",
                    "target": {"agent_id": f"agent-{i}"},
                    "payload": {"text": "synthetic"},
                },
            )
        pending = await call("drain_outbox", "home-mac")
        assert len(pending) == 14
        for message in pending:
            cid = message["command_id"]
            assert await call("mark_command_dispatched", cid, now())
            result = await dispatch.dispatch(message)
            assert result["status"] == "succeeded"
            await call(
                "note_command_result",
                cid,
                "home-mac",
                "succeeded",
                result["result_digest"],
                "",
                None,
                now(),
            )
        assert await call("drain_outbox", "home-mac") == []

    usage_ready, pairing_ready = threading.Event(), threading.Event()

    def collect():
        with store._lock:
            usage_ready.set()
            time.sleep(0.35 if contended else 0)

    def approve():
        with state.pairing._transaction():
            pairing_ready.set()
            time.sleep(0.35 if contended else 0)

    async def usage():
        if not offloaded:
            # Inline cache validation, the baseline, using the real collector lock.
            store.data_generation()
            service._fingerprint(now())
        assert (await service.page(query, "owner", "phone")).status_code == 200

    async def pairing(client):
        if offloaded:
            response = await client.post(
                f"/v1/pairing/{requested['request_id']}/status", json=proof()
            )
            assert response.status_code == 200
        else:
            state.pairing.poll(
                requested["request_id"], proof(), state.hosts.record_for_host("home-mac"), now()
            )

    threads = [threading.Thread(target=collect), threading.Thread(target=approve)]
    probe = asyncio.create_task(output_and_probe())
    await asyncio.sleep(0)
    try:
        for thread in threads:
            thread.start()
        while not (usage_ready.is_set() and pairing_ready.is_set()):
            await asyncio.sleep(0)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(create_app(state)), base_url="https://test"
        ) as client:
            await asyncio.gather(usage(), pairing(client), commands())
        await asyncio.sleep(0.01)
        return {"max_lag_ms": round(max(lags) * 1000, 2), "output_frames": frames}
    finally:
        stop.set()
        await probe
        for thread in threads:
            await asyncio.to_thread(thread.join)
        await service.close()
        store.close()
        journal.close()
        connector_journal.close()
        state.push.close()


@pytest.mark.parametrize("contended", [False, True], ids=["normal", "contended"])
def test_loop_synthetic_14_agent_load_before_and_after(tmp_path, contended):
    async def scenario():
        before = await _load(tmp_path / "before", False, contended)
        after = await _load(tmp_path / "after", True, contended)
        print(
            "LOOP_LOAD "
            + json.dumps({"contended": contended, "before": before, "after": after}, sort_keys=True)
        )
        if contended:
            assert before["max_lag_ms"] > 250
            assert after["output_frames"] > before["output_frames"]
        assert after["max_lag_ms"] < 200

    asyncio.run(scenario())
