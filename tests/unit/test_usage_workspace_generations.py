"""Workspace identity. Generations bump on disappearance and on
reconnect fingerprint mismatch; history keeps its label; the wire stays stable."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

from relaydeck.usage.contracts import (
    Batch,
    Coverage,
    UsageQuery,
    parse_space_id,
    space_wire_id,
)
from relaydeck.usage.store import SqliteUsageStore
from tests.support.usage.mapping import pane, source_fixture
from tests.support.usage.store import NOW, SOURCE, event, mapping


def _snap(
    space: str, label: str = "Label", repo: str | None = None, panes: tuple = ()
) -> dict[str, Any]:
    workspace: dict[str, Any] = {"workspace_id": space, "label": label}
    if repo is not None:
        workspace["worktree"] = {"repo_key": repo}
    return {"workspaces": [workspace], "panes": list(panes)}


def _cwd_pane(pid: str, space: str, folder: str, agent: str = "kind-a") -> dict[str, Any]:
    return {
        "pane_id": pid,
        "workspace_id": space,
        "agent": agent,
        "foreground_cwd": folder,
    }


def _generations(source: Any) -> set[int]:
    # Only this file's space: the fixture bootstrap owns its own episodes.
    return {m.generation for m in source.usage_mappings() if m.workspace_id == "w9"}


def test_disappearance_bumps_the_generation(tmp_path: Path) -> None:
    source, _, now = source_fixture(tmp_path)
    source._usage_snapshot(_snap("w9", panes=[pane("w9")]))
    assert _generations(source) == {0}
    source._usage_snapshot({"workspaces": [], "panes": []})
    now[0] += 1
    source._usage_snapshot(_snap("w9", panes=[pane("w9")]))
    assert _generations(source) == {0, 1}


def test_plain_revalidation_never_splits(tmp_path: Path) -> None:
    source, _, now = source_fixture(tmp_path)
    doc = _snap("w9", repo="repo-a", panes=[pane("w9")])
    source._usage_snapshot(doc)
    now[0] += 1
    source._usage_snapshot(_snap("w9", repo="repo-a", panes=[pane("w9")]), revalidate=True)
    assert _generations(source) == {0}
    assert len([m for m in source.usage_mappings() if m.workspace_id == "w9"]) == 1


def test_fingerprint_change_on_revalidate_bumps(tmp_path: Path) -> None:
    source, _, now = source_fixture(tmp_path)
    source._usage_snapshot(_snap("w9", "Old", repo="repo-a", panes=[pane("w9")]))
    now[0] += 1
    source._usage_snapshot(_snap("w9", "New", repo="repo-b", panes=[pane("w9")]), revalidate=True)
    assert _generations(source) == {0, 1}


def test_rename_without_revalidate_flows_into_the_live_episode(tmp_path: Path) -> None:
    source, _, now = source_fixture(tmp_path)
    source._usage_snapshot(_snap("w9", "Old", panes=[pane("w9")]))
    now[0] += 1
    source._usage_snapshot(_snap("w9", "New", panes=[pane("w9")]))
    maps = [m for m in source.usage_mappings() if m.workspace_id == "w9"]
    assert len(maps) == 1 and maps[0].generation == 0 and maps[0].label == "New"


def test_workspace_closed_event_bumps(tmp_path: Path) -> None:
    source, _, now = source_fixture(tmp_path)
    source._usage_snapshot(_snap("w9", panes=[pane("w9")]))
    source._usage_event("workspace.closed", {"workspace_id": "w9"})
    now[0] += 1
    source._usage_snapshot(_snap("w9", panes=[pane("w9")]))
    assert _generations(source) == {0, 1}


def test_incomplete_snapshot_never_bumps(tmp_path: Path) -> None:
    source, _, now = source_fixture(tmp_path)
    source._usage_snapshot(_snap("w9", panes=[pane("w9")]))
    now[0] += 1
    source._usage_snapshot({"panes": []})
    now[0] += 1
    source._usage_snapshot(_snap("w9", panes=[pane("w9")]))
    assert _generations(source) == {0}


def test_episodes_carry_label_and_keyed_repo_fingerprint(tmp_path: Path) -> None:
    source, _, _ = source_fixture(tmp_path)
    source.configure_usage(
        source._usage_sources,
        digest=lambda folder: "digest",
        repo_fingerprint=lambda repo: f"fp:{repo}",
    )
    source._usage_snapshot(_snap("w9", "Label Nine", repo="repo-a", panes=[pane("w9")]))
    (found,) = [m for m in source.usage_mappings() if m.workspace_id == "w9"]
    assert (found.label, found.repo_fp, found.generation) == ("Label Nine", "fp:repo-a", 0)


def test_pane_digest_prefers_foreground_cwd_and_gates_kind(tmp_path: Path) -> None:
    source, _, now = source_fixture(tmp_path)
    seen: list[str] = []
    source.configure_usage(source._usage_sources, digest=lambda folder: seen.append(folder) or "dd")
    both = _cwd_pane("p9", "w9", "/synthetic/fg")
    both["cwd"] = "/synthetic/bg"
    other = _cwd_pane("p8", "w9", "/synthetic/x", agent="unknown-kind")
    source._usage_snapshot(_snap("w9", panes=[both, other]))
    assert seen == ["/synthetic/fg"]
    obs = source.usage_cwd_observations()
    assert len(obs) == 1 and obs[0].digest == "dd" and obs[0].kind == "kind-a"


def test_pane_digest_episode_splits_and_closes(tmp_path: Path) -> None:
    source, _, now = source_fixture(tmp_path)
    source.configure_usage(source._usage_sources, digest=lambda folder: f"d:{folder}")
    source._usage_snapshot(_snap("w9", panes=[_cwd_pane("p9", "w9", "/synthetic/a")]))
    now[0] += 1
    source._usage_snapshot(_snap("w9", panes=[_cwd_pane("p9", "w9", "/synthetic/b")]))
    assert len(source.usage_cwd_observations()) == 2
    now[0] += 1
    source._usage_snapshot(_snap("w9", panes=[]))
    assert len(source.usage_cwd_observations()) == 2  # closed episodes persist until acked


def test_no_digest_callable_means_no_pane_observations(tmp_path: Path) -> None:
    source, _, _ = source_fixture(tmp_path)
    source._usage_snapshot(_snap("w9", panes=[_cwd_pane("p9", "w9", "/synthetic/a")]))
    assert source.usage_cwd_observations() == ()


def test_generations_surface_as_compound_wire_ids(tmp_path: Path) -> None:
    with SqliteUsageStore(tmp_path / "usage.db", "UTC", clock=lambda: NOW) as store:
        first = store.cursor(SOURCE)
        store.apply(
            "kind-a",
            Batch((event("e1", t=NOW + 100),), first, Coverage.COMPLETE, first.revision),
            (mapping("w2", "a", NOW, NOW + 200),),
            source_key=SOURCE,
        )
        second = store.cursor(SOURCE)
        aged = replace(mapping("w2", "b", NOW + 300, NOW + 400), generation=1)
        store.apply(
            "kind-a",
            Batch((event("e2", t=NOW + 350),), second, Coverage.COMPLETE, second.revision),
            (aged,),
            source_key=SOURCE,
        )
        rows = {
            row.workspace_id: row
            for row in store.query(UsageQuery("h1", "2026-10-11", "2026-10-11"), NOW).rows
            if row.workspace_id is not None
        }
        assert set(rows) == {"w2", "w2~g0"}
        assert rows["w2"].counters.total == 12 and rows["w2~g0"].counters.total == 12
        bare = store.query(UsageQuery("h1", "2026-10-11", "2026-10-11", space_id="w2"), NOW)
        assert {row.workspace_id for row in bare.rows} <= {"w2", None}
        assert sum(row.counters.total for row in bare.rows if row.workspace_id == "w2") == 12
        old = store.query(UsageQuery("h1", "2026-10-11", "2026-10-11", space_id="w2~g0"), NOW)
        assert {row.workspace_id for row in old.rows} <= {"w2~g0", None}


def test_history_keeps_its_label(tmp_path: Path) -> None:
    with SqliteUsageStore(tmp_path / "usage.db", "UTC", clock=lambda: NOW) as store:
        first = store.cursor(SOURCE)
        store.apply(
            "kind-a",
            Batch((event("e1", t=NOW + 100),), first, Coverage.COMPLETE, first.revision),
            (replace(mapping("w2", "a", NOW, NOW + 200), label="Old Label"),),
            source_key=SOURCE,
        )
        second = store.cursor(SOURCE)
        store.apply(
            "kind-a",
            Batch((event("e2", t=NOW + 350),), second, Coverage.COMPLETE, second.revision),
            (replace(mapping("w2", "b", NOW + 300, NOW + 400), generation=1, label="New Label"),),
            source_key=SOURCE,
        )
        report = store.query(UsageQuery("h1", "2026-10-11", "2026-10-11"), NOW)
        assert dict(report.workspace_labels) == {"w2": "New Label", "w2~g0": "Old Label"}
        assert dict(report.workspace_days) == {"w2": "2026-10-11", "w2~g0": "2026-10-11"}


def test_legacy_mapping_rows_read_as_generation_zero(tmp_path: Path) -> None:
    with SqliteUsageStore(tmp_path / "usage.db", "UTC", clock=lambda: NOW) as store:
        cursor = store.cursor(SOURCE)
        store.apply(
            "kind-a",
            Batch((event(t=NOW + 100),), cursor, Coverage.COMPLETE, cursor.revision),
            (),
            source_key=SOURCE,
        )
        store._db.execute(
            "INSERT INTO mappings(source,session,interval,host,kind,workspace,first,last)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (SOURCE, "s1", "legacy", "h1", "kind-a", "w2", NOW, NOW + 200),
        )
        rows = store.query(UsageQuery("h1", "2026-10-11", "2026-10-11"), NOW).rows
        assert any(row.workspace_id == "w2" for row in rows)


def test_space_id_helpers() -> None:
    assert parse_space_id("w2") == ("w2", None)
    assert parse_space_id("w2~g0") == ("w2", 0)
    assert parse_space_id("w2~g12") == ("w2", 12)
    assert parse_space_id("weird") == ("weird", None)
    assert space_wire_id("w2", 1, 1) == "w2"
    assert space_wire_id("w2", 0, 1) == "w2~g0"
