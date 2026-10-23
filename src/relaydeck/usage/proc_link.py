"""Rank-2 process-evidence prober: which session locks a pane's pid holds open.

A pane whose agent never reports a session id can still be linked when its
foreground process holds a session lock open under a configured usage root.
This module runs the bounded subprocess queries for one pane and returns
session keys only: paths never leave it, are never logged, and are never
stored. Every failure (timeout, parse error, uid mismatch) yields None and
simply leaves the episode unextended; absence of evidence is never evidence.

Subprocess budget per pane: `herdr pane process-info` (3 s) for the
foreground pid, then `/bin/ps` for uid/start (2 s — the document carries
neither), then `/usr/sbin/lsof -a -p <pid> -Fn` (3 s). Each call is further
capped by the caller's wall deadline. One probe covers every lock-bearing
source of the pane's kind; each source filters the same open-file list by
its own root, so session keys stay source-relative.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from relaydeck.usage.contracts import Reader

LSOF_BIN = "/usr/sbin/lsof"
PS_BIN = "/bin/ps"
LSOF_TIMEOUT_S = 3.0
PS_TIMEOUT_S = 2.0
HERDR_TIMEOUT_S = 3.0
MAX_LOCKS_PER_PID = 8

TICK_MS = 45_000
PANES_PER_PASS = 8
PASS_WALL_S = 5.0

# Type of the injectable subprocess runner: argv + timeout -> stdout, or None
# on any failure. Production uses `default_run`; tests inject fakes.
RunFn = Callable[[tuple[str, ...], float], str | None]


@dataclass(frozen=True)
class ProcTarget:
    """One lock-bearing source to filter the pane's open files against."""

    source_key: str
    root: Path
    reader: Reader


@dataclass(frozen=True)
class ProcLock:
    """One session lock held open: the linked session and its parent, if any."""

    session: str
    parent: str | None


@dataclass(frozen=True)
class ProcTargetLocks:
    """The locks found under one target's root (possibly none)."""

    source_key: str
    locks: tuple[ProcLock, ...]


@dataclass(frozen=True)
class ProcObservation:
    """A validated pane observation: pid, opaque start token, locks per target."""

    pid: int
    start: str
    targets: tuple[ProcTargetLocks, ...]


# The connector's probe seam: pane id + targets + clock + deadline.
ProbeFn = Callable[
    [str, tuple[ProcTarget, ...], Callable[[], float], float], ProcObservation | None
]


