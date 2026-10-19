"""A prompt cut off by a reconnect is reconciled read-only by its command id; never resent."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from relaydeck.client.features.composer.domain import (
    DELIVERY_DELIVERED,
    DELIVERY_FAILED,
    DELIVERY_UNKNOWN,
    SEND_IGNORED,
    SUBMISSION_ACKED,
    SUBMISSION_FAILED,
    SUBMISSION_SENDING,
    SUBMISSION_UNKNOWN,
    ComposerStore,
)
from relaydeck.client.features.composer.observation import CommandRefused
from relaydeck.client.features.terminal.diagnostics import diagnostics_logger
from tests.support.host_harness import run

SITES = ("detach", "fence_hidden", "takeover", "reconnect_flow")


class _Env:
    """Scripted gateway: the first status read is the original send's own observation."""

    def __init__(self, reads: list[Callable[[], Awaitable[None]]] | None = None) -> None:
        self.submits: list[dict[str, Any]] = []
        self.reads = reads or []
        self.read_count = 0
        self.original_gate = asyncio.Event()
        self.changes = 0

    async def submit(self, command: dict[str, Any]) -> dict[str, Any]:
        self.submits.append(command)
        return {"status": "accepted", "command_id": command["command_id"]}

    async def observe(self, host_id: str, command_id: str) -> dict[str, Any]:
        self.read_count += 1
        if self.read_count == 1:
            await self.original_gate.wait()  # the observation the reconnect cuts off
            return {"command_id": command_id}
        step = self.reads.pop(0) if self.reads else None
        if step is not None:
            await step()
        return {"command_id": command_id}

    def store(self, budget: float = 1.0) -> ComposerStore:
        ids = iter(f"cmd-{n}" for n in range(1, 50))
        return ComposerStore(
            target="w1:p1",
            submit_command=self.submit,
            prompt_text=None,
            host_id="home-mac",
            host_epoch="epoch-7",
            agent_id="w1:p1",
            command_id_factory=lambda: next(ids),
            observe_command=self.observe,
            reconcile_budget_s=budget,
            reconcile_interval_s=0.01,
            on_change=self._changed,
        )

    def _changed(self) -> None:
        self.changes += 1


async def _cut_off(
    env: _Env, store: ComposerStore, site: str = "reconnect_flow"
) -> asyncio.Task[str]:
    store.replace_draft("hello agent")
    task = asyncio.create_task(store.send())
    while env.read_count < 1:
        await asyncio.sleep(0)
    store.reconnect(site)
    assert store.submission == SUBMISSION_UNKNOWN
    return task


async def _settled(store: ComposerStore, env: _Env) -> None:
    for _ in range(100):
        await asyncio.sleep(0.01)
        if store.messages[-1].state.kind != DELIVERY_UNKNOWN:
            return


def test_succeeded_send_is_marked_delivered_and_the_draft_cleared_without_resending() -> None:
    async def scenario() -> None:
        env = _Env()
        store = env.store()
        task = await _cut_off(env, store)
        await _settled(store, env)
        (message,) = store.messages
        assert message.state.kind == DELIVERY_DELIVERED
        assert store.draft == "" and store.submission == SUBMISSION_ACKED and store.error is None
        assert len(env.submits) == 1 and env.changes == 1
        env.original_gate.set()
        assert await task == SEND_IGNORED

    run(scenario())


def test_an_edited_draft_is_kept_when_the_cut_off_send_turns_out_delivered() -> None:
    async def scenario() -> None:
        env = _Env()
        store = env.store()
        task = await _cut_off(env, store)
        store.replace_draft("something new")
        await _settled(store, env)
        assert store.messages[-1].state.kind == DELIVERY_DELIVERED
        assert store.draft == "something new" and len(env.submits) == 1
        env.original_gate.set()
        await task

    run(scenario())


def test_failed_send_shows_the_reason_and_keeps_the_draft() -> None:
    async def scenario() -> None:
        async def refuse() -> None:
            raise CommandRefused("agent_not_found")

        env = _Env([refuse])
        store = env.store()
        task = await _cut_off(env, store)
        await _settled(store, env)
        (message,) = store.messages
        assert message.state.kind == DELIVERY_FAILED
        assert store.submission == SUBMISSION_FAILED
        assert "agent_not_found" in (store.error or "")
        assert store.draft == "hello agent" and len(env.submits) == 1
        env.original_gate.set()
        await task

    run(scenario())


def test_a_status_read_that_keeps_failing_leaves_the_send_unknown_without_resending() -> None:
    async def scenario() -> None:
        async def down() -> None:
            raise ConnectionError("down")

        env = _Env([down] * 1000)
        store = env.store(budget=0.1)
        task = await _cut_off(env, store)
        await asyncio.sleep(0.4)
        (message,) = store.messages
        assert message.state.kind == DELIVERY_UNKNOWN
        assert store.submission == SUBMISSION_UNKNOWN and store.draft == "hello agent"
        assert len(env.submits) == 1 and env.changes == 0
        env.original_gate.set()
        await task

    run(scenario())


def test_a_late_answer_settles_only_its_own_message() -> None:
    async def scenario() -> None:
        release, hold = asyncio.Event(), asyncio.Event()

        async def late() -> None:
            await release.wait()

        async def newer_send_still_waiting() -> None:
            await hold.wait()

        env = _Env([late, newer_send_still_waiting])
        store = env.store()
        task = await _cut_off(env, store)
        # A second send arrives while the first answer is still pending.
        env.original_gate.set()
        await task
        second = asyncio.create_task(store.send())
        while len(env.submits) < 2:
            await asyncio.sleep(0)
        release.set()
        await asyncio.sleep(0.05)
        first, newer = store.messages
        assert first.state.kind == DELIVERY_DELIVERED
        assert newer.state.kind != DELIVERY_DELIVERED
        assert store.submission == SUBMISSION_SENDING and store.draft == "hello agent"
        second.cancel()
        hold.set()

    run(scenario())


@pytest.mark.parametrize("site", SITES)
def test_every_reset_site_reconciles_and_logs_content_free_lines(
    site: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    logger = diagnostics_logger()
    logger.addHandler(caplog.handler)

    async def scenario() -> None:
        env = _Env()
        store = env.store()
        task = await _cut_off(env, store, site)
        await _settled(store, env)
        assert store.messages[-1].state.kind == DELIVERY_DELIVERED
        assert "send_reconcile outcome=delivered waited_ms=" in caplog.text
        assert f"site={site}" in caplog.text
        assert "hello agent" not in caplog.text and "cmd-1" not in caplog.text
        env.original_gate.set()
        await task

    try:
        run(scenario())
    finally:
        logger.removeHandler(caplog.handler)
