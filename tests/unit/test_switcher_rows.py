"""Phone agent switcher ordering: active first, current space first, then name."""

from __future__ import annotations

from dataclasses import replace

from relaydeck.client.features.terminal.switcher import SwitcherRow, switcher_rows, working_count
from relaydeck.domain.models import AgentStatus
from tests.support.ui.spaces_filter import _spaces_state


def _keys(rows: tuple[SwitcherRow, ...]) -> list[str]:
    return [row.key for row in rows]


def test_active_group_first_then_current_space_then_name() -> None:
    rows = switcher_rows(_spaces_state(), "home-mac", "home-mac:w1", {})
    assert _keys(rows) == ["home-mac:b1", "home-mac:w1", "home-mac:w2", "home-mac:d2"]
    assert [row.status for row in rows] == ["blocked", "working", "working", "done"]


def test_the_current_workspace_leads_inside_each_group() -> None:
    rows = switcher_rows(_spaces_state(), "home-mac", "home-mac:w2", {})
    # The current workspace is w2: its working agent leads the active group.
    assert _keys(rows) == ["home-mac:w2", "home-mac:b1", "home-mac:w1", "home-mac:d2"]


def test_names_decide_inside_a_group_case_insensitively() -> None:
    names = {"home-mac:b1": "Zulu", "home-mac:w1": "alpha"}
    rows = switcher_rows(_spaces_state(), "home-mac", "home-mac:w1", names)
    assert _keys(rows)[:2] == ["home-mac:w1", "home-mac:b1"]
    assert [row.name for row in rows[:2]] == ["alpha", "Zulu"]


def test_only_the_current_row_is_marked() -> None:
    rows = switcher_rows(_spaces_state(), "home-mac", "home-mac:w2", {})
    assert [row.key for row in rows if row.current] == ["home-mac:w2"]


def test_only_agents_on_the_host() -> None:
    rows = switcher_rows(_spaces_state(), "lab-mac", "lab-mac:lab-b", {})
    assert _keys(rows) == ["lab-mac:lab-b"]
    assert all(not key.startswith("home-mac") for key in _keys(rows))


def test_space_label_comes_from_the_workspace_and_is_empty_when_missing() -> None:
    state = _spaces_state()
    rows = switcher_rows(state, "home-mac", "home-mac:w1", {})
    assert {row.key: row.space for row in rows}["home-mac:w1"] == "Atlas"
    workspaces = {k: v for k, v in state.inventory.workspaces.items() if k != "home-mac:w1"}
    bare = replace(state, inventory=replace(state.inventory, workspaces=workspaces))
    rows = switcher_rows(bare, "home-mac", "home-mac:w1", {})
    assert {row.key: row.space for row in rows}["home-mac:w1"] == ""


def test_working_count_counts_blocked_and_working() -> None:
    rows = switcher_rows(_spaces_state(), "home-mac", "home-mac:w1", {})
    assert working_count(rows) == 3
    assert working_count(()) == 0
    done = [replace(row, status=AgentStatus.DONE.value) for row in rows]
    assert working_count(done) == 0


def test_empty_host_and_single_agent() -> None:
    state = _spaces_state()
    assert switcher_rows(state, "nowhere", "", {}) == ()
    (only,) = switcher_rows(state, "lab-mac", "home-mac:w1", {})
    assert only.key == "lab-mac:lab-b" and not only.current
