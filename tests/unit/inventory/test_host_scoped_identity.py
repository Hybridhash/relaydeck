"""Host-scoped inventory identity.

Host-local workspace/tab/pane/agent ids repeat across hosts. Map keys,
Selection.session_key, row keys, and route segments carry the scoped
form (quote(host)+":"+quote(bare)); records and the wire keep bare
upstream ids.
"""

from __future__ import annotations

from dataclasses import replace

from relaydeck.client.features.inventory.routing import (
    RouteBridge,
    format_route,
    parse_route,
)
from relaydeck.client.features.inventory.selectors import agent_rows, shell_rows
from relaydeck.domain import reducers
from relaydeck.domain.models import (
    Agent,
    AppState,
    EventType,
    MetadataEvent,
    Pane,
    Snapshot,
    Tab,
    Workspace,
    lookup,
    scope_key,
    unscope_key,
)


def _twin_snapshot(host_id: str, label: str) -> Snapshot:
    """Same bare ids (w1/t1/p1/a1) on every host — the collision case."""
    return Snapshot(
        host_id=host_id,
        host_epoch=f"{host_id}-e1",
        cursor=f"c-{host_id}",
        workspaces=(Workspace(id="w1", host_id=host_id, label=label),),
        tabs=(Tab(id="t1", host_id=host_id, workspace_id="w1", label=label),),
        panes=(Pane(id="p1", host_id=host_id, workspace_id="w1", tab_id="t1"),),
        agents=(
            Agent(
                terminal_id="a1",
                host_id=host_id,
                workspace_id="w1",
                tab_id="t1",
                pane_id="p1",
                name=label,
            ),
        ),
    )


def _two_host_state() -> AppState:
    state = reducers.apply_snapshot(AppState(), _twin_snapshot("h1", "one"))
    return reducers.apply_snapshot(state, _twin_snapshot("h2", "two"))


# Key codec.


def test_scope_key_round_trip() -> None:
    assert scope_key("h1", "a1") == "h1:a1"
    assert unscope_key("h1:a1") == ("h1", "a1")


def test_scope_key_quotes_segment_breakers() -> None:
    key = scope_key("h/1", "a:1%2F")
    assert "/" not in key
    assert key.count(":") == 1
    assert unscope_key(key) == ("h/1", "a:1%2F")


def test_unscope_key_rejects_malformed() -> None:
    assert unscope_key("") is None
    assert unscope_key("bare") is None  # legacy bare id: never valid
    assert unscope_key(":a1") is None
    assert unscope_key("h1:") is None
    assert unscope_key("h1:a1:extra") is None


def test_lookup_checks_host_segment() -> None:
    state = _two_host_state()
    hit = lookup(state.inventory.agents, scope_key("h1", "a1"))
    assert hit is not None and hit.name == "one"
    assert lookup(state.inventory.agents, "a1") is None
    assert lookup(state.inventory.agents, scope_key("h9", "a1")) is None


# Snapshot coexistence.


def test_twin_snapshots_coexist_with_bare_records() -> None:
    state = _two_host_state()
    assert set(state.inventory.agents) == {"h1:a1", "h2:a1"}
    assert set(state.inventory.tabs) == {"h1:t1", "h2:t1"}
    assert set(state.inventory.panes) == {"h1:p1", "h2:p1"}
    assert set(state.inventory.workspaces) == {"h1:w1", "h2:w1"}
    # Records keep bare upstream ids.
    assert state.inventory.agents["h2:a1"].terminal_id == "a1"
    assert state.inventory.tabs["h1:t1"].id == "t1"
    # Per-host labels stay distinct: h2 did not overwrite h1.
    assert state.inventory.agents["h1:a1"].name == "one"
    assert state.inventory.agents["h2:a1"].name == "two"


def test_snapshot_for_one_host_preserves_other_slice() -> None:
    state = reducers.apply_snapshot(AppState(), _twin_snapshot("h1", "one"))
    state = reducers.apply_snapshot(state, _twin_snapshot("h2", "two"))
    assert state.inventory.workspaces["h1:w1"].label == "one"


