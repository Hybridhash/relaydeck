"""Blocked push SQL stays off-loop and mutations retain admission order."""

import asyncio
import threading

import httpx
import pytest

from relaydeck.gateway.notification_transitions import NotificationTransitions, Status
from relaydeck.gateway.push import PushStore
from relaydeck.gateway.push_io import push_io
from tests.support.terminal.lease_gateway import _client, _token


def test_authenticated_device_list_waits_off_loop_for_sql(monkeypatch):
    async def scenario():
        client, state, _, _ = _client()
        token = _token(state)
        entered = threading.Event()
        original = state.push.device_metadata

        def metadata(account):
            entered.set()
            return original(account)

        monkeypatch.setattr(state.push, "device_metadata", metadata)
        state.push._lock.acquire()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=client.app), base_url="http://test"
        ) as http:
            task = asyncio.create_task(
                http.get("/v1/devices", headers={"authorization": f"Bearer {token}"})
            )
            try:
                assert await asyncio.to_thread(entered.wait, 1)
                assert not task.done()
            finally:
                state.push._lock.release()
            assert (await task).status_code == 200

    asyncio.run(scenario())


@pytest.mark.parametrize("fail", [False, True])
def test_old_notification_completion_cannot_settle_newer_status(fail):
    async def scenario():
        transitions = NotificationTransitions()
        base = ("acct", "host", "e1", "terminal", "pane")
        transitions._observe(Status(*base, "working", "initial"), 0)
        transitions._observe(Status(*base, "blocked", "blocked"), 1)
        entered, release = asyncio.Event(), asyncio.Event()

        async def emit(status, category):
            entered.set()
            await release.wait()
            if fail:
                raise OSError("synthetic")

        task = asyncio.create_task(transitions.flush_async(4000, emit))
        await entered.wait()
        newer = Status(*base, "done", "newer")
        transitions._observe(newer, 4001)
        release.set()
        await task
        assert transitions.pending[("host", "terminal")] == (7001, newer)
        assert transitions.attempts == {}

    asyncio.run(scenario())


def test_cancelled_started_mutation_finishes_before_next_revoke(monkeypatch):
    async def scenario():
        store = PushStore()
        entered, release = threading.Event(), threading.Event()
        calls = []
        original = store.register_device

        def register(*args):
            entered.set()
            assert release.wait(2)
            calls.append("register")
            return original(*args)

        original_revoke = store.revoke_device

        def revoke(*args):
            calls.append("revoke")
            return original_revoke(*args)

        monkeypatch.setattr(store, "register_device", register)
        monkeypatch.setattr(store, "revoke_device", revoke)
        first = asyncio.create_task(
            push_io(store, "register_device", "acct", "dev", "token", "android", 100)
        )
        try:
            assert await asyncio.to_thread(entered.wait, 1)
            first.cancel()
            second = asyncio.create_task(push_io(store, "revoke_device", "acct", "dev", 101))
            await asyncio.sleep(0)
            assert not first.done() and not second.done() and calls == []
            release.set()
            await asyncio.gather(first, return_exceptions=True)
            assert await second
            assert calls == ["register", "revoke"]
            assert store.active_devices("acct") == []
        finally:
            release.set()
            await asyncio.gather(first, return_exceptions=True)
            store.close()

    asyncio.run(scenario())
