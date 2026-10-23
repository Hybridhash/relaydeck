"""rank-2 process-evidence linking. Fake panes, fake lsof output, synthetic
layouts; no subprocess runs except the runner's own echo/exit/timeout checks."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from relaydeck.connector.source import HerdrSource
from relaydeck.usage import proc_link
from relaydeck.usage.contracts import (
    Batch,
    Budget,
    Coverage,
    Cursor,
    PaneCwdObservation,
    RowScope,
    SessionMapping,
    UsageEvent,
    UsageQuery,
    UsageRow,
    UsageSource,
    UsageUnavailable,
)
from relaydeck.usage.readers import READERS
from relaydeck.usage.readers.jsonl_event_usage import (
    LOCK_FILENAME,
)
from relaydeck.usage.readers.jsonl_event_usage import (
    READER as EVENT_READER,
)
from relaydeck.usage.store import SqliteUsageStore
from tests.support.usage.store import NOW, SOURCE, event, mapping

EVENT_FORMAT = "jsonl-event-usage-v1"
TURNS_FORMAT = "json-session-turns-v1"
EVENT_SOURCE = f"h1/kind-a/{EVENT_FORMAT}"
UID = os.getuid()
START = "Mon Oct 12 11:00:00 2026"


class FakeRun:
    """argv[0]-keyed stdout scripts; records every call with its timeout."""

    def __init__(self, outputs: dict[str, str | None]) -> None:
        self.outputs = outputs
        self.calls: list[tuple[tuple[str, ...], float]] = []

    def __call__(self, argv: tuple[str, ...], timeout: float) -> str | None:
        self.calls.append((argv, timeout))
        for key, out in self.outputs.items():
            if key in argv[0]:
                return out
        return None


def _target(source_key: str = SOURCE, root: Path | None = None) -> proc_link.ProcTarget:
    return proc_link.ProcTarget(source_key, root or Path("/root"), EVENT_READER)


def _probe(
    run: FakeRun,
    targets: tuple[proc_link.ProcTarget, ...] | None = None,
    deadline: float = 1005.0,
    now: float = 1000.0,
) -> proc_link.ProcObservation | None:
    return proc_link.probe_pane(
        run,
        herdr=("herdr",),
        pane_id="p1",
        targets=targets if targets is not None else (_target(),),
        monotonic=lambda: now,
        deadline=deadline,
    )


def _lsof(*paths: str) -> str:
    lines = ["p123"]
    for i, path in enumerate(paths):
        lines += [f"f{i}", f"n{path}"]
    return "\n".join(lines) + "\n"


def _proc(pid: int, **over: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "pid": pid,
        "name": "n",
        "argv0": "a",
        "argv": ["a"],
        "cmdline": "c",
        "cwd": "/w",
    }
    entry.update(over)
    return entry


def _herdr(
    pid: int = 123,
    *,
    group: int | None = None,
    shell_pid: int = 99,
    procs: list[dict[str, Any]] | None = None,
) -> str:
    """The real `herdr pane process-info` shape with synthetic values."""
    return json.dumps(
        {
            "id": "r1",
            "result": {
                "type": "pane_process_info",
                "process_info": {
                    "pane_id": "p1",
                    "shell_pid": shell_pid,
                    "foreground_process_group_id": pid if group is None else group,
                    "foreground_processes": procs if procs is not None else [_proc(pid)],
                },
            },
        }
    )


def _ps(uid: int = UID) -> str:
    return f"{uid}  {START}\n"


# -- default runner -----------------------------------------------------------


def test_default_run_reports_stdout_success_and_failure() -> None:
    hello = proc_link.default_run((sys.executable, "-c", "print('hi')"), 5.0)
    assert hello == "hi\n"
    assert proc_link.default_run((sys.executable, "-c", "raise SystemExit(1)"), 5.0) is None
    assert proc_link.default_run(("/nonexistent-binary-xyz",), 1.0) is None


def test_default_run_times_out() -> None:
    assert (
        proc_link.default_run((sys.executable, "-c", "import time; time.sleep(30)"), 0.05) is None
    )


# -- process-info parsing -----------------------------------------------------


def test_group_pid_wins_among_several_and_shell_pid_is_ignored() -> None:
    run = FakeRun(
        {
            "herdr": _herdr(group=123, shell_pid=99, procs=[_proc(111), _proc(123), _proc(130)]),
            "ps": _ps(),
            "lsof": _lsof("/root/x"),
        }
    )
    obs = _probe(run)
    assert obs is not None and (obs.pid, obs.start) == (123, START)
    assert [argv for argv, _ in run.calls if "ps" in argv[0]] == [
        ("/bin/ps", "-p", "123", "-o", "uid=,lstart=")
    ]


def test_single_entry_needs_no_group_match() -> None:
    doc = json.loads(_herdr(123))
    doc["result"]["process_info"]["foreground_process_group_id"] = None
    for raw in (_herdr(123, group=999), json.dumps(doc)):
        run = FakeRun({"herdr": raw, "ps": _ps(), "lsof": _lsof("/root/x")})
        obs = _probe(run)
        assert obs is not None and obs.pid == 123


def test_several_entries_without_group_match_yield_nothing() -> None:
    run = FakeRun(
        {
            "herdr": _herdr(group=999, procs=[_proc(111), _proc(123)]),
            "ps": _ps(),
            "lsof": _lsof("/root/x"),
        }
    )
    assert _probe(run) is None
    assert not any("ps" in argv[0] for argv, _ in run.calls)


def test_ps_always_supplies_uid_and_start() -> None:
    run = FakeRun({"herdr": _herdr(), "ps": _ps(), "lsof": _lsof("/root/x")})
    obs = _probe(run)
    assert obs is not None and (obs.pid, obs.start) == (123, START)
    assert [argv for argv, _ in run.calls if "ps" in argv[0]] == [
        ("/bin/ps", "-p", "123", "-o", "uid=,lstart=")
    ]


def test_other_uid_is_rejected() -> None:
    run = FakeRun({"herdr": _herdr(), "ps": _ps(UID + 1), "lsof": ""})
    assert _probe(run) is None


def test_unusable_process_info_yields_nothing() -> None:
    docs = [
        "not json",
        "[]",
        "{}",
        json.dumps({"result": {}}),
        json.dumps({"result": {"process_info": {}}}),
        json.dumps({"result": {"process_info": {"foreground_processes": []}}}),
        json.dumps({"result": {"process_info": {"foreground_processes": [{"pid": "x"}]}}}),
        json.dumps({"result": {"process_info": {"foreground_processes": [{"pid": True}]}}}),
        _herdr(group=999, procs=[_proc(111), _proc(123)]),
    ]
    for doc in docs:
        run = FakeRun({"herdr": doc, "ps": _ps(), "lsof": _lsof("/root/x")})
        assert _probe(run) is None
    run = FakeRun({"herdr": None, "ps": _ps(), "lsof": _lsof("/root/x")})
    assert _probe(run) is None


def test_failed_lsof_yields_nothing() -> None:
    run = FakeRun({"herdr": _herdr(), "ps": _ps(), "lsof": None})
    assert _probe(run) is None


def test_deadline_is_honored_and_caps_timeouts() -> None:
    run = FakeRun({"herdr": _herdr(), "ps": _ps()})
    assert _probe(run, deadline=1000.0, now=1000.0) is None
    assert run.calls == []
    run = FakeRun({"herdr": _herdr(), "ps": _ps(), "lsof": _lsof("/root/x")})
    _probe(run, deadline=1000.4, now=1000.0)
    assert run.calls and all(timeout <= 0.4 + 1e-9 for _, timeout in run.calls)


def test_real_process_info_shape_end_to_end(tmp_path: Path) -> None:
    root = tmp_path / "root"
    day = "sessions/2026/10/11"
    junk = _proc(123, argv="not-a-list", cmdline=7, cwd=None, name={"x": 1})
    doc = json.loads(_herdr(group=123, shell_pid=99, procs=[_proc(111), junk]))
    doc["result"]["process_info"]["tty"] = "/dev/ttys000"
    run = FakeRun(
        {
            "herdr": json.dumps(doc),
            "ps": _ps(),
            "lsof": _lsof(f"{root}/{day}/s1/{LOCK_FILENAME}"),
        }
    )
    obs = proc_link.probe_pane(
        run,
        herdr=("herdr",),
        pane_id="p1",
        targets=(_target(root=root),),
        monotonic=lambda: 1000.0,
        deadline=1005.0,
    )
    assert obs is not None
    assert (obs.pid, obs.start) == (123, START)
    (target,) = obs.targets
    assert [(lock.session, lock.parent) for lock in target.locks] == [("s1", None)]


# -- lock filtering and session derivation ------------------------------------


def test_locks_resolve_to_sessions_in_deterministic_order(tmp_path: Path) -> None:
    root = tmp_path / "root"
    day = "sessions/2026/10/11"
    lsof = _lsof(
        f"{root}/{day}/s2/{LOCK_FILENAME}",
        f"{root}/{day}/s1/{LOCK_FILENAME}",
        f"{root}/{day}/s1/session.jsonl",
        f"/elsewhere/{day}/s9/.session.lock",
        f"{root}/{day}/s1/subagent/g1/{LOCK_FILENAME}",
        f"{root}/other/s3/{LOCK_FILENAME}",
    )
    run = FakeRun({"herdr": _herdr(), "ps": _ps(), "lsof": lsof})
    obs = proc_link.probe_pane(
        run,
        herdr=("herdr",),
        pane_id="p1",
        targets=(_target(root=root),),
        monotonic=lambda: 1000.0,
        deadline=1005.0,
    )
    assert obs is not None
    (target,) = obs.targets
    assert [(lock.session, lock.parent) for lock in target.locks] == [
        ("s1", None),
        ("s2", None),
    ]


def test_same_pid_serves_two_roots_with_source_relative_keys(tmp_path: Path) -> None:
    a, b = tmp_path / "a", tmp_path / "b"
    day = "sessions/2026/10/11"
    lsof = _lsof(f"{a}/{day}/s1/{LOCK_FILENAME}", f"{b}/{day}/s1/{LOCK_FILENAME}")
    run = FakeRun({"herdr": _herdr(), "ps": _ps(), "lsof": lsof})
    obs = proc_link.probe_pane(
        run,
        herdr=("herdr",),
        pane_id="p1",
        targets=(_target("h1/kind-a/f1", a), _target("h1/kind-a/f2", b)),
        monotonic=lambda: 1000.0,
        deadline=1005.0,
    )
    assert obs is not None
    assert [(t.source_key, [lk.session for lk in t.locks]) for t in obs.targets] == [
        ("h1/kind-a/f1", ["s1"]),
        ("h1/kind-a/f2", ["s1"]),
    ]


def test_no_usable_lock_is_a_confirmed_empty_observation(tmp_path: Path) -> None:
    run = FakeRun({"herdr": _herdr(), "ps": _ps(), "lsof": _lsof("/root/x")})
    obs = _probe(run)
    assert obs is not None and obs.targets[0].locks == ()


def test_observation_carries_no_paths(tmp_path: Path) -> None:
    root = tmp_path / "secret-root"
    lsof = _lsof(f"{root}/p/s1/{LOCK_FILENAME}")
    run = FakeRun({"herdr": _herdr(), "ps": _ps(), "lsof": lsof})
    obs = proc_link.probe_pane(
        run,
        herdr=("herdr",),
        pane_id="p1",
        targets=(_target(root=root),),
        monotonic=lambda: 1000.0,
        deadline=1005.0,
    )
    assert obs is not None
    assert "secret-root" not in repr(obs) and "secret-root" not in str(obs.targets)


def test_lockless_target_is_skipped() -> None:
    target = proc_link.ProcTarget(SOURCE, Path("/root"), READERS[TURNS_FORMAT])
    assert target.reader.lock_filename is None
    run = FakeRun({"herdr": _herdr(), "ps": _ps(), "lsof": _lsof("/root/x")})
    obs = proc_link.probe_pane(
        run,
        herdr=("herdr",),
        pane_id="p1",
        targets=(target,),
        monotonic=lambda: 1000.0,
        deadline=1005.0,
    )
    assert obs is not None and obs.targets == ()


# -- reader-exact session keys (condition 2) ----------------------------------


def _event_record(session: str) -> dict[str, Any]:
    return {
        "payload": {
            "event": {
                "kind": "goal_usage_attribution",
                "record": {
                    "usage_id": f"u-{session}",
                    "owner": {"session_id": session},
                    "quantity": {
                        "unit": "tokens",
                        "reported": True,
                        "input_tokens": 3,
                        "output_tokens": 1,
                    },
                },
            }
        },
        "record_type": "event",
        "recorded_at": NOW * 1000,
    }


def _write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")


def test_readers_pin_locks_to_their_own_layouts(tmp_path: Path) -> None:
    assert set(READERS) == {
        "jsonl-event-usage-v1",
        "jsonl-message-usage-v1",
        "json-session-turns-v1",
        "jsonl-cumulative-token-count-v1",
        "sqlite-session-usage-v1",
        "json-session-credits-v1",
    }
    for format_id, reader in sorted(READERS.items()):
        if format_id != EVENT_FORMAT:
            assert reader.lock_filename is None, format_id
            assert reader.lock_session_keys("sessions/2026/10/11/s1/.session.lock") == (
                None,
                None,
            )
    event = READERS[EVENT_FORMAT]
    assert event.lock_filename == LOCK_FILENAME
    root = tmp_path / "logs"
    _write_jsonl(
        root / "sessions" / "2026" / "10" / "11" / "sess-1" / "session.jsonl",
        [_event_record("sess-1")],
    )
    batch = event.read(root, Cursor(), Budget())
    assert batch.coverage == Coverage.UNVERIFIED
    (emitted,) = batch.events
    assert emitted.session_key == "sess-1"
    assert event.lock_session_keys("sessions/2026/10/11/sess-1/.session.lock") == (
        "sess-1",
        None,
    )
    assert event.lock_session_keys("sessions/2026/10/11/sess-1/session.jsonl") == (None, None)
    assert event.lock_session_keys("other/2026/10/11/sess-1/.session.lock") == (None, None)


# -- connector episodes -------------------------------------------------------


class _StubAdapter:
    async def bootstrap(self, subscriptions):  # pragma: no cover - unused here
        raise AssertionError

    async def session_snapshot(self):  # pragma: no cover - unused here
        raise AssertionError

    async def agent_list(self):  # pragma: no cover - unused here
        return []


class FakeProbe:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.obs: dict[str, proc_link.ProcObservation | None] = {}
        self.deadlines: list[float] = []

    def __call__(
        self,
        pane_id: str,
        targets: tuple[proc_link.ProcTarget, ...],
        monotonic,
        deadline: float,
    ):
        self.calls.append(pane_id)
        self.deadlines.append(deadline)
        return self.obs.get(pane_id)


def _obs(
    *locks: tuple[str, str | None],
    pid: int = 100,
    start: str = START,
    source: str = EVENT_SOURCE,
) -> proc_link.ProcObservation:
    return proc_link.ProcObservation(
        pid,
        start,
        (proc_link.ProcTargetLocks(source, tuple(proc_link.ProcLock(s, p) for s, p in locks)),),
    )


def _pane(pane_id: str, space: str, kind: str | None = "kind-a") -> dict[str, Any]:
    pane: dict[str, Any] = {"pane_id": pane_id, "workspace_id": space}
    if kind is not None:
        pane["agent"] = kind
    return pane


def _source(
    tmp_path: Path, probe: FakeProbe, mono: list[float], formats: tuple[str, ...] = (EVENT_FORMAT,)
) -> tuple[HerdrSource, list[int]]:
    now = [NOW]
    source = HerdrSource(
        _StubAdapter(),  # type: ignore[arg-type]
        clock=lambda: now[0],
        proc_probe=probe,
        proc_clock=lambda: mono[0],
    )
    roots = []
    for i in range(len(formats)):
        root = tmp_path / f"r{i}"
        root.mkdir(parents=True, exist_ok=True)
        roots.append(root)
    source.configure_usage(
        tuple(
            UsageSource("h1", "kind-a", fmt, root) for fmt, root in zip(formats, roots, strict=True)
        )
    )
    return source, now


def _rank2(maps: tuple[SessionMapping, ...]) -> tuple[SessionMapping, ...]:
    return tuple(m for m in maps if m.rank == 2)


def test_first_tick_creates_a_rank2_episode(tmp_path: Path) -> None:
    probe, mono = FakeProbe(), [1000.0]
    source, _ = _source(tmp_path, probe, mono)
    probe.obs["p1"] = _obs(("s1", None))
    source._usage_snapshot({"panes": [_pane("p1", "w1")]})
    (found,) = _rank2(source.usage_mappings())
    assert probe.calls == ["p1"]
    assert (found.session_key, found.workspace_id, found.first_seen_ms) == ("s1", "w1", NOW)
    assert found.interval_id.startswith("p2:") and found.source_key == EVENT_SOURCE


def test_second_tick_extends_without_new_interval(tmp_path: Path) -> None:
    probe, mono = FakeProbe(), [1000.0]
    source, now = _source(tmp_path, probe, mono)
    probe.obs["p1"] = _obs(("s1", None))
    source._usage_snapshot({"panes": [_pane("p1", "w1")]})
    (first,) = _rank2(source.usage_mappings())
    now[0] += proc_link.TICK_MS
    source._usage_snapshot({"panes": [_pane("p1", "w1")]})
    (second,) = _rank2(source.usage_mappings())
    assert probe.calls == ["p1", "p1"]
    assert (second.interval_id, second.first_seen_ms) == (first.interval_id, NOW)
    assert second.last_seen_ms == NOW + proc_link.TICK_MS


def test_changed_start_token_opens_a_new_window(tmp_path: Path) -> None:
    probe, mono = FakeProbe(), [1000.0]
    source, now = _source(tmp_path, probe, mono)
    probe.obs["p1"] = _obs(("s1", None), start="start-a")
    source._usage_snapshot({"panes": [_pane("p1", "w1")]})
    (first,) = _rank2(source.usage_mappings())
    now[0] += proc_link.TICK_MS
    probe.obs["p1"] = _obs(("s1", None), start="start-b")
    source._usage_snapshot({"panes": [_pane("p1", "w1")]})
    maps = _rank2(source.usage_mappings())
    assert len(maps) == 2
    old = [m for m in maps if m.interval_id == first.interval_id]
    assert len(old) == 1 and (old[0].first_seen_ms, old[0].last_seen_ms) == (NOW, NOW)
    new = [m for m in maps if m.interval_id != first.interval_id]
    assert len(new) == 1 and new[0].first_seen_ms == NOW + proc_link.TICK_MS


def test_two_panes_holding_one_lock_stay_distinct(tmp_path: Path) -> None:
    probe, mono = FakeProbe(), [1000.0]
    source, _ = _source(tmp_path, probe, mono)
    probe.obs["p1"] = _obs(("s1", None), pid=100)
    probe.obs["p2"] = _obs(("s1", None), pid=200)
    source._usage_snapshot({"panes": [_pane("p1", "w1"), _pane("p2", "w2")]})
    maps = _rank2(source.usage_mappings())
    assert sorted((m.session_key, m.workspace_id) for m in maps) == [("s1", "w1"), ("s1", "w2")]
    assert len({m.interval_id for m in maps}) == 2


def test_parent_and_subagent_locks_yield_two_episodes(tmp_path: Path) -> None:
    probe, mono = FakeProbe(), [1000.0]
    source, _ = _source(tmp_path, probe, mono)
    probe.obs["p1"] = _obs(("s1", None), ("g1", "s1"))
    source._usage_snapshot({"panes": [_pane("p1", "w1")]})
    maps = _rank2(source.usage_mappings())
    assert sorted(m.session_key for m in maps) == ["g1", "s1"]


def test_tick_cadence_and_idle_skip(tmp_path: Path) -> None:
    probe, mono = FakeProbe(), [1000.0]
    source, now = _source(tmp_path, probe, mono)
    assert source.usage_mappings() == () and probe.calls == []
    probe.obs["p1"] = _obs(("s1", None))
    source._usage_snapshot({"panes": [_pane("p1", "w1")]})
    source.usage_mappings()
    assert probe.calls == ["p1"]
    now[0] += proc_link.TICK_MS - 1
    source.usage_mappings()
    assert probe.calls == ["p1"]
    now[0] += 1
    source.usage_mappings()
    assert probe.calls == ["p1", "p1"]


def test_eight_pane_cap_with_round_robin(tmp_path: Path) -> None:
    probe, mono = FakeProbe(), [1000.0]
    source, now = _source(tmp_path, probe, mono)
    panes = [_pane(f"p{i:02d}", "w1") for i in range(10)]
    for pane in panes:
        probe.obs[pane["pane_id"]] = _obs((pane["pane_id"], None))
    source._usage_snapshot({"panes": panes})
    source.usage_mappings()
    assert probe.calls == [f"p{i:02d}" for i in range(8)]
    now[0] += proc_link.TICK_MS
    source._usage_snapshot({"panes": panes})
    source.usage_mappings()
    assert probe.calls[8:] == [f"p{i:02d}" for i in (8, 9, 0, 1, 2, 3, 4, 5)]


def test_wall_budget_stops_the_pass(tmp_path: Path) -> None:
    times = [1000.0, 1000.0, 9999.0]
    probe = FakeProbe()
    now = [NOW]
    source = HerdrSource(
        _StubAdapter(),  # type: ignore[arg-type]
        clock=lambda: now[0],
        proc_probe=probe,
        proc_clock=lambda: times.pop(0) if len(times) > 1 else times[0],
    )
    root = tmp_path / "r0"
    root.mkdir(parents=True, exist_ok=True)
    source.configure_usage((UsageSource("h1", "kind-a", EVENT_FORMAT, root),))
    probe.obs["p1"] = _obs(("s1", None))
    probe.obs["p2"] = _obs(("s2", None))
    source._usage_snapshot({"panes": [_pane("p1", "w1"), _pane("p2", "w1")]})
    source.usage_mappings()
    assert probe.calls == ["p1"]
    assert probe.deadlines == [1005.0]


def test_unconfigured_kind_rank1_and_lockless_formats_are_never_probed(
    tmp_path: Path,
) -> None:
    probe, mono = FakeProbe(), [1000.0]
    source, _ = _source(tmp_path, probe, mono, formats=(TURNS_FORMAT,))
    other = _pane("p9", "w1", kind="kind-z")
    reported = _pane("p8", "w1")
    reported["agent_session"] = {"agent": "kind-a", "kind": "id", "source": "herdr", "value": "s9"}
    idle = _pane("p7", "w1")
    source._usage_snapshot({"panes": [other, reported, idle]})
    maps = source.usage_mappings()
    assert probe.calls == []
    assert len(maps) == 1 and maps[0].session_key == "s9" and maps[0].rank == 1


def test_reported_identity_takes_over_and_supersedes(tmp_path: Path) -> None:
    probe, mono = FakeProbe(), [1000.0]
    source, now = _source(tmp_path, probe, mono)
    probe.obs["p1"] = _obs(("s1", None))
    source._usage_snapshot({"panes": [_pane("p1", "w1")]})
    assert len(_rank2(source.usage_mappings())) == 1
    now[0] += proc_link.TICK_MS
    taken = _pane("p1", "w1")
    taken["agent_session"] = {"agent": "kind-a", "kind": "id", "source": "herdr", "value": "s1"}
    source._usage_snapshot({"panes": [taken]})
    maps = source.usage_mappings()
    assert probe.calls == ["p1"]  # has_session panes are ineligible
    assert sorted(m.rank for m in maps) == [1, 2]
    assert [m for m in maps if m.rank == 2][0].last_seen_ms == NOW  # unextended


def test_probe_failure_leaves_mappings_intact(tmp_path: Path) -> None:
    def blowing_up(pane_id, targets, monotonic, deadline):
        raise RuntimeError("boom")

    now = [NOW]
    source = HerdrSource(
        _StubAdapter(),  # type: ignore[arg-type]
        clock=lambda: now[0],
        proc_probe=blowing_up,
        proc_clock=lambda: 1000.0,
    )
    root = tmp_path / "r0"
    root.mkdir(parents=True, exist_ok=True)
    source.configure_usage((UsageSource("h1", "kind-a", EVENT_FORMAT, root),))
    source._usage_snapshot({"panes": [_pane("p1", "w1")]})
    assert source.usage_mappings() == ()


def test_vanished_pane_drops_its_record_and_freezes_its_window(tmp_path: Path) -> None:
    probe, mono = FakeProbe(), [1000.0]
    source, now = _source(tmp_path, probe, mono)
    probe.obs["p1"] = _obs(("s1", None))
    source._usage_snapshot({"panes": [_pane("p1", "w1")]})
    assert len(_rank2(source.usage_mappings())) == 1
    now[0] += proc_link.TICK_MS
    source._usage_snapshot({"panes": []})
    maps = _rank2(source.usage_mappings())
    assert probe.calls == ["p1"]
    assert len(maps) == 1 and maps[0].last_seen_ms == NOW


def test_acknowledge_marks_rank2_persisted(tmp_path: Path) -> None:
    probe, mono = FakeProbe(), [1000.0]
    source, now = _source(tmp_path, probe, mono)
    probe.obs["p1"] = _obs(("s1", None))
    source._usage_snapshot({"panes": [_pane("p1", "w1")]})
    maps = source.usage_mappings()
    now[0] += proc_link.TICK_MS
    source._usage_snapshot({"panes": [_pane("p1", "w1")]})
    extended = source.usage_mappings()
    source.acknowledge_usage(EVENT_SOURCE, extended, ())
    (interval,) = {m.interval_id for m in extended if m.rank == 2}
    assert source._proc_episodes[interval].persisted[EVENT_SOURCE] == NOW + proc_link.TICK_MS
    assert maps[0].interval_id == interval


# -- store ranks --------------------------------------------------------------


def _store(tmp_path: Path) -> SqliteUsageStore:
    return SqliteUsageStore(tmp_path / "usage.db", "UTC", clock=lambda: NOW)


def _write(
    store: SqliteUsageStore,
    events: tuple[UsageEvent, ...],
    maps: tuple[SessionMapping, ...] = (),
    cwd: tuple[PaneCwdObservation, ...] = (),
) -> None:
    cursor = store.cursor(SOURCE)
    store.apply(
        "kind-a",
        Batch(events, cursor, Coverage.COMPLETE, cursor.revision),
        maps,
        source_key=SOURCE,
        cwd=cwd,
    )


def _rows(store: SqliteUsageStore) -> tuple[UsageRow, ...]:
    return store.query(UsageQuery("h1", "2026-10-11", "2026-10-11"), NOW).rows


def _workspace(rows: tuple[UsageRow, ...], space: str) -> UsageRow:
    return next(r for r in rows if r.scope == RowScope.WORKSPACE and r.workspace_id == space)


def _r2(space: str, interval: str, first: int = NOW, last: int = NOW + 200) -> SessionMapping:
    return replace(mapping(space, interval, first, last), rank=2)


def _digest_event(**kw) -> UsageEvent:
    return replace(event(**kw), cwd_digest="d1" * 16)


def _obs_cwd(space: str = "w1") -> PaneCwdObservation:
    return PaneCwdObservation(
        SOURCE, "kind-a", "c1", "h1", "d1" * 16, space, 0, NOW - 100, NOW + 300
    )


def test_rank2_alone_links_and_counts_as_exact(tmp_path: Path) -> None:
    with _store(tmp_path) as store:
        _write(store, (event(t=NOW + 100),), (_r2("w2", "p"),))
        row = _workspace(_rows(store), "w2")
        assert (row.linked_sessions, row.inferred_sessions, row.sessions) == (1, 0, 1)


def test_rank1_beats_rank2_on_conflict(tmp_path: Path) -> None:
    with _store(tmp_path) as store:
        _write(
            store,
            (event(t=NOW + 100),),
            (mapping("w1", "a", NOW, NOW + 200), _r2("w2", "p", NOW, NOW + 200)),
        )
        row = _workspace(_rows(store), "w1")
        assert (row.linked_sessions, row.sessions) == (1, 1)


def test_rank2_pair_in_one_space_still_links(tmp_path: Path) -> None:
    with _store(tmp_path) as store:
        _write(
            store,
            (event(t=NOW + 100),),
            (_r2("w2", "p", NOW, NOW + 200), _r2("w2", "q", NOW, NOW + 200)),
        )
        row = _workspace(_rows(store), "w2")
        assert row.sessions == 1


def test_ambiguous_rank2_never_falls_through_to_inferred(tmp_path: Path) -> None:
    with _store(tmp_path) as store:
        _write(
            store,
            (_digest_event(t=NOW + 100),),
            (_r2("w1", "p", NOW, NOW + 200), _r2("w2", "q", NOW, NOW + 200)),
            cwd=(_obs_cwd("w1"),),
        )
        rows = _rows(store)
        assert [row.scope for row in rows] == [RowScope.NOT_LINKED]
        assert rows[0].inferred_sessions == 0


def test_bad_rank_is_rejected(tmp_path: Path) -> None:
    with _store(tmp_path) as store:
        for rank in (0, 3):
            with pytest.raises(UsageUnavailable):
                _write(store, (), (replace(mapping("w1", "a"), rank=rank),))


def test_legacy_rows_migrate_as_rank1_and_win(tmp_path: Path) -> None:
    path = tmp_path / "usage.db"
    with _store(tmp_path):
        pass
    db = sqlite3.connect(path)
    db.execute("ALTER TABLE mappings DROP COLUMN rank")
    db.execute(
        "INSERT INTO mappings VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (SOURCE, "s1", "a", "h1", "kind-a", "w1", NOW, NOW + 200, 0, None, None),
    )
    db.commit()
    db.close()
    with _store(tmp_path) as store:
        names = [row[1] for row in store._db.execute("PRAGMA table_info(mappings)")]
        assert "rank" in names
        assert store._db.execute("SELECT rank FROM mappings").fetchone()[0] == 1
        _write(store, (event(t=NOW + 100),), (_r2("w2", "p", NOW, NOW + 200),))
        row = _workspace(_rows(store), "w1")
        assert row.sessions == 1


def test_retention_anchor_prefers_rank1(tmp_path: Path) -> None:
    old = NOW - 100 * 86400_000
    with _store(tmp_path) as store:
        _write(
            store,
            (event(t=NOW),),
            (
                mapping("w1", "a", old, old + 100),
                _r2("w2", "p", old, old + 200),
            ),
        )
        store.prune(NOW)
        left = store._db.execute("SELECT interval FROM mappings").fetchall()
        assert left == [("a",)]
