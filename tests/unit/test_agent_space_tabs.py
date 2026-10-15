"""The agent switcher offers only the agents in the open agent's space."""

from __future__ import annotations

from types import SimpleNamespace

from relaydeck.client.hooks.views import agent_space_tabs


def _agent(
    terminal_id: str, workspace_id: str, host_id: str = "mac", status: str = "idle"
) -> SimpleNamespace:
    return SimpleNamespace(
        terminal_id=terminal_id,
        host_id=host_id,
        workspace_id=workspace_id,
        status=SimpleNamespace(value=status),
    )


def _state(*agents: SimpleNamespace) -> SimpleNamespace:
    return SimpleNamespace(inventory=SimpleNamespace(agents={a.terminal_id: a for a in agents}))


def test_only_agents_of_the_same_space_and_host_are_offered() -> None:
    current = _agent("a1", "w2")
    other_host = _agent("c1", "w2", host_id="other")
    state = _state(current, _agent("a2", "w2", status="working"), _agent("b1", "w1"), other_host)
    names = {"a1": "One", "a2": "Two", "b1": "Elsewhere"}

    assert agent_space_tabs(state, "mac", current, names) == (
        ("a1", "One", "idle"),
        ("a2", "Two", "working"),
    )


def test_unknown_current_agent_falls_back_to_the_host() -> None:
    state = _state(_agent("a1", "w2"), _agent("b1", "w1"))

    assert agent_space_tabs(state, "mac", None, {}) == (
        ("a1", "a1", "idle"),
        ("b1", "b1", "idle"),
    )