def test_snapshot_clears_only_dead_selection() -> None:
    state = _two_host_state()
    state = reducers.select_session(state, "h2", scope_key("h2", "a1"), "agent")
    # h1 resync keeps the h2 selection (different host).
    kept = reducers.apply_snapshot(state, _twin_snapshot("h1", "one"))
    assert kept.selection.session_key == scope_key("h2", "a1")
    # h2 resync without the agent drops it.
    dropped = Snapshot(host_id="h2", host_epoch="h2-e2", cursor="c2")
    cleared = reducers.apply_snapshot(kept, dropped)
    assert cleared.selection.session_key == ""


# Event isolation.


def _event(host_id: str, seq: int, **kwargs) -> MetadataEvent:  # type: ignore[no-untyped-def]
    base = {
        "event_id": f"{host_id}-e{seq}",
        "host_id": host_id,
        "host_epoch": f"{host_id}-e1",
        "sequence": seq,
        "cursor": f"c{seq}",
    }
    base.update(kwargs)
    return MetadataEvent(**base)  # type: ignore[arg-type]


def _on_host(state: AppState, host_id: str) -> AppState:
    return replace(state, connection=replace(state.connection, host_id=host_id))


def test_event_upsert_touches_only_its_host_twin() -> None:
    state = _on_host(_two_host_state(), "h2")
    event = _event(
        "h2",
        1,
        type=EventType.AGENT_UPSERTED,
        resource_id="a1",
        resource_revision=3,
        payload={"name": "two-v2"},
    )
    state, action = reducers.apply_event(state, event)
    assert action == "applied"
    assert state.inventory.agents["h2:a1"].name == "two-v2"
    assert state.inventory.agents["h1:a1"].name == "one"


def test_agent_revision_guard_is_per_twin() -> None:
    state = _on_host(_two_host_state(), "h2")
    event = _event(
        "h2",
        1,
        type=EventType.AGENT_UPSERTED,
        resource_id="a1",
        resource_revision=5,
        payload={"name": "two-v2"},
    )
    state, _ = reducers.apply_event(state, event)
    assert state.inventory.agents["h2:a1"].revision == 5
    # A stale h2 revision is ignored; the h1 twin (rev 0) is untouched.
    stale = _event(
        "h2",
        2,
        type=EventType.AGENT_UPSERTED,
        resource_id="a1",
        resource_revision=2,
        payload={"name": "stale"},
    )
    state, _ = reducers.apply_event(state, stale)
    assert state.inventory.agents["h2:a1"].name == "two-v2"
    assert state.inventory.agents["h1:a1"].revision == 0


def test_removal_drops_only_its_host_twin() -> None:
    state = _on_host(_two_host_state(), "h2")
    state = reducers.select_session(state, "h1", scope_key("h1", "t1"), "shell")
    event = _event("h2", 1, type=EventType.RESOURCE_REMOVED, resource_id="t1")
    state, action = reducers.apply_event(state, event)
    assert action == "applied"
    assert set(state.inventory.tabs) == {"h1:t1"}
    # The h1 selection survives the h2 removal.
    assert state.selection.session_key == scope_key("h1", "t1")


def test_removal_clears_matching_scoped_selection() -> None:
    state = _on_host(_two_host_state(), "h2")
    state = reducers.select_session(state, "h2", scope_key("h2", "t1"), "shell")
    event = _event("h2", 1, type=EventType.RESOURCE_REMOVED, resource_id="t1")
    state, _ = reducers.apply_event(state, event)
    assert state.selection.session_key == ""
    assert state.selection.host_id == "h2"


# select_session validation.


def test_select_session_accepts_scoped_key() -> None:
    state = reducers.select_session(_two_host_state(), "h2", scope_key("h2", "a1"), "agent")
    assert state.selection.session_key == "h2:a1"
    assert state.selection.host_id == "h2"
    assert state.selection.generation == 1


def test_select_session_accepts_cleared_key() -> None:
    before = reducers.select_session(_two_host_state(), "h2", scope_key("h2", "a1"), "agent")
    cleared = reducers.select_session(before, "h2", "", "")
    assert cleared.selection.session_key == ""
    assert cleared.selection.generation == 2


def test_select_session_rejects_bare_malformed_mismatched() -> None:
    before = reducers.select_session(_two_host_state(), "h1", scope_key("h1", "a1"), "agent")
    for bad in ("a1", "", "h1:", ":a1", "h1:a1:x"):
        if bad == "":
            continue
        assert reducers.select_session(before, "h1", bad, "agent") is before
    # Host-mismatched scoped key: rejected, prior selection untouched.
    assert reducers.select_session(before, "h1", scope_key("h2", "a1"), "agent") is before
    assert before.selection.session_key == "h1:a1"


