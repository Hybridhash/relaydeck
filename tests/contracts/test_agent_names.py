"""Gateway rename kinds and pinned upstream request parameter shapes."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from relaydeck.client.features.inventory.rename import rename_command
from relaydeck.connector.herdr import HerdrAdapter
from relaydeck.connector.journal import GATEWAY_KINDS
from relaydeck.contracts.schemas import COMMAND_KINDS, validate
from relaydeck.gateway.policy import COMMAND_CAPABILITY
from tests.support.gateway.herdr_adapter import _FakeReader, _FakeWriter
from tests.support.host_harness import run


@pytest.mark.parametrize("method", ["agent.rename", "tab.create", "tab.rename"])
def test_names_adapter_matches_pinned_parameters(method: str) -> None:
    async def scenario() -> None:
        writer = _FakeWriter()
        reader = _FakeReader([b'{"id":"rd-1","result":{"type":"ok"}}\n'])

        async def opener() -> tuple[_FakeReader, _FakeWriter]:
            return reader, writer

        adapter = HerdrAdapter(opener=opener)
        if method == "agent.rename":
            await adapter.agent_rename("agent-a", "release-review")
            expected = {"target": "agent-a", "name": "release-review"}
        elif method == "tab.create":
            await adapter.tab_create("w1", label="release-review")
            expected = {"workspace_id": "w1", "label": "release-review"}
        else:
            await adapter.tab_rename("tab-a", "release-review")
            expected = {"tab_id": "tab-a", "label": "release-review"}
        request = json.loads(writer.written)
        assert request["method"] == method and request["params"] == expected
        schema = json.loads(Path("tests/fixtures/upstream/herdr-0.9.1-schema.json").read_text())
        definition = {
            "agent.rename": "AgentRenameParams",
            "tab.create": "TabCreateParams",
            "tab.rename": "TabRenameParams",
        }[method]
        shape = schema["schemas"]["request"]["$defs"][definition]
        assert set(shape.get("required", [])) <= set(request["params"]) <= set(shape["properties"])
        assert all(isinstance(value, str) for value in request["params"].values())

    run(scenario())


def test_names_gateway_contracts_agree() -> None:
    assert "agent.rename" in COMMAND_KINDS and "agent.rename" in GATEWAY_KINDS
    assert COMMAND_CAPABILITY["agent.rename"] == "agents.start"
    command = rename_command("home-mac", "epoch-7", "agent-a", "release-review")
    validate("relaydeck/gateway/v1/command-submit", command)
    record: dict[str, Any] = {
        **{key: command[key] for key in ("protocol", "command_id", "host_id", "kind", "target")},
        "status": "succeeded",
        "updated_ms": 1,
        "result": {"type": "ok"},
    }
    validate("relaydeck/gateway/v1/command-record", record)
    assert command["payload"] == {"name": "release-review"}


def test_agent_rename_can_clear_the_name_with_null() -> None:
    async def scenario() -> None:
        writer = _FakeWriter()
        reader = _FakeReader([b'{"id":"rd-1","result":{"type":"ok"}}\n'])

        async def opener() -> tuple[_FakeReader, _FakeWriter]:
            return reader, writer

        await HerdrAdapter(opener=opener).agent_rename("agent-a", None)
        request = json.loads(writer.written)
        assert request["params"] == {"target": "agent-a", "name": None}
        schema = json.loads(Path("tests/fixtures/upstream/herdr-0.9.1-schema.json").read_text())
        shape = schema["schemas"]["request"]["$defs"]["AgentRenameParams"]
        assert "null" in shape["properties"]["name"]["type"]

    run(scenario())