def default_run(argv: tuple[str, ...], timeout: float) -> str | None:
    """Run argv with no shell and no stdin; stdout on success, else None."""
    try:
        out = subprocess.run(  # noqa: S603 - fixed herdr subcommands, no shell
            argv, text=True, capture_output=True, stdin=subprocess.DEVNULL, timeout=timeout
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.stdout if out.returncode == 0 else None


def default_probe(herdr: tuple[str, ...]) -> ProbeFn:
    """The production probe seam over `default_run` for one herdr binary."""

    def run(
        pane_id: str,
        targets: tuple[ProcTarget, ...],
        monotonic: Callable[[], float],
        deadline: float,
    ) -> ProcObservation | None:
        return probe_pane(
            default_run,
            herdr=herdr,
            pane_id=pane_id,
            targets=targets,
            monotonic=monotonic,
            deadline=deadline,
        )

    return run


def _foreground_pid(run: RunFn, herdr: tuple[str, ...], pane_id: str, timeout: float) -> int | None:
    """The pane's foreground pid from `herdr pane process-info`.

    Reads `result.process_info.foreground_processes` only: the entry whose pid
    equals `foreground_process_group_id`, else the single entry. Several
    entries with no group match yield nothing. The shell pid, names, argv,
    cmdline and cwd are never read.
    """
    if not herdr or not isinstance(pane_id, str) or not 0 < len(pane_id) <= 256:
        return None
    out = run((*herdr, "pane", "process-info", "--pane", pane_id), timeout)
    if out is None:
        return None
    try:
        doc = json.loads(out)
    except ValueError:
        return None
    if not isinstance(doc, dict):
        return None
    result = doc.get("result")
    info = result.get("process_info") if isinstance(result, dict) else None
    if not isinstance(info, dict):
        return None
    entries = info.get("foreground_processes")
    if not isinstance(entries, list):
        return None
    pids: list[int] = [
        entry["pid"]
        for entry in entries
        if isinstance(entry, dict)
        and isinstance(entry.get("pid"), int)
        and not isinstance(entry.get("pid"), bool)
        and entry["pid"] > 0
    ]
    group: object = info.get("foreground_process_group_id")
    if isinstance(group, int) and not isinstance(group, bool) and group in pids:
        return group
    if len(pids) == 1:
        return pids[0]
    return None


def _ps_start(run: RunFn, pid: int, timeout: float) -> tuple[int | None, str | None]:
    """(uid, start) for exactly this pid; the only fallback, two fields only."""
    out = run((PS_BIN, "-p", str(pid), "-o", "uid=,lstart="), timeout)
    if out is None:
        return None, None
    for line in out.splitlines():
        cells = line.split(None, 1)
        if len(cells) != 2:
            continue
        try:
            uid = int(cells[0])
        except ValueError:
            continue
        start = cells[1].strip()
        if uid > 0 and start and len(start) <= 256:
            return uid, start
    return None, None


def _open_paths(run: RunFn, pid: int, timeout: float) -> list[str] | None:
    """Absolute paths the pid holds open, from `lsof -Fn` `n` lines."""
    out = run((LSOF_BIN, "-a", "-p", str(pid), "-Fn"), timeout)
    if out is None:
        return None
    return [line[1:] for line in out.splitlines() if line.startswith("n") and len(line) > 1]


def _lock_relpaths(paths: list[str], root: str, lock_filename: str) -> list[str]:
    """Root-relative lock paths, confined to root, in deterministic order."""
    try:
        real_root = os.path.realpath(root)
    except OSError:
        return []
    found: list[str] = []
    for path in paths:
        if os.path.basename(path) != lock_filename or not os.path.isabs(path):
            continue
        try:
            real = os.path.realpath(path)
        except OSError:
            continue
        if real == real_root or not real.startswith(real_root + os.sep):
            continue
        found.append(os.path.relpath(real, real_root))
    return sorted(found)


def probe_pane(
    run: RunFn,
    *,
    herdr: tuple[str, ...],
    pane_id: str,
    targets: tuple[ProcTarget, ...],
    monotonic: Callable[[], float],
    deadline: float,
) -> ProcObservation | None:
    """One bounded pane probe: validated pid/start plus held locks per target.

    Returns None when the pane has no usable foreground process, the process
    is another user's, or any query fails or the deadline passes. Each target's
    reader derives session keys from its lock relpaths with the same helper it
    uses for events, so probed locks link exactly the reported sessions.
    """
    if not targets:
        return None

    def remaining(cap: float) -> float | None:
        left = min(cap, deadline - monotonic())
        return left if left > 0 else None

    budget = remaining(HERDR_TIMEOUT_S)
    if budget is None:
        return None
    pid = _foreground_pid(run, herdr, pane_id, budget)
    if pid is None:
        return None
    # The document carries no uid or start time: both always come from ps.
    budget = remaining(PS_TIMEOUT_S)
    if budget is None:
        return None
    uid, start = _ps_start(run, pid, budget)
    if uid != os.getuid() or start is None:
        return None
    budget = remaining(LSOF_TIMEOUT_S)
    if budget is None:
        return None
    paths = _open_paths(run, pid, budget)
    if paths is None:
        return None
    per_target: list[ProcTargetLocks] = []
    for target in targets:
        if target.reader.lock_filename is None or not target.root.is_absolute():
            continue
        locks: list[ProcLock] = []
        rels = _lock_relpaths(paths, os.fspath(target.root), target.reader.lock_filename)
        for rel in rels[:MAX_LOCKS_PER_PID]:
            session, parent = target.reader.lock_session_keys(rel)
            if session is not None:
                locks.append(ProcLock(session, parent))
        per_target.append(ProcTargetLocks(target.source_key, tuple(locks)))
    return ProcObservation(pid, start, tuple(per_target))
