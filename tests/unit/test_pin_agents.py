"""Pinned agents sort first; pins are validated, bounded and pruned."""

from __future__ import annotations

import json
from dataclasses import replace

from relaydeck.client.features.inventory.selectors import (
    AgentRow,
    agent_rows,
    pinned_first,
    pruned_pins,
)
from relaydeck.client.features.settings.prefs import PINS_MAX, TerminalPrefs, clean, encode
from relaydeck.domain.models import AgentStatus
from tests.support.ui.spaces_filter import _spaces_state


def _row(key: str, status: AgentStatus = AgentStatus.IDLE) -> AgentRow:
    return AgentRow(key=key, title=key, subtitle="", status=status, focused=False)


def test_pinned_first_keeps_the_existing_order_inside_each_group() -> None:
    rows = tuple(_row(key) for key in ("a:1", "a:2", "a:3", "a:4"))
    assert [r.key for r in pinned_first(rows, {"a:3", "a:2"})] == ["a:2", "a:3", "a:1", "a:4"]
    assert pinned_first(rows, ()) == rows
    assert pinned_first(rows, {"a:9"}) == rows  # unknown pins are ignored


def test_pinned_first_over_real_rows_keeps_status_then_title_order_in_each_group() -> None:
    state = _spaces_state()
    rows = agent_rows(state)
    pinned = {"home-mac:w2", "home-mac:w1"}
    sorted_rows = pinned_first(rows, pinned)
    head = [r.key for r in sorted_rows[:2]]
    assert set(head) == pinned
    assert head == [r.key for r in rows if r.key in pinned]
    assert [r.key for r in sorted_rows[2:]] == [r.key for r in rows if r.key not in pinned]


def test_pruned_pins_drops_missing_agents_and_toggles() -> None:
    state = _spaces_state()
    assert pruned_pins(state, ["home-mac:w1", "home-mac:gone"]) == ("home-mac:w1",)
    assert pruned_pins(state, ["home-mac:w1"], toggled="home-mac:w2") == (
        "home-mac:w1",
        "home-mac:w2",
    )
    assert pruned_pins(state, ["home-mac:w1"], toggled="home-mac:w1") == ()
    assert pruned_pins(state, [], toggled="home-mac:nope") == ()


def test_stored_pins_are_validated_and_bounded() -> None:
    assert clean({"pinned_agents": ["h:a", "h:a", 3, "", "nocolon", "h:b\n"]}).pinned_agents == (
        "h:a",
    )
    assert clean({"pinned_agents": "h:a"}).pinned_agents == ()
    many = [f"h:{index}" for index in range(PINS_MAX + 50)]
    assert len(clean({"pinned_agents": many}).pinned_agents) == PINS_MAX
    prefs = replace(TerminalPrefs(), pinned_agents=("h:a",))
    assert clean(json.loads(encode(prefs))) == prefs
