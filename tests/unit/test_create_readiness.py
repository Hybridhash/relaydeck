"""Prompt initialization refusals may retry; ambiguous launches never do."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

import relaydeck.connector.journal as dispatch_module
from relaydeck.connector.fixture import FixtureAdapter
from relaydeck.connector.herdr import HerdrError, HerdrProtocolError
from relaydeck.connector.journal import ConnectorDispatch
from relaydeck.journal import CommandJournal
from tests.support.creation.command_journal import NOW
from tests.support.creation.create_readiness import _message
from tests.support.host_harness import run


class InitializingShell(FixtureAdapter):
    def __init__(self, busy: int, error: Exception | None = None) -> None:
        super().__init__()
        self.busy, self.error = busy, error
        self.attempts: list[tuple[str, str, str, list[str] | None]] = []

    async def agent_start(
        self,
        name: str,
        kind: str,
        pane_id: str,
        args: list[str] | None = None,
        timeout_ms: int | None = None,
    ) -> dict[str, Any]:
        self.attempts.append((name, kind, pane_id, args))
        if self.error is not None:
            raise self.error
        if self.busy:
            self.busy -= 1
            raise HerdrError("agent_pane_busy", "synthetic shell is initializing")
        return await super().agent_start(name, kind, pane_id, args, timeout_ms)


@pytest.mark.parametrize("new", [False, True])
def test_busy_shell_then_ready_reuses_only_journaled_pane(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    new: bool,
) -> None:
    async def scenario() -> None:
        elapsed = [0.0]
        waits: list[float] = []

        async def wait(delay: float) -> None:
            waits.append(delay)
            elapsed[0] += delay

        monkeypatch.setattr(dispatch_module, "monotonic", lambda: elapsed[0], raising=False)
        monkeypatch.setattr(dispatch_module, "sleep", wait, raising=False)
        journal = CommandJournal(tmp_path / "commands.db", clock=lambda: NOW)
        try:
            source = InitializingShell(2)
            dispatch = ConnectorDispatch(journal, source, host_id="home-mac", clock=lambda: NOW)
            message = _message(new)
            result = await dispatch.dispatch(message)
            assert result["status"] == "succeeded"
            assert len(source.attempts) == 3 and len(set(a[2] for a in source.attempts)) == 1
            assert all(a[:2] == ("qa", "agent-a") and a[3] == ["--flag"] for a in source.attempts)
            assert waits and sum(waits) <= 15
            assert len(source._tabs) == 1 and len(source._agents) == 1
            assert (
                sum(method in {"workspace.create", "tab.create"} for method, _ in source.calls) == 1
            )
            assert not any(method == "pane.close" for method, _ in source.calls)
            before = list(source.attempts)
            assert await dispatch.dispatch(message) == result
            assert source.attempts == before
        finally:
            journal.close()

    run(scenario())


def test_busy_readiness_budget_leaves_partial_tab_reviewable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        elapsed = [0.0]

        async def wait(delay: float) -> None:
            elapsed[0] += delay

        monkeypatch.setattr(dispatch_module, "monotonic", lambda: elapsed[0], raising=False)
        monkeypatch.setattr(dispatch_module, "sleep", wait, raising=False)
        journal = CommandJournal(tmp_path / "commands.db", clock=lambda: NOW)
        try:
            source = InitializingShell(1000)
            dispatch = ConnectorDispatch(journal, source, host_id="home-mac", clock=lambda: NOW)
            result = await dispatch.dispatch(_message())
            assert result["status"] == "failed" and result["error_code"] == "agent_pane_busy"
            assert len(source.attempts) > 1 and elapsed[0] == 15
            assert result["result"]["type"] == "partial_creation"
            assert result["result"]["tab_id"] in source._tabs
            assert result["result"]["pane_id"] in source._panes
            assert not source._agents
            before = list(source.attempts)
            assert await dispatch.dispatch(_message()) == result
            assert source.attempts == before
        finally:
            journal.close()

    run(scenario())


@pytest.mark.parametrize(
    "error,status",
    [
        (HerdrError("agent_not_ready", "already launched"), "failed"),
        (HerdrError("agent_name_in_use", "refused"), "failed"),
        (HerdrProtocolError("agent_pane_busy", "untrusted envelope"), "unknown"),
        (TimeoutError("synthetic lost result"), "unknown"),
        (OSError("synthetic disconnect"), "unknown"),
    ],
)
@pytest.mark.parametrize("after_busy", [False, True])
def test_other_errors_are_not_retried(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    status: str,
    after_busy: bool,
) -> None:
    async def scenario() -> None:
        source = InitializingShell(int(after_busy), None if after_busy else error)
        elapsed = [0.0]

        async def wait(delay: float) -> None:
            elapsed[0] += delay
            source.error = error

        monkeypatch.setattr(dispatch_module, "monotonic", lambda: elapsed[0])
        monkeypatch.setattr(dispatch_module, "sleep", wait)
        journal = CommandJournal(tmp_path / "commands.db", clock=lambda: NOW)
        try:
            dispatch = ConnectorDispatch(journal, source, host_id="home-mac", clock=lambda: NOW)
            result = await dispatch.dispatch(_message())
            assert result["status"] == status
            assert len(source.attempts) == 1 + int(after_busy) and len(source._tabs) == 1
            assert dispatch.reconcile("ready-agent-command")["progress"]["tab_id"] in source._tabs
            await dispatch.dispatch(_message())
            assert len(source.attempts) == 1 + int(after_busy)
        finally:
            journal.close()

    run(scenario())


def test_redelivery_during_readiness_wait_never_starts_a_second_operation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        entered, release = asyncio.Event(), asyncio.Event()

        async def wait(delay: float) -> None:
            entered.set()
            await release.wait()

        monkeypatch.setattr(dispatch_module, "sleep", wait)
        journal = CommandJournal(tmp_path / "commands.db", clock=lambda: NOW)
        try:
            source = InitializingShell(1)
            dispatch = ConnectorDispatch(journal, source, host_id="home-mac", clock=lambda: NOW)
            task = asyncio.create_task(dispatch.dispatch(_message()))
            await asyncio.wait_for(entered.wait(), 1)
            try:
                duplicate = await dispatch.dispatch(_message())
                assert duplicate["status"] == "unknown"
                assert len(source.attempts) == 1 and len(source._tabs) == 1
            finally:
                release.set()
            assert (await task)["status"] == "succeeded"
            assert len(source.attempts) == 2 and len(source._agents) == 1
        finally:
            journal.close()

    run(scenario())


@pytest.mark.parametrize("change", ["epoch", "expiry", "cancel"])
def test_readiness_wait_revalidates_boundary_and_never_recreates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    async def scenario() -> None:
        now, epoch = [NOW], ["epoch-7"]
        elapsed = [0.0]

        async def wait(delay: float) -> None:
            elapsed[0] += delay
            if change == "epoch":
                epoch[0] = "replacement"
            elif change == "expiry":
                now[0] += 1001
            else:
                raise asyncio.CancelledError

        monkeypatch.setattr(dispatch_module, "monotonic", lambda: elapsed[0], raising=False)
        monkeypatch.setattr(dispatch_module, "sleep", wait, raising=False)
        journal = CommandJournal(tmp_path / "commands.db", clock=lambda: now[0])
        try:
            source = InitializingShell(5)
            dispatch = ConnectorDispatch(
                journal,
                source,
                host_id="home-mac",
                clock=lambda: now[0],
                epoch_probe=lambda: epoch[0],
            )
            message = {**_message(), "expires_ms": NOW + 1000}
            if change == "cancel":
                with pytest.raises(asyncio.CancelledError):
                    await dispatch.dispatch(message)
            else:
                result = await dispatch.dispatch(message)
                assert result["status"] == "failed"
                assert result["error_code"] == (
                    "stale_epoch" if change == "epoch" else "agent_pane_busy"
                )
            assert len(source.attempts) == 1 and len(source._tabs) == 1
            await dispatch.dispatch(message)
            assert len(source.attempts) == 1
        finally:
            journal.close()

    run(scenario())
