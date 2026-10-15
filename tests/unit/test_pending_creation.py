"""Verified UI success settles one scoped identity; recovery retains uncertainty."""

from __future__ import annotations

import pytest

from relaydeck.client.pending_commands import MemoryCommandStore, PendingCommands, RecoveryError
from tests.support.creation.pending_creation import _creation, _record
from tests.support.host_harness import run


@pytest.mark.parametrize("agent", [False, True])
@pytest.mark.parametrize("new_workspace", [False, True])
def test_settle_only_exact_success_and_persist_without_losing_other_operations(
    agent: bool, new_workspace: bool
) -> None:
    async def scenario() -> None:
        journal = PendingCommands(MemoryCommandStore(), "https://gw", "acct", "dev")
        body = _creation(agent, new_workspace)
        foreign = {**body, "command_id": "foreign-operation", "host_id": "lab-mac"}
        await journal.begin(foreign)
        await journal.begin(body)
        await journal.observe("home-mac", body["command_id"], _record(body, "succeeded"))
        await journal.settle_creation("home-mac", body["command_id"], "created-resource")
        await journal.settle_creation("home-mac", body["command_id"], "created-resource")
        restored = PendingCommands(journal.store, "https://gw", "acct", "dev")
        await restored.load()
        assert set(restored.records) == {("lab-mac", "foreign-operation")}
        assert restored.unresolved(restored.records[("lab-mac", "foreign-operation")])

    run(scenario())


@pytest.mark.parametrize("agent", [False, True])
@pytest.mark.parametrize("outcome", ["accepted", "dispatched", "unknown", "failed", "rejected"])
def test_settle_cannot_dismiss_uncertain_failed_or_partial_creation(
    agent: bool, outcome: str
) -> None:
    async def scenario() -> None:
        journal = PendingCommands(MemoryCommandStore(), "https://gw", "acct", "dev")
        body = _creation(agent, new_workspace=True)
        await journal.begin(body)
        doc = _record(body, outcome)
        if outcome == "failed":
            doc["result"]["type"] = "partial_creation"
        await journal.observe("home-mac", body["command_id"], doc)
        before = await journal.store.get(journal.key)
        with pytest.raises(RecoveryError, match="not verified"):
            await journal.settle_creation("home-mac", body["command_id"], "created-resource")
        assert await journal.store.get(journal.key) == before
        assert len(journal.records) == 1

    run(scenario())


@pytest.mark.parametrize("agent", [False, True])
def test_settle_cannot_dismiss_a_different_or_unverified_resource(agent: bool) -> None:
    async def scenario() -> None:
        journal = PendingCommands(MemoryCommandStore(), "https://gw", "acct", "dev")
        body = _creation(agent)
        await journal.begin(body)
        await journal.observe("home-mac", body["command_id"], _record(body, "succeeded"))
        with pytest.raises(RecoveryError, match="not verified"):
            await journal.settle_creation("home-mac", body["command_id"], "other-resource")
        assert len(journal.records) == 1
        await journal.close()
        with pytest.raises(RecoveryError, match="no longer current"):
            await journal.settle_creation("home-mac", body["command_id"], "created-resource")

    run(scenario())
