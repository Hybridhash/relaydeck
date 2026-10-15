"""Flet-free contracts for the limits plane.

The limits plane refreshes allowance snapshots on its own worker, outside the
token-ledger pass. Providers are pure: `fetch` gets everything it needs in the
context (including a bounded subprocess runner) and keeps no global state.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from relaydeck.usage.contracts import LimitError, LimitSnapshot, sanitize_unit
from relaydeck.usage.limits_plane.refresh_state import RefreshState


@dataclass(frozen=True)
class FetchContext:
    """Everything one provider fetch may use. `run` executes argv with the
    scheduler's sandbox: absolute executable, minimal env, own process group,
    hard timeout with process-group kill, bounded stdout, stderr discarded,
    no shell."""

    provider_key: str  # "host/kind/format": the configured block being refreshed
    kind: str
    format: str
    now_ms: int
    timeout_s: float
    run: Callable[[Sequence[str], float, Mapping[str, str] | None], bytes] = field(repr=False)
    # the block's configured argv (validated absolute executable) and the
    # per-install account-key path. Empty/None when the block carries none.
    command: tuple[str, ...] = ()
    key_path: Path | None = None
    # the scheduler's attempt id ("" for timer fetches), the
    # resolved TUI bundle for on-demand pane reads (else None), the most
    # recent linked workspace for the kind (tier-2 choice, else None), and the
    # shared refresh state handle (else the provider opens its own).
    attempt_id: str = ""
    tui: TuiBundle | None = None
    linked_workspace: str | None = None
    refresh: RefreshState | None = None


@dataclass(frozen=True)
class ProviderResult:
    """One fetch outcome. `error` set means failure: any snapshots in the same
    result are ignored. The previous stored reading (last-good) is kept only for a positively
    identified account and a transient error; an unknown identity or `auth_required` suppresses
    it."""

    snapshots: tuple[LimitSnapshot, ...] = ()
    error: LimitError | None = None
    # machine-readable attempt detail for the refresh record
    # ("no_space", "not_reported", "cleanup_failed"); "" means none. Never screen text.
    note: str = ""


# per-tool TUI recipe, loaded from catalog/config DATA (never code
# literals naming tools). All patterns are Python `re` syntax; the same source
# must also be valid Rust regex for `wait-output --regex` (markers only use a
# shared subset: literals, classes, `+*?`, groups, `(?m)`).
_RESET_KINDS = frozenset({"clock", "monthday", "date"})
_MAX_PATTERN_CHARS = 2048
_MAX_WINDOWS = 8
_MAX_ARG_CHARS = 1024
_ID_PLACEHOLDER = "{id}"
_KEY_RE = re.compile(r"[a-z0-9_]{1,32}\Z")
_DISMISS_RE = re.compile(r"[a-z+_-]{1,32}\Z")


@dataclass(frozen=True)
class TuiWindowSpec:
    """One allowance window: how to find it and what its numbers mean."""

    key: str
    pattern: str  # named groups: `percent`, or `used` + `limit`
    reset_pattern: str | None = None  # one named group: `reset`
    reset_kind: str | None = None  # "clock" | "monthday" | "date"
    unit: str | None = None  # `[a-z_]{1,16}`, for amount windows
    minutes: int | None = None


@dataclass(frozen=True)
class TuiRecipe:
    """The validated per-tool recipe for one `tui-usage-pane-v1` block."""

    launch_new: tuple[str, ...]
    launch_resume: tuple[str, ...] | None  # None = never resume, always fresh
    quit_text: str
    quit_marker: str | None  # None = process check only
    dismiss: str  # "" = none, else a key name such as "esc"
    prompt_marker: str
    usage_marker: str
    hint_pattern: str | None  # one named group: `id`; None = no id tracking
    id_pattern: str  # "" when hint_pattern is None
    session_root: str | None  # "~"-prefixed or absolute; None with no tracking
    not_found: tuple[str, ...]
    not_reported: tuple[str, ...]  # the tool answered but has no limits to show right now
    binary: str  # expected foreground basename of the tool
    plan_pattern: str | None  # one named group: `plan`
    windows: tuple[TuiWindowSpec, ...]
    max_lines: int = 60


@dataclass(frozen=True)
class TuiBundle:
    """Runtime-resolved inputs for one TUI fetch (spec-side view included)."""

    recipe: TuiRecipe
    herdr: tuple[str, ...]  # absolute herdr argv prefix; () = unavailable
    state_path: Path
    timezone: str
    socket_path: str


def _pattern(value: Any, label: str, *, groups: frozenset[str] = frozenset()) -> str:
    if not isinstance(value, str) or not 0 < len(value) <= _MAX_PATTERN_CHARS:
        raise ValueError(f"tui recipe: bad {label}")
    try:
        compiled = re.compile(value)
    except re.error as exc:
        raise ValueError(f"tui recipe: bad {label}") from exc
    if groups and groups - compiled.groupindex.keys():
        raise ValueError(f"tui recipe: {label} misses a group")
    return value


def _argv(value: Any, label: str, *, placeholder: bool) -> tuple[str, ...]:
    if (
        not isinstance(value, list)
        or not value
        or any(
            not isinstance(item, str)
            or not 0 < len(item) <= _MAX_ARG_CHARS
            or any(ord(c) < 32 for c in item)
            for item in value
        )
    ):
        raise ValueError(f"tui recipe: bad {label}")
    argv = tuple(value)
    if " ".join(argv).count(_ID_PLACEHOLDER) != (1 if placeholder else 0):
        raise ValueError(f"tui recipe: bad {label} placeholder")
    return argv


def tui_recipe_from(data: Any) -> TuiRecipe:
    """Validate catalog/config recipe DATA into a TuiRecipe (ValueError if bad)."""
    if not isinstance(data, dict):
        raise ValueError("tui recipe: not a table")
    resume = data.get("launch_resume")
    quit_marker = data.get("quit_marker")
    hint = data.get("hint_pattern")
    root = data.get("session_root")
    id_raw = data.get("id_pattern")
    if hint is not None and id_raw is None:
        raise ValueError("tui recipe: hint needs id_pattern")
    if id_raw is not None and hint is None:
        raise ValueError("tui recipe: id_pattern needs hint_pattern")
    plan = data.get("plan_pattern")
    dismiss = data.get("dismiss", "")
    if not isinstance(dismiss, str) or (dismiss and _DISMISS_RE.fullmatch(dismiss) is None):
        raise ValueError("tui recipe: bad dismiss")
    binary = data.get("binary")
    if (
        not isinstance(binary, str)
        or not 0 < len(binary) <= 128
        or "/" in binary
        or any(ord(c) < 32 for c in binary)
    ):
        raise ValueError("tui recipe: bad binary")
    if root is not None and (
        not isinstance(root, str)
        or ".." in root
        or not (root.startswith("~/") or root.startswith("/"))
    ):
        raise ValueError("tui recipe: bad session_root")
    if (hint is None) != (root is None):
        raise ValueError("tui recipe: hint and session_root go together")
    raw_windows = data.get("windows")
    if not isinstance(raw_windows, list) or not 0 < len(raw_windows) <= _MAX_WINDOWS:
        raise ValueError("tui recipe: bad windows")
    windows: list[TuiWindowSpec] = []
    for entry in raw_windows:
        if not isinstance(entry, dict):
            raise ValueError("tui recipe: bad window")
        key = entry.get("key")
        if not isinstance(key, str) or _KEY_RE.fullmatch(key) is None:
            raise ValueError("tui recipe: bad window key")
        pattern = _pattern(entry.get("pattern"), "window pattern")
        names = set(re.compile(pattern).groupindex)
        if names - {"percent", "used", "limit"}:
            raise ValueError("tui recipe: bad window groups")
        if "percent" in names and names & {"used", "limit"}:
            raise ValueError("tui recipe: percent xor amounts")
        if not names >= {"used", "limit"} and "percent" not in names:
            raise ValueError("tui recipe: window needs percent or used+limit")
        reset = entry.get("reset_pattern")
        kind = entry.get("reset_kind")
        if (reset is None) != (kind is None):
            raise ValueError("tui recipe: reset pattern and kind go together")
        if kind is not None and kind not in _RESET_KINDS:
            raise ValueError("tui recipe: bad reset kind")
        minutes = entry.get("minutes")
        if minutes is not None and (type(minutes) is not int or minutes <= 0):
            raise ValueError("tui recipe: bad minutes")
        unit = sanitize_unit(entry["unit"]) if "unit" in entry else None
        if "unit" in entry and unit is None:
            raise ValueError("tui recipe: bad unit")
        windows.append(
            TuiWindowSpec(
                key,
                pattern,
                _pattern(reset, "reset pattern", groups=frozenset({"reset"}))
                if reset is not None
                else None,
                kind,
                unit,
                minutes,
            )
        )
    max_lines = data.get("max_lines", 60)
    if type(max_lines) is not int or not 10 <= max_lines <= 200:
        raise ValueError("tui recipe: bad max_lines")
    raw_not_found = data.get("not_found", [])
    if not isinstance(raw_not_found, list):
        raise ValueError("tui recipe: bad not_found")
    raw_not_reported = data.get("not_reported", [])
    if not isinstance(raw_not_reported, list):
        raise ValueError("tui recipe: bad not_reported")
    return TuiRecipe(
        launch_new=_argv(data.get("launch_new"), "launch_new", placeholder=False),
        launch_resume=_argv(resume, "launch_resume", placeholder=True) if resume else None,
        quit_text=_argv([data.get("quit_text")], "quit_text", placeholder=False)[0],
        quit_marker=_pattern(quit_marker, "quit marker") if quit_marker is not None else None,
        dismiss=dismiss,
        prompt_marker=_pattern(data.get("prompt_marker"), "prompt marker"),
        usage_marker=_pattern(data.get("usage_marker"), "usage marker"),
        hint_pattern=_pattern(hint, "hint pattern", groups=frozenset({"id"}))
        if hint is not None
        else None,
        id_pattern=_pattern(id_raw, "id pattern") if id_raw is not None else "",
        session_root=root,
        not_found=tuple(_pattern(item, "not_found") for item in raw_not_found),
        not_reported=tuple(_pattern(item, "not_reported") for item in raw_not_reported),
        binary=binary,
        plan_pattern=_pattern(plan, "plan pattern", groups=frozenset({"plan"}))
        if plan is not None
        else None,
        windows=tuple(windows),
        max_lines=max_lines,
    )


@dataclass(frozen=True)
class RefreshDecision:
    """Outcome of one manual refresh request."""

    state: str  # "started" | "in_progress" | "cooldown" | "not_configured"
    attempt_id: str | None = None
    retry_after_s: int | None = None


@dataclass(frozen=True)
class ProviderSpec:
    """Scheduler-side view of one enabled `[[usage.limits]]` block."""

    provider_key: str
    kind: str
    format: str
    min_interval_ms: int
    timeout_s: float
    command: tuple[str, ...] = ()
    # manual-only specs are never time-due; `tui` carries the
    # resolved recipe bundle for `tui-usage-pane-v1` blocks, else None.
    on_demand: bool = False
    tui: TuiBundle | None = None


class LimitsProvider(Protocol):
    """A limits backend. Pure with respect to its inputs: no global state,
    no credential refresh, no writes outside the returned snapshots."""

    format_id: str  # versioned, e.g. "fake-limits-stdio-v1"

    def fetch(self, ctx: FetchContext) -> ProviderResult: ...
