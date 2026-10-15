"""Memory cache and real session watch leases; no network or persisted screen content."""

import asyncio
import json
from dataclasses import replace
from typing import Any

import pytest
from relaydeck_terminal.terminal import RelayDeckTerminal

from relaydeck.client.features.shell.ops import AttachmentInfo
from relaydeck.client.features.terminal.screen_cache import (
    SCREEN_CACHE_BYTES,
    ScreenCache,
    ScreenSnapshot,
)
from relaydeck.client.features.terminal.warm_watch import watch_only
from relaydeck.client.retained_sessions import SLOT_IDLE_MS, RetainedSessions
from relaydeck.gateway import terminal_wire
from tests.support.scripted_sessions import make_scripted_sessions
from tests.support.ui.instant_open import jobs


def snapshot(text: str = "screen") -> ScreenSnapshot:
    return ScreenSnapshot(
        text,
        "host-e1",
        AttachmentInfo(
            "attachment",
            "mac",
            "terminal",
            "agent",
            1,
            "stream-e1",
            "dev",
            "device",
            True,
        ),
    )


@pytest.mark.parametrize("agent", [False, True])
def test_late_screen_reply_cannot_cache_over_newer_warm_output(
    agent: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        scripted = make_scripted_sessions()
        pool = RetainedSessions()
        entered, finish, applied = asyncio.Event(), asyncio.Event(), asyncio.Event()

        def create() -> Any:
            return (
                scripted.factory.make_agent(
                    host_id="mac", host_epoch="h1", agent_id="a", target="a"
                )
                if agent
                else scripted.factory.make_shell(host_id="mac")
            )

        entry = await pool.acquire("mac", "agent" if agent else "shell", "a", "h1", create)
        session = entry.session
        try:
            if agent:
                session.open()
                await session.confirm_takeover()
            else:
                await session.open("a")
            term = RelayDeckTerminal()
            session.bind_view(term, write=lambda *_: True)

            async def delayed_snapshot(timeout: float) -> str:
                entered.set()
                await finish.wait()
                return "old snapshot racing newer output"

            monkeypatch.setattr(term, "get_snapshot_text", delayed_snapshot)
            pool.release()
            await asyncio.wait_for(entered.wait(), 2)
            await entry.warm_task
            data = b"newer hidden output"
            session.subscribe(
                lambda live: applied.set() if live.term_state.applied_offset == len(data) else None
            )
            socket = next(
                sock
                for lease, _, sock in scripted.streams.opened
                if lease == session.lease.lease_id
            )
            socket.queue(("bytes", terminal_wire.encode_output(session.lease.epoch, 0, data)))
            await asyncio.wait_for(applied.wait(), 2)
            finish.set()
            await jobs(pool)
            assert pool.screens.get(entry.cache_key, "h1", session.attachment) is None
            assert session.cached_screen == "" and bytes(session.output_log) == data
        finally:
            finish.set()
            await pool.shutdown()

    asyncio.run(scenario())


def test_cache_lru_byte_bound_and_generation_scope() -> None:
    cache = ScreenCache()
    for i in range(8):
        assert cache.put(("mac", "agent", str(i)), snapshot(str(i)))
    assert cache.get(("mac", "agent", "0"), "host-e1").text == "0"
    cache.put(("mac", "agent", "8"), snapshot("8"))
    assert len(cache) == 8 and cache.get(("mac", "agent", "1"), "host-e1") is None
    assert cache.get(("other", "agent", "0"), "host-e1") is None
    assert cache.get(("mac", "shell", "0"), "host-e1") is None
    assert not cache.put(("mac", "agent", "0"), snapshot("😀" * (SCREEN_CACHE_BYTES // 4 + 1)))
    assert cache.get(("mac", "agent", "0"), "host-e1") is None
    cache.put(("mac", "agent", "new"), snapshot())
    assert cache.get(("mac", "agent", "new"), "host-e2") is None
    for change in ({"generation": 2}, {"epoch": "stream-e2"}, {"attachment_id": "replacement"}):
        screen = snapshot()
        cache.put(("mac", "agent", "new"), screen)
        assert (
            cache.get(("mac", "agent", "new"), "host-e1", replace(screen.attachment, **change))
            is None
        )


@pytest.mark.parametrize("agent", [False, True])
def test_warm_view_releases_control_preserves_emulator_and_never_writes_or_resizes(
    agent: bool,
) -> None:
    async def scenario() -> None:
        scripted = make_scripted_sessions()
        pool = RetainedSessions()
        kind = "agent" if agent else "shell"

        def create() -> Any:
            if agent:
                session = scripted.factory.make_agent(
                    host_id="mac", host_epoch="h1", agent_id="a", target="a"
                )
                session.open()
                return session
            return scripted.factory.make_shell(host_id="mac")

        entry = await pool.acquire("mac", kind, "a", "h1", create)
        session = entry.session
        try:
            assert not await watch_only(session, lambda: True)
            assert not scripted.ops.attaches and not scripted.ops.leases
            if agent:
                await session.confirm_takeover()
                session.on_draft_change("unsent draft")
            else:
                await session.open("a")
            term = RelayDeckTerminal()
            session.bind_view(term, write=lambda *_: True)
            old = session.controller
            token = session.input_token
            lease = session.lease
            pool.release()
            assert old.key_generation() is None
            assert not await old.request_resize(30, 80)
            await jobs(pool)
            assert session.lease.mode == "view" and session.controller is not old
            assert lease.lease_id in scripted.ops.releases and len(scripted.ops.attaches) == 1
            assert session.current_term is term and not session.foreground
            output = bytes(session.output_log)
            old.on_state(replace(session.term_state, lease_mode="control"))
            old.on_bytes(b"old-controller bytes")
            assert session.term_state.lease_mode == "view" and bytes(session.output_log) == output
            assert session.controller.key_generation() is None
            assert not await session.controller.request_resize(30, 80)
            assert await session.handle_view_input(1, b"forged", token=token) == "dropped"
            assert all(
                kind != "bytes" for _, _, sock in scripted.streams.opened for kind, _ in sock.sent
            )
            again = await pool.acquire("mac", kind, "a", "h1", lambda: pytest.fail("recreated"))
            await pool.preconnect(again)
            assert again is entry and session.lease.mode == "view"
            assert session.current_term is term and len(scripted.ops.attaches) == 1
            if agent:
                assert session.store.draft == "unsent draft"
        finally:
            await pool.shutdown()
        assert not scripted.ops.closes

    asyncio.run(scenario())


def test_two_background_views_across_hosts_idle_expiry_and_background_drop() -> None:
    async def scenario() -> None:
        scripted = make_scripted_sessions()
        now = [1000]
        pool = RetainedSessions(clock=lambda: now[0])
        entries = []
        try:
            for i in range(5):
                host = f"mac-{i}"
                entry = await pool.acquire(
                    host,
                    "shell",
                    str(i),
                    "h1",
                    lambda host_id=host: scripted.factory.make_shell(host_id=host_id),
                )
                await entry.session.open(str(i))
                entries.append(entry)
                await jobs(pool)
                now[0] += 1000
            assert entries[0].session.lease is None
            assert [e.session.lease.mode for e in entries[1:]] == [
                "view",
                "view",
                "view",
                "control",
            ]
            assert sum(not sock.closed for _, _, sock in scripted.streams.opened) == 4
            now[0] += SLOT_IDLE_MS
            await pool.evict_idle()
            assert pool.entries() == (entries[-1],)
            pool.set_device_foreground(False)
            await jobs(pool)
            assert entries[-1].session.paused and entries[-1].session.lease is None
            assert all(sock.closed for _, _, sock in scripted.streams.opened)
            pool.set_device_foreground(True)
            await asyncio.sleep(0)
            assert entries[-1].session.lease is None  # foreground never auto takes control
        finally:
            await pool.shutdown()

    asyncio.run(scenario())


def test_capture_hit_no_draft_persistence_and_revocation_fences_late_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        scripted = make_scripted_sessions()
        pool = RetainedSessions()
        entry = await pool.acquire(
            "mac",
            "agent",
            "a",
            "h1",
            lambda: scripted.factory.make_agent(
                host_id="mac", host_epoch="h1", agent_id="a", target="a"
            ),
        )
        session = entry.session
        try:
            session.open()
            await session.confirm_takeover()
            term = RelayDeckTerminal()
            session.bind_view(term, write=lambda *_: True)

            async def screen(timeout: float) -> str:
                return "private emulator screen"

            monkeypatch.setattr(term, "get_snapshot_text", screen)
            pool.release()
            await jobs(pool)
            assert (
                pool.screens.get(entry.cache_key, "h1", session.attachment).text
                == "private emulator screen"
            )
            assert "private emulator screen" not in json.dumps(session.store.export_draft())
            await pool.acquire("mac", "agent", "a", "h1", lambda: pytest.fail("recreated"))
            assert session.cached_screen == "private emulator screen"
            waiter = asyncio.Event()

            async def late(timeout: float) -> str:
                await waiter.wait()
                return "late revoked screen"

            monkeypatch.setattr(term, "get_snapshot_text", late)
            pool.release()
            await asyncio.sleep(0)
            pool.invalidate()
            waiter.set()
            await jobs(pool)
            assert len(pool.screens) == 0
        finally:
            await pool.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize("boundary", ["foreground", "revoke"])
def test_inflight_warm_response_never_promotes_control_or_survives_revocation(
    boundary: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        scripted = make_scripted_sessions()
        pool = RetainedSessions()
        entry = await pool.acquire(
            "mac", "shell", "a", "h1", lambda: scripted.factory.make_shell(host_id="mac")
        )
        await entry.session.open("a")
        entered, finish = asyncio.Event(), asyncio.Event()
        original = scripted.ops.acquire_lease
        issued: list[Any] = []

        async def delayed(attachment: str, mode: str) -> Any:
            lease = await original(attachment, mode)
            if not issued:
                issued.append(lease)
                entered.set()
                await finish.wait()
            return lease

        monkeypatch.setattr(scripted.ops, "acquire_lease", delayed)
        try:
            pool.release()
            await asyncio.wait_for(entered.wait(), 2)
            if boundary == "foreground":
                await pool.acquire("mac", "shell", "a", "h1", lambda: pytest.fail("recreated"))
            else:
                pool.invalidate()
            finish.set()
            await jobs(pool)
            assert issued[0].lease_id in scripted.ops.releases
            if boundary == "foreground":
                assert await pool.preconnect(entry)
                assert entry.session.lease.mode == "view"
            else:
                assert not await pool.preconnect(entry) and entry.session.lease is None
                assert len(pool.screens) == 0
            assert len(scripted.ops.attaches) == 1
            assert all(call["mode"] == "view" for call in scripted.ops.leases[1:])
        finally:
            finish.set()
            await pool.shutdown()

    asyncio.run(scenario())
