"""creation validation, argv parsing, command mapping."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from relaydeck.client.features.create.commands import (
    agent_start_command,
    shell_create_command,
)
from relaydeck.client.features.create.validation import (
    AgentDraft,
    ShellDraft,
    creation_context,
    open_terminal_behavior,
    parse_argv,
    validate_agent,
    validate_shell,
)
from relaydeck.contracts.schemas import validate
from relaydeck.gateway.policy import authorize_command

HOSTS = frozenset({"home-mac", "work-mac"})
WORKSPACES = {
    "home-mac": frozenset({"w1", "w2"}),
    "work-mac": frozenset({"w9"}),
}
NOW = datetime(2026, 10, 3, 12, 0, 0, tzinfo=UTC)


def test_creation_context_preselects_only_a_known_scoped_space() -> None:
    hosts = (("home", "Home"), ("lab", "Lab"))
    spaces = {"home": (("w1", "Home1"), ("w2", "Home2")), "lab": (("w1", "Lab1"),)}
    assert creation_context(hosts, spaces, "home", "w2") == ("home", "w2")
    assert creation_context(hosts, spaces, "home", "missing") == ("home", "")
    assert creation_context(hosts, spaces, "lab") == ("lab", "w1")
    assert creation_context(hosts, spaces, "foreign", "w1") == ("home", "")
    assert creation_context(hosts, spaces) == ("home", "")
    assert creation_context((), spaces, "home", "w2") == ("", "")


KINDS = ("agent-a", "agent-b")


def _validate(draft: AgentDraft, **kwargs: Any) -> dict[str, str]:
    return validate_agent(draft, kinds=KINDS, **kwargs)


def test_parse_argv() -> None:
    assert parse_argv("") == ([], None)
    assert parse_argv("  ") == ([], None)
    assert parse_argv("--model o3 --flag") == (["--model", "o3", "--flag"], None)
    assert parse_argv('--name "my agent"') == (["--name", "my agent"], None)
    _, error = parse_argv('--name "oops')
    assert error
    _, error = parse_argv(" ".join(f"a{i}" for i in range(33)))
    assert error


def test__validate() -> None:
    good = AgentDraft("home-mac", "w1", "web", "agent-a", "--model o3")
    assert _validate(good, host_ids=HOSTS, workspace_ids_by_host=WORKSPACES) == {}
    assert (
        _validate(
            AgentDraft("nope", "w1", "web", "agent-a", ""),
            host_ids=HOSTS,
            workspace_ids_by_host=WORKSPACES,
        )["host"]
        == "Choose a host."
    )
    # Stale workspace after a host change is rejected.
    assert (
        _validate(
            AgentDraft("work-mac", "w1", "web", "agent-a", ""),
            host_ids=HOSTS,
            workspace_ids_by_host=WORKSPACES,
        )["workspace"]
        == "Choose a workspace."
    )
    bad = AgentDraft("home-mac", "w1", "", "evil", "--x 'oops")
    errors = _validate(bad, host_ids=HOSTS, workspace_ids_by_host=WORKSPACES)
    assert set(errors) == {"kind", "argv"}  # Empty name generates a kind-derived default.


def test_validate_shell() -> None:
    good = ShellDraft("home-mac", "w2", "dev")
    assert validate_shell(good, host_ids=HOSTS, workspace_ids_by_host=WORKSPACES) == {}
    errors = validate_shell(
        ShellDraft("home-mac", "w9", "x" * 65),
        host_ids=HOSTS,
        workspace_ids_by_host=WORKSPACES,
    )
    assert set(errors) == {"workspace", "label"}
    assert "agent" not in str(ShellDraft("home-mac", "w1", "")).lower()


def test_open_terminal_behavior() -> None:
    assert open_terminal_behavior(()) == "create"
    assert open_terminal_behavior(("t1",)) == "open"
    assert open_terminal_behavior(("t1", "t2")) == "choose"


def test_agent_start_command_validates_and_authorizes() -> None:
    body = agent_start_command(
        command_id="cmd-agent-001",
        host_id="home-mac",
        host_epoch="epoch-7",
        workspace_id="w1",
        kind="agent-a",
        name="web",
        argv=["--model", "o3"],
        now=NOW,
    )
    validate("relaydeck/gateway/v1/command-submit", body)
    assert body["expires_at"] == "2026-10-03T12:02:00Z"
    assert (
        authorize_command(
            frozenset({"agents.start"}),
            frozenset({"home-mac"}),
            body["kind"],
            body["host_id"],
            body["target"],
            body["payload"],
        )
        is None
    )


def test_shell_create_command_never_agent_start() -> None:
    body = shell_create_command(
        command_id="cmd-shell-001",
        host_id="home-mac",
        host_epoch="epoch-7",
        workspace_id="w1",
        label="dev",
        now=NOW,
    )
    validate("relaydeck/gateway/v1/command-submit", body)
    assert body["kind"] == "shell.create"
    assert "kind" not in body["payload"]
    assert "name" not in body["payload"]
    assert (
        authorize_command(
            frozenset({"shells.create"}),
            frozenset({"home-mac"}),
            body["kind"],
            body["host_id"],
            body["target"],
            body["payload"],
        )
        is None
    )