# Grant removal interplay.


def _grant(host_id: str) -> dict[str, object]:
    return {
        "host_id": host_id,
        "label": host_id,
        "epoch": f"{host_id}-e1",
        "reachable": True,
        "upstream": {"protocol": 22, "version": "0.9.1", "compatible": True},
    }


def test_grant_loss_keeps_surviving_twins() -> None:
    state = reducers.merge_hosts(_two_host_state(), [_grant("h1"), _grant("h2")])
    state = reducers.select_session(state, "h2", scope_key("h2", "a1"), "agent")
    merged = reducers.merge_hosts(state, [_grant("h1")])
    assert set(merged.inventory.hosts) == {"h1"}
    assert set(merged.inventory.agents) == {"h1:a1"}
    assert set(merged.inventory.tabs) == {"h1:t1"}
    assert set(merged.inventory.workspaces) == {"h1:w1"}
    assert set(merged.inventory.panes) == {"h1:p1"}
    # Selection pointed at the revoked host: cleared with a bump.
    assert merged.selection.session_key == ""
    assert merged.selection.generation == state.selection.generation + 1


def test_grant_loss_keeps_granted_selection_without_bump() -> None:
    state = reducers.merge_hosts(_two_host_state(), [_grant("h1"), _grant("h2")])
    state = reducers.select_session(state, "h1", scope_key("h1", "a1"), "agent")
    merged = reducers.merge_hosts(state, [_grant("h1")])
    assert merged.selection == state.selection


def test_revoked_snapshot_cannot_resurrect_twin() -> None:
    state = reducers.merge_hosts(_two_host_state(), [_grant("h1")])
    assert reducers.apply_snapshot(state, _twin_snapshot("h2", "evil")) is state


# Selectors.


def test_agent_rows_use_scoped_keys_and_host_labels() -> None:
    rows = agent_rows(_two_host_state())
    assert {r.key for r in rows} == {"h1:a1", "h2:a1"}
    by_key = {r.key: r for r in rows}
    assert "one" in by_key["h1:a1"].subtitle  # h1 workspace label
    assert "two" in by_key["h2:a1"].subtitle  # h2 workspace label


def test_agent_rows_host_filter() -> None:
    rows = agent_rows(_two_host_state(), host_id="h2")
    assert [r.key for r in rows] == ["h2:a1"]
    assert agent_rows(_two_host_state(), host_id="h9") == ()


def test_agent_rows_query_matches_bare_id() -> None:
    assert len(agent_rows(_two_host_state(), query="a1")) == 2
    assert [r.key for r in agent_rows(_two_host_state(), query="two")] == ["h2:a1"]


def test_shell_rows_scope_keys_and_pairwise_agent_tabs() -> None:
    state = _two_host_state()
    # h1:t1 is agent-owned, h2:t1 is a plain shell twin of the same bare id.
    state = replace(
        state,
        inventory=replace(
            state.inventory,
            agents={"h1:a1": state.inventory.agents["h1:a1"]},
        ),
    )
    rows = shell_rows(state)
    assert [r.key for r in rows] == ["h2:t1"]
    assert shell_rows(state, host_id="h1") == ()


def test_shell_rows_query_matches_bare_id() -> None:
    state = _two_host_state()
    state = replace(state, inventory=replace(state.inventory, agents={}))
    assert {r.key for r in shell_rows(state, query="t1")} == {"h1:t1", "h2:t1"}


# Routes.


def test_scoped_keys_round_trip_through_routes() -> None:
    route = format_route("agents", scope_key("h1", "a1"))
    assert route == "/agents/h1:a1"
    assert parse_route(route).selection == "h1:a1"


def test_quoted_keys_survive_one_segment() -> None:
    key = scope_key("h/1", "a:b")
    parsed = parse_route(format_route("terminals", key))
    assert parsed.selection == key
    assert unscope_key(parsed.selection) == ("h/1", "a:b")


def test_route_bridge_carries_scoped_selection() -> None:
    bridge = RouteBridge(route="/agents/h2:a1", width=1280)
    assert bridge.state.selection == "h2:a1"
    assert bridge.set_route("/agents/h1:a1") is True
    assert bridge.state.selection == "h1:a1"
    # Back to the list clears the selection.
    assert bridge.set_route("/agents") is True
    assert bridge.state.selection == ""
