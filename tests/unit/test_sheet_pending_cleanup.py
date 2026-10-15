"""Completed prompts leave recovery; uncertain and creation outcomes survive."""

import json

import pytest

from relaydeck.client.pending_commands import MemoryCommandStore, PendingCommands
from tests.support.creation.pending_command_records import command, record
from tests.support.creation.pending_creation import _creation, _record
from tests.support.host_harness import run


def test_observed_successful_prompt_is_removed_durably_and_next_send_is_independent() -> None:
    async def scenario() -> None:
        store = MemoryCommandStore()
        journal = PendingCommands(store, "https://mac", "acct", "device")
        body = command()
        await journal.begin(body)
        await journal.observe(body["host_id"], body["command_id"], record(body, "succeeded"))
        assert journal.records == {}
        restored = PendingCommands(store, "https://mac", "acct", "device")
        await restored.load()
        assert restored.records == {}
        await restored.begin(command("next"))
        assert len(restored.records) == 1

    run(scenario())


@pytest.mark.parametrize(
    "state", ["pending", "accepted", "dispatched", "unknown", "failed", "rejected"]
)
def test_reload_prunes_only_proven_prompt_success_in_its_own_scope(state: str) -> None:
    async def scenario() -> None:
        store = MemoryCommandStore()
        journal = PendingCommands(store, "https://mac", "acct", "device")
        body = command()
        await journal.begin(body)
        doc = json.loads(store.data[journal.key])
        successful = {**doc["records"][0], "outcome": "succeeded"}
        retained = {**doc["records"][0], "id": "retained-command", "outcome": state}
        doc["records"] = [successful, retained]
        store.data[journal.key] = json.dumps(doc)
        foreign = PendingCommands(store, "https://mac", "acct", "other-device")
        await foreign.begin(command("foreign"))
        foreign_before = store.data[foreign.key]
        await journal.load()
        assert list(journal.records) == [("home-mac", "retained-command")]
        assert journal.records[("home-mac", "retained-command")]["outcome"] == state
        assert json.loads(store.data[journal.key])["records"] == [retained]
        assert store.data[foreign.key] == foreign_before

    run(scenario())


@pytest.mark.parametrize("agent", [False, True])
@pytest.mark.parametrize("state", ["unknown", "succeeded", "failed", "rejected"])
def test_creation_records_require_verified_settlement_or_explicit_review(
    agent: bool, state: str
) -> None:
    async def scenario() -> None:
        journal = PendingCommands(MemoryCommandStore(), "https://mac", "acct", "device")
        body = _creation(agent, True)
        await journal.begin(body)
        doc = _record(body, state)
        if state == "failed":
            doc["result"]["type"] = "partial_creation"
        await journal.observe(body["host_id"], body["command_id"], doc)
        await journal.load()
        assert len(journal.records) == 1
        assert next(iter(journal.records.values()))["outcome"] == state

    run(scenario())


@pytest.mark.parametrize("mismatch", ["host_id", "command_id", "kind", "target", "schema"])
def test_unverified_success_cannot_prune_pending_prompt(mismatch: str) -> None:
    async def scenario() -> None:
        journal = PendingCommands(MemoryCommandStore(), "https://mac", "acct", "device")
        body = command()
        await journal.begin(body)
        doc = record(body, "succeeded")
        if mismatch == "target":
            doc[mismatch] = {"agent_id": "other"}
        elif mismatch == "schema":
            doc["status"] = "made-up"
        else:
            doc[mismatch] = "other"
        await journal.observe(body["host_id"], body["command_id"], doc)
        assert len(journal.records) == 1
        assert next(iter(journal.records.values()))["outcome"] == "unknown"

    run(scenario())
