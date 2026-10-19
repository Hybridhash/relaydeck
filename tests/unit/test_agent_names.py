"""Names survive replacement agents and explicit, journaled renames."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest

from relaydeck.client.adapters.fake import FakeCredentialStore
from relaydeck.client.adapters.server_commands import ServerCommandStore
from relaydeck.client.features.inventory.rename import rename_command
from relaydeck.client.features.inventory.selectors import agent_names, metadata_rows
from relaydeck.client.features.sync.controller import snapshot_from_wire
from relaydeck.client.pending_commands import MemoryCommandStore, PendingCommands, RecoveryError
from relaydeck.connector.fixture import FixtureAdapter
from relaydeck.connector.herdr import HerdrAdapter, HerdrError
from relaydeck.connector.journal import ConnectorDispatch
from relaydeck.connector.source import HerdrSource
from relaydeck.contracts.launch import agent_name_error as name_error
from relaydeck.domain import reducers
from relaydeck.domain.models import AppState, scope_key
from relaydeck.gateway.policy import authorize_command
from relaydeck.journal import CommandJournal
from tests.support.creation.command_journal import NOW, _dispatch_msg
from tests.support.creation.create_readiness import _message
from tests.support.creation.pending_command_records import command as prompt_command
from tests.support.creation.pending_command_records import record
from tests.support.creation.pending_creation import _creation
from tests.support.host_harness import run


def test_fresh_page_name_wins_over_old_snapshot_name() -> None:
    page = {
        "host_id": "home-mac",
        "terminal_id": "term-1",
        "name": "new-name",
        "title": "subject",
        "status": "working",
    }
    assert metadata_rows([page], 0, names={"home-mac:term-1": "8"})[0].title == "new-name"
    assert (
        metadata_rows([{**page, "name": ""}], 0, names={"home-mac:term-1": "tab-label"})[0].title
        == "tab-label"
    )


@pytest.mark.parametrize("new", [False, True])
def test_start_name_labels_tab_and_survives_replacement(tmp_path: Path, new: bool) -> None:
    async def scenario() -> None:
        source = FixtureAdapter()
        journal = CommandJournal(tmp_path / "commands.db", clock=lambda: NOW)
        try:
            dispatch = ConnectorDispatch(journal, source, host_id="home-mac", clock=lambda: NOW)
            result = await dispatch.dispatch(_message(new))
            assert result["status"] == "succeeded"
            tab_id = result["result"]["tab_id"]
            assert source.state()["tabs"][tab_id]["label"] == "qa"
            methods = [method for method, _ in source.calls]
            if new:
                assert methods == ["workspace.create", "tab.rename", "agent.start"]
                assert source.calls[1][1] == {"tab_id": tab_id, "label": "qa"}
            else:
                assert methods == ["tab.create", "agent.start"]
                assert source.calls[0][1] == {"workspace_id": "w1", "label": "qa"}
            snapshot = snapshot_from_wire(source.snapshot_document("home-mac"))
            state = reducers.apply_snapshot(AppState(), snapshot)
            original_id = result["result"]["agent_id"]
            assert agent_names(state)[scope_key("home-mac", original_id)] == "qa"
            replacement = replace(
                snapshot.agents[0],
                terminal_id="replacement",
                name=None,  # type: ignore[arg-type]
            )
            state = reducers.apply_snapshot(state, replace(snapshot, agents=(replacement,)))
            assert agent_names(state) == {"home-mac:replacement": "qa"}
            calls = source.calls
            assert await dispatch.dispatch(_message(new)) == result
            assert source.calls == calls
        finally:
            journal.close()

    run(scenario())


@pytest.mark.parametrize("platform", ["web", "native"])
def test_rename_pending_restores_uncertainty_without_payload_and_settles_success(
    tmp_path: Path, platform: str
) -> None:
    async def scenario() -> None:
        store = (
            ServerCommandStore(tmp_path / "pending.db")
            if platform == "web"
            else FakeCredentialStore()
        )
        commands = PendingCommands(store, "https://gateway.invalid", "synthetic", "device-a")
        try:
            command = rename_command("home-mac", "epoch-7", "term-1", "release-review")
            await commands.begin(command)
            await commands.observe("home-mac", command["command_id"], record(command, "unknown"))
            raw = await store.get(commands.key)
            assert raw is not None and "release-review" not in raw and "payload" not in raw
            restored = PendingCommands(store, "https://gateway.invalid", "synthetic", "device-a")
            await restored.load()
            assert restored.records[("home-mac", command["command_id"])]["target"] == {
                "agent_id": "term-1"
            }
            with pytest.raises(RecoveryError, match="unresolved"):
                await restored.begin(command)
            explicit = rename_command("home-mac", "epoch-7", "term-1", "other-name")
            await restored.begin(explicit)
            assert len(restored.records) == 2
            await restored.observe("home-mac", command["command_id"], record(command, "succeeded"))
            await restored.observe(
                "home-mac", explicit["command_id"], record(explicit, "succeeded")
            )
            again = PendingCommands(store, "https://gateway.invalid", "synthetic", "device-a")
            await again.load()
            assert not again.records
            await again.begin(rename_command("home-mac", "epoch-7", "term-1", "other-name"))
            assert len(again.records) == 1
        finally:
            await commands.close()
            if isinstance(store, ServerCommandStore):
                store.close()

    run(scenario())


@pytest.mark.parametrize("kind", ["agent.prompt", "agent.start", "shell.create"])
def test_rename_exception_keeps_other_unresolved_target_guards(kind: str) -> None:
    async def scenario() -> None:
        commands = PendingCommands(
            MemoryCommandStore(), "https://gateway.invalid", "synthetic", "device-a"
        )
        body = prompt_command() if kind == "agent.prompt" else _creation(kind == "agent.start")
        await commands.begin(body)
        await commands.observe("home-mac", body["command_id"], record(body, "unknown"))
        with pytest.raises(RecoveryError, match="unresolved"):
            await commands.begin({**body, "command_id": "new-operation-123"})
        assert len(commands.records) == 1

    run(scenario())


@pytest.mark.parametrize("name", ["build-helper", "a", "a_1", "a" * 32])
@pytest.mark.parametrize("kind", ["agent.rename", "agent.start"])
def test_name_policy_and_editor_accept_same_names(name: str, kind: str) -> None:
    assert not name_error(name)
    assert (
        authorize_command(
            frozenset({"agents.start"}),
            frozenset({"home-mac"}),
            kind,
            "home-mac",
            {"agent_id": "agent-a"} if kind == "agent.rename" else {"workspace_id": "w1"},
            {"name": name, **({"kind": "tool-a"} if kind == "agent.start" else {})},
        )
        is None
    )


@pytest.mark.parametrize(
    "name",
    [
        "Build Helper",
        "8",
        "-x",
        "",
        "a" * 33,
        "é",
        " a",
        "a ",
        "a\nb",
        "a\x00",
        "a\u200db",
        "a\ud800",
    ],
)
def test_start_name_policy_and_editor_reject_same_names(name: str) -> None:
    assert name_error(name)
    assert (
        authorize_command(
            frozenset({"agents.start"}),
            frozenset({"home-mac"}),
            "agent.start",
            "home-mac",
            {"workspace_id": "w1"},
            {"name": name, "kind": "tool-a"},
        )
        == "forbidden"
    )


def _rename_verdict(name: Any) -> str | None:
    return authorize_command(
        frozenset({"agents.start"}),
        frozenset({"home-mac"}),
        "agent.rename",
        "home-mac",
        {"agent_id": "agent-a"},
        {"name": name},
    )


@pytest.mark.parametrize(
    "name",
    ["Build Helper", "8", "é", "Ünïcode Name!", "🚀 launch", "a\u200db", "x" * 32],
)
def test_rename_policy_accepts_free_display_names(name: str) -> None:
    assert _rename_verdict(name) is None


@pytest.mark.parametrize(
    "name", ["", "   ", "a\nb", "a\x00", "a\ud800", "a\u200eb", "x" * 33, "🚀" * 33, 7, None]
)
def test_rename_policy_rejects_empty_control_and_overlong_names(name: Any) -> None:
    assert _rename_verdict(name) == "forbidden"


@pytest.mark.parametrize(
    "caps,host,target,payload,reason",
    [
        (
            frozenset({"agents.prompt"}),
            "home-mac",
            {"agent_id": "a"},
            {"name": "New"},
            "unsupported_capability",
        ),
        (frozenset({"agents.start"}), "other", {"agent_id": "a"}, {"name": "New"}, "not_found"),
        (frozenset({"agents.start"}), "home-mac", {"tab_id": "a"}, {"name": "New"}, "forbidden"),
        (
            frozenset({"agents.start"}),
            "home-mac",
            {"agent_id": "a", "tab_id": "t"},
            {"name": "New"},
            "forbidden",
        ),
        (
            frozenset({"agents.start"}),
            "home-mac",
            {"agent_id": "a"},
            {"name": "New", "extra": True},
            "forbidden",
        ),
        (frozenset({"agents.start"}), "home-mac", {"agent_id": "a"}, {"name": None}, "forbidden"),
    ],
)
def test_rename_policy_rejects_other_shapes(
    caps: frozenset[str], host: str, target: dict[str, Any], payload: dict[str, Any], reason: str
) -> None:
    assert (
        authorize_command(caps, frozenset({"home-mac"}), "agent.rename", host, target, payload)
        == reason
    )


@pytest.mark.parametrize("failure", [False, True])
def test_rename_current_agent_chain_and_uncertain_redelivery(tmp_path: Path, failure: bool) -> None:
    class Source(FixtureAdapter):
        async def agent_rename(self, target: str, name: str) -> dict[str, Any]:
            result = await super().agent_rename(target, name)
            if failure:
                raise TimeoutError("response lost")
            return result

    async def scenario() -> None:
        source = Source()
        journal = CommandJournal(tmp_path / "commands.db", clock=lambda: NOW)
        try:
            dispatch = ConnectorDispatch(journal, source, host_id="home-mac", clock=lambda: NOW)
            started = (await dispatch.dispatch(_message()))["result"]
            message = _dispatch_msg(
                "rename-a",
                "agent.rename",
                {"agent_id": started["agent_id"]},
                {"name": "release-review"},
            )
            result = await dispatch.dispatch(message)
            assert result["status"] == ("unknown" if failure else "succeeded")
            assert source.calls[-3:] == (
                ("agent.list", {}),
                ("tab.rename", {"tab_id": started["tab_id"], "label": "release-review"}),
                ("agent.rename", {"target": started["agent_id"], "name": "release-review"}),
            )
            state = reducers.apply_snapshot(
                AppState(), snapshot_from_wire(source.snapshot_document("home-mac"))
            )
            assert agent_names(state) == {
                scope_key("home-mac", started["agent_id"]): "release-review"
            }
            before = source.calls
            assert (await dispatch.dispatch(message))["status"] == result["status"]
            assert source.calls == before
        finally:
            journal.close()

    run(scenario())


def test_rename_unknown_agent_is_typed_and_never_guesses_tab(tmp_path: Path) -> None:
    async def scenario() -> None:
        source = FixtureAdapter()
        journal = CommandJournal(tmp_path / "commands.db", clock=lambda: NOW)
        try:
            dispatch = ConnectorDispatch(journal, source, host_id="home-mac", clock=lambda: NOW)
            message = _dispatch_msg(
                "rename-missing", "agent.rename", {"agent_id": "missing"}, {"name": "New"}
            )
            result = await dispatch.dispatch(message)
            assert result["status"] == "failed" and result["error_code"] == "agent_not_found"
            assert source.calls == (("agent.list", {}),)
            with pytest.raises(HerdrError, match="agent_not_found"):
                await source.agent_rename("missing", "new")
        finally:
            journal.close()

    run(scenario())


def test_new_workspace_label_failure_keeps_partial_identity_without_recreation(
    tmp_path: Path,
) -> None:
    class Source(FixtureAdapter):
        async def tab_rename(self, tab_id: str, label: str) -> dict[str, Any]:
            await super().tab_rename(tab_id, label)
            raise TimeoutError("response lost")

    async def scenario() -> None:
        source = Source()
        journal = CommandJournal(tmp_path / "commands.db", clock=lambda: NOW)
        try:
            dispatch = ConnectorDispatch(journal, source, host_id="home-mac", clock=lambda: NOW)
            result = await dispatch.dispatch(_message(True))
            assert result["status"] == "unknown"
            progress = dispatch.reconcile("ready-agent-command")["progress"]
            assert progress["tab_id"] in source._tabs
            assert progress["pane_id"] in source._panes
            assert progress["workspace_id"] in source._workspaces
            calls = source.calls
            assert (await dispatch.dispatch(_message(True)))["status"] == "unknown"
            assert source.calls == calls and not source._agents
        finally:
            journal.close()

    run(scenario())


def test_source_passthrough_invalidates_snapshot_after_rename() -> None:
    class Adapter(FixtureAdapter):
        def __init__(self) -> None:
            super().__init__()
            self.renamed: list[str] = []

        async def agent_rename(self, target: str, name: str) -> dict[str, Any]:
            self.renamed.append(target)
            row = next(row for row in self._agents.values() if row["pane_id"] == target)
            return await super().agent_rename(row["terminal_id"], name)

    async def scenario() -> None:
        adapter = Adapter()
        created = await adapter.tab_create("w1", label="Old")
        agent = (await adapter.agent_start("Old", "agent-a", created["root_pane"]["pane_id"]))[
            "agent"
        ]
        source = HerdrSource(cast(HerdrAdapter, adapter))
        source.take_snapshot_dirty()
        assert (await source.agent_list())[0]["terminal_id"] == agent["terminal_id"]
        await source.tab_rename(created["tab"]["tab_id"], "New")
        assert source.take_snapshot_dirty() and not source.take_snapshot_dirty()
        await source.agent_rename(agent["terminal_id"], "new")
        assert adapter.renamed == [created["root_pane"]["pane_id"]]
        assert source.take_snapshot_dirty()
        with pytest.raises(HerdrError):
            await source.agent_rename("missing", "new")
        assert source.take_snapshot_dirty()

    run(scenario())


def _renamed(tmp_path: Path, name: str) -> tuple[Any, dict[str, str]]:
    async def scenario() -> tuple[Any, dict[str, str]]:
        source = FixtureAdapter()
        journal = CommandJournal(tmp_path / "commands.db", clock=lambda: NOW)
        try:
            dispatch = ConnectorDispatch(journal, source, host_id="home-mac", clock=lambda: NOW)
            started = (await dispatch.dispatch(_message()))["result"]
            message = _dispatch_msg(
                "rename-free", "agent.rename", {"agent_id": started["agent_id"]}, {"name": name}
            )
            result = await dispatch.dispatch(message)
            assert result["status"] == "succeeded", result
            return source, started
        finally:
            journal.close()

    return run(scenario())


def test_free_display_name_sets_the_tab_label_and_a_slug_for_the_agent(tmp_path: Path) -> None:
    source, started = _renamed(tmp_path, "  Docs Writer ")
    assert source.calls[-2:] == (
        ("tab.rename", {"tab_id": started["tab_id"], "label": "Docs Writer"}),
        ("agent.rename", {"target": started["agent_id"], "name": "docs-writer"}),
    )
    state = reducers.apply_snapshot(
        AppState(), snapshot_from_wire(source.snapshot_document("home-mac"))
    )
    assert list(agent_names(state).values()) == ["Docs Writer"]


@pytest.mark.parametrize("name", ["8", "🚀", "42 !"])
def test_a_label_without_a_usable_slug_renames_the_tab_only(tmp_path: Path, name: str) -> None:
    source, started = _renamed(tmp_path, name)
    assert source.calls[-1] == ("tab.rename", {"tab_id": started["tab_id"], "label": name})
    assert not any(kind == "agent.rename" for kind, _ in source.calls)


def test_the_fixture_enforces_the_upstream_agent_name_rule() -> None:
    async def scenario() -> None:
        source = FixtureAdapter()
        created = await source.tab_create("w1", label="Old")
        agent = (await source.agent_start("old", "agent-a", created["root_pane"]["pane_id"]))[
            "agent"
        ]
        with pytest.raises(HerdrError, match="invalid_agent_name"):
            await source.agent_rename(agent["terminal_id"], "Docs Writer")

    run(scenario())


def test_display_prefers_the_tab_label_when_the_agent_name_is_its_slug() -> None:
    page = {
        "host_id": "home-mac",
        "terminal_id": "term-1",
        "name": "docs-writer",
        "title": "subject",
        "status": "working",
    }
    shown = metadata_rows([page], 0, names={"home-mac:term-1": "Docs Writer"})[0].title
    assert shown == "Docs Writer"
    other = metadata_rows([page], 0, names={"home-mac:term-1": "Something Else"})[0].title
    assert other == "docs-writer"


def test_a_slug_another_agent_holds_clears_this_agents_name_and_still_succeeds(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        source = FixtureAdapter()
        first = await source.tab_create("w1", label="Old")
        await source.agent_start("docs-writer", "agent-a", first["root_pane"]["pane_id"])
        second = await source.tab_create("w1", label="Other")
        agent = (await source.agent_start("other", "agent-a", second["root_pane"]["pane_id"]))[
            "agent"
        ]
        journal = CommandJournal(tmp_path / "commands.db", clock=lambda: NOW)
        try:
            dispatch = ConnectorDispatch(journal, source, host_id="home-mac", clock=lambda: NOW)
            message = _dispatch_msg(
                "rename-taken",
                "agent.rename",
                {"agent_id": agent["terminal_id"]},
                {"name": "Docs Writer"},
            )
            result = await dispatch.dispatch(message)
            assert result["status"] == "succeeded", result
            assert source.calls[-3:] == (
                ("tab.rename", {"tab_id": second["tab"]["tab_id"], "label": "Docs Writer"}),
                ("agent.rename", {"target": agent["terminal_id"], "name": "docs-writer"}),
                ("agent.rename", {"target": agent["terminal_id"], "name": None}),
            )
            progress = journal.record("rename-taken")["progress"]
            assert progress == {"tab_id": second["tab"]["tab_id"], "agent_id": agent["terminal_id"]}
            state = reducers.apply_snapshot(
                AppState(), snapshot_from_wire(source.snapshot_document("home-mac"))
            )
            assert sorted(agent_names(state).values()) == ["Docs Writer", "docs-writer"]
        finally:
            journal.close()

    run(scenario())


def test_other_refusals_of_the_agent_step_still_fail_the_command(tmp_path: Path) -> None:
    class Source(FixtureAdapter):
        async def agent_rename(self, target: str, name: str | None) -> dict[str, Any]:
            raise HerdrError("invalid_agent_name", "refused")

    async def scenario() -> None:
        source = Source()
        journal = CommandJournal(tmp_path / "commands.db", clock=lambda: NOW)
        try:
            dispatch = ConnectorDispatch(journal, source, host_id="home-mac", clock=lambda: NOW)
            started = (await dispatch.dispatch(_message()))["result"]
            message = _dispatch_msg(
                "rename-refused", "agent.rename", {"agent_id": started["agent_id"]}, {"name": "Ok"}
            )
            result = await dispatch.dispatch(message)
            assert result["status"] == "failed" and result["error_code"] == "invalid_agent_name"
        finally:
            journal.close()

    run(scenario())
