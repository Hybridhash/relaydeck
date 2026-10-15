"""agent display names, subject line, disambiguation and search (synthetic data)."""

from __future__ import annotations

from relaydeck.client.features.inventory.selectors import (
    agent_names,
    agent_rows,
    agent_subject,
    metadata_rows,
)
from relaydeck.domain.models import (
    Agent,
    AgentStatus,
    AppState,
    Inventory,
    Tab,
    Workspace,
    scope_key,
)


def _agent(
    terminal: str, tab: str, *, name: str = "", title: str = "", kind: str = "kind-a"
) -> Agent:
    return Agent(
        terminal_id=terminal,
        host_id="home-mac",
        workspace_id="w1",
        tab_id=tab,
        pane_id=f"pane-{terminal}",
        name=name,
        agent=kind,
        title=title,
        status=AgentStatus.WORKING,
    )


def _state(agents: list[Agent], tabs: dict[str, str]) -> AppState:
    inventory = Inventory(
        workspaces={
            scope_key("home-mac", "w1"): Workspace(id="w1", host_id="home-mac", label="Space Alpha")
        },
        tabs={
            scope_key("home-mac", tab): Tab(
                id=tab, host_id="home-mac", workspace_id="w1", label=label
            )
            for tab, label in tabs.items()
        },
        agents={scope_key(a.host_id, a.terminal_id): a for a in agents},
    )
    return AppState(inventory=inventory)


def test_name_comes_from_the_agent_then_the_tab_label_then_the_subject_then_the_kind() -> None:
    state = _state(
        [
            _agent("t1", "tab-1", name="Ops Lead", title="Subject one"),
            _agent("t2", "tab-2", title="Subject guidelines alignment"),
            _agent("t3", "tab-3", title="Subject three"),
            _agent("t4", "tab-4", title="", kind="kind-b"),
        ],
        {"tab-1": "kind-a", "tab-2": "Review Desk", "tab-3": "kind-a", "tab-4": ""},
    )
    names = agent_names(state)
    assert names[scope_key("home-mac", "t1")] == "Ops Lead"  # agent name wins
    assert names[scope_key("home-mac", "t2")] == "Review Desk"  # tab label beats the subject
    assert names[scope_key("home-mac", "t3")] == "kind-a"  # the Herdr label as set
    assert names[scope_key("home-mac", "t4")] == "Kind-B"  # nothing better: the kind


def test_a_tab_named_after_its_kind_still_names_the_agent() -> None:
    state = _state(
        [_agent("t1", "tab-1", kind="kind-a", title="Real subject")], {"tab-1": "kind-a"}
    )
    assert agent_names(state)[scope_key("home-mac", "t1")] == "kind-a"
    assert agent_subject(state, scope_key("home-mac", "t1")) == "Real subject"


def test_two_panes_of_one_tab_are_numbered_in_terminal_order() -> None:
    state = _state(
        [
            _agent("t2", "tab-1", title="Second pane"),
            _agent("t1", "tab-1", title="First pane"),
            _agent("t9", "tab-2", title="Solo"),
        ],
        {"tab-1": "Build Desk", "tab-2": "Solo tab"},
    )
    names = agent_names(state)
    assert names[scope_key("home-mac", "t1")] == "Build Desk · 1"
    assert names[scope_key("home-mac", "t2")] == "Build Desk · 2"
    assert names[scope_key("home-mac", "t9")] == "Solo tab"


def test_subject_is_only_a_secondary_line_when_it_adds_something() -> None:
    state = _state(
        [_agent("t1", "tab-1", title="Mock documentation"), _agent("t2", "tab-2", title="Same")],
        {"tab-1": "Review Desk", "tab-2": "Same"},
    )
    assert agent_subject(state, scope_key("home-mac", "t1")) == "Mock documentation"
    assert agent_subject(state, scope_key("home-mac", "t2")) == ""


def test_rows_title_subject_and_search_match_name_and_subject() -> None:
    state = _state(
        [_agent("t1", "tab-1", title="Subject guidelines alignment")],
        {"tab-1": "Review Desk"},
    )
    (row,) = agent_rows(state)
    assert row.title == "Review Desk"
    assert row.subject == "Subject guidelines alignment"
    assert agent_rows(state, query="guidelines")  # found by subject
    assert agent_rows(state, query="review desk")  # found by name
    assert agent_rows(state, query="nothing-here") == ()


def test_paged_metadata_uses_the_loaded_name_and_keeps_the_subject() -> None:
    state = _state([_agent("t1", "tab-1", title="Subject text")], {"tab-1": "Review Desk"})
    page = [
        {
            "host_id": "home-mac",
            "terminal_id": "t1",
            "title": "Subject text",
            "status": "working",
            "agent": "kind-a",
        }
    ]
    (row,) = metadata_rows(page, 0, names=agent_names(state))
    assert row.title == "Review Desk"
    assert row.subject == "Subject text"
    (bare,) = metadata_rows(page, 0)  # without names: the page's own title, no duplicate subject
    assert bare.title == "Subject text" and bare.subject == ""
