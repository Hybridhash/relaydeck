"""On-demand TUI usage reads through Herdr panes.

Format id: `tui-usage-pane-v1`. One fetch opens a fresh `<kind>-relaydeck`
tab, starts the tool (resuming a proven session id when one is saved),
sends `/usage`, parses the allowance numbers from the screen, quits, captures
the quit hint when the recipe tracks sessions, and closes the tab. Every step
is bounded; every write is verified; every tab is journaled and closed.

Trust model:
- A session id is saved ONLY from the tool's own quit hint read live from our
  verified pane, corroborated per recipe (existence under the tool's session
  root plus birth time at or after our attempt start). Recipes without a hint
  pattern never track sessions. File mtimes are never a source.
- Every write targets the pane id from the create response and is preceded by
  a foreground-process check; screen text alone never authorizes a write.
- On unexpected state no quit text is sent: the verified owned tab is closed.
- Tab ids are never reused across attempts; at most two tabs exist per attempt
  (one definite dead-id retry), both journaled.
"""

from __future__ import annotations

import os
import re
import shlex
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from relaydeck.usage.contracts import (
    LimitError,
    LimitSnapshot,
    LimitStatus,
    LimitWindow,
    sanitize_plan_label,
)
from relaydeck.usage.limits_plane.contracts import (
    FetchContext,
    ProviderResult,
    TuiRecipe,
    TuiWindowSpec,
)
from relaydeck.usage.limits_plane.herdr_seam import (
    CliHerdrSeam,
    HerdrSeam,
    SeamError,
    SeamTimeout,
    run_from_context,
)
from relaydeck.usage.limits_plane.providers.jsonrpc_rate_limits_stdio import account_ref_for
from relaydeck.usage.limits_plane.refresh_state import (
    RefreshState,
    session_scope,
    socket_identity,
)

FORMAT_ID = "tui-usage-pane-v1"

NOTE_NO_SPACE = "no_space"
NOTE_CLEANUP_FAILED = "cleanup_failed"
NOTE_NOT_REPORTED = "not_reported"
NOTE_IDLE = "idle"
NOTE_REFUSED = "refused"
# Each abort carries a fixed step code so a failed read says where it stopped.
NOTE_DEADLINE = "deadline"
NOTE_PANE_LIST_FAILED = "pane_list_failed"
NOTE_TAB_LIST_FAILED = "tab_list_failed"
NOTE_CREATE_FAILED = "create_failed"
NOTE_CREATE_AMBIGUOUS = "create_ambiguous"
NOTE_NOT_SHELL_AT_START = "not_shell_at_start"
NOTE_EXITED_BEFORE_PROMPT = "exited_before_prompt"
NOTE_NEEDS_ATTENTION = "needs_attention"
NOTE_NO_USAGE_MARKER = "no_usage_marker"
NOTE_FOREGROUND_LOST = "foreground_lost"
NOTE_DISMISS_FAILED = "dismiss_failed"
NOTE_QUIT_FAILED = "quit_failed"
NOTE_PARSE_NO_WINDOW = "parse_no_window"
NOTE_NOT_CONFIGURED = "not_configured"
NOTE_SEAM_FAILED = "seam_failed"

QUOTA_KEY = "plan"
SHELL_NAMES = frozenset({"sh", "bash", "zsh", "fish", "dash", "ksh"})
MAX_AMOUNT = 1e18
MAX_PARSE_CHARS = 256 * 1024

_CALL_S = 10.0
_START_S = 30.0
_USAGE_S = 15.0
_QUIT_S = 15.0
_SHELL_S = 10.0
_IDLE_RECHECK_S = 5.0  # the tool may not have finished signing in when /usage first ran
_ENTER_RETRIES = 3
_TYPED_SETTLE_S = 0.8  # an Enter right behind typed text reads as part of a paste (Muse 1.4.10)
_ENTER_WAIT_MS = 6000
_SETTLE_WAIT_MS = 8000
RECOVERY_MAX_ENTRIES = 8

_MONTHS = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}


class _Abort(Exception):
    def __init__(self, error: LimitError, note: str = "") -> None:
        super().__init__(error.value)
        self.error = error
        self.note = note


class _Idle(Exception):
    """The screen says the first window has seen no use yet."""


class _RetryFresh(Exception):
    """The resumed id is definitely dead: clear it and run fresh in a new tab."""


def _matches_binary(found: str | None, names: frozenset[str]) -> bool:
    """A launcher may exec a versioned binary (`tool` runs `tool-bin-1.4.6-R6000.1`), so a
    basename equal to a name or starting with `<name>-` counts as that program."""
    if not found:
        return False
    return found in names or any(name and found.startswith(f"{name}-") for name in names)


def _foreground_basename(info: dict[str, Any] | None) -> str | None:
    """Basename of the foreground process (group match, else a single entry)."""
    if not info:
        return None
    procs = info.get("foreground_processes")
    if not isinstance(procs, list) or not procs:
        return None
    group = info.get("foreground_process_group_id")
    chosen: Any = None
    for proc in procs:
        if isinstance(proc, dict) and proc.get("pid") == group:
            chosen = proc
            break
    if chosen is None and len(procs) == 1 and isinstance(procs[0], dict):
        chosen = procs[0]
    if not isinstance(chosen, dict):
        return None
    raw = chosen.get("argv0") or chosen.get("name") or ""
    if not isinstance(raw, str) or not raw:
        return None
    return os.path.basename(raw) or None


def _parse_amount(text: str) -> float | None:
    cleaned = text.replace(",", "").replace(" ", "").replace("_", "")
    try:
        value = float(cleaned)
    except ValueError:
        return None
    if not (0 <= value < MAX_AMOUNT) or value != value:
        return None
    return value


def _clock_to_ms(text: str, now_ms: int, zone: ZoneInfo) -> int | None:
    """'10:22 PM' -> today at that time, host zone. Past stays past (the
    store then marks the window expired); nothing is pushed to tomorrow."""
    match = re.fullmatch(r"([0-9]{1,2}):([0-9]{2}) ([AP]M)", text.strip())
    if match is None:
        return None
    hour, minute, meridiem = int(match.group(1)), int(match.group(2)), match.group(3)
    if not 1 <= hour <= 12 or minute > 59:
        return None
    hour = hour % 12 + (12 if meridiem == "PM" else 0)
    now = datetime.fromtimestamp(now_ms / 1000, zone)
    try:
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    except ValueError:
        return None
    return int(target.timestamp() * 1000)


def _monthday_to_ms(text: str, now_ms: int, zone: ZoneInfo) -> int | None:
    """'Oct 19 at 3:00 AM' -> this year, or last year when that would be more
    than 30 days out (a weekly-style window never resets that far ahead)."""
    match = re.fullmatch(
        r"([A-Za-z]+) ([0-9]{1,2}) at ([0-9]{1,2}):([0-9]{2}) ([AP]M)", text.strip()
    )
    if match is None:
        return None
    month = _MONTHS.get(match.group(1).lower()[:3])
    day, hour, minute = (int(match.group(n)) for n in (2, 3, 4))
    meridiem = match.group(5)
    if month is None or not 1 <= hour <= 12 or minute > 59:
        return None
    hour = hour % 12 + (12 if meridiem == "PM" else 0)
    now = datetime.fromtimestamp(now_ms / 1000, zone)
    try:
        target = now.replace(month=month, day=day, hour=hour, minute=minute)
        target = target.replace(second=0, microsecond=0)
    except ValueError:
        return None
    if (target - now).total_seconds() > 30 * 86400:
        try:
            target = target.replace(year=target.year - 1)
        except ValueError:
            return None
    return int(target.timestamp() * 1000)


def _date_to_ms(text: str, zone: ZoneInfo) -> int | None:
    """'2026-11-01' -> start of that day, host zone."""
    match = re.fullmatch(r"([0-9]{4})-([0-9]{2})-([0-9]{2})", text.strip())
    if match is None:
        return None
    year, month, day = (int(match.group(n)) for n in (1, 2, 3))
    if not 2000 <= year <= 2100:
        return None
    try:
        target = datetime(year, month, day, tzinfo=zone)
    except ValueError:
        return None
    return int(target.timestamp() * 1000)


def _reset_to_ms(text: str, kind: str, now_ms: int, time_zone: str) -> int | None:
    try:
        zone = ZoneInfo(time_zone)
    except (ZoneInfoNotFoundError, ValueError):
        return None
    if kind == "clock":
        return _clock_to_ms(text, now_ms, zone)
    if kind == "monthday":
        return _monthday_to_ms(text, now_ms, zone)
    if kind == "date":
        return _date_to_ms(text, zone)
    return None


@dataclass
class _Tab:
    tab_id: str
    pane_id: str
    workspace: str


class _Run:
    def __init__(self, ctx: FetchContext) -> None:
        bundle = ctx.tui
        assert bundle is not None  # checked by fetch()
        self.ctx = ctx
        self.bundle = bundle
        self.recipe: TuiRecipe = bundle.recipe
        self.seam: HerdrSeam = CliHerdrSeam(bundle.herdr, run_from_context(ctx.run))
        self.state = ctx.refresh or RefreshState(bundle.state_path)
        self.deadline = time.monotonic() + ctx.timeout_s
        self.label = f"{ctx.kind}-relaydeck"
        self.server = socket_identity(bundle.socket_path)
        self.cleanup_failed = False
        self.plan_label: str | None = None
        self.idle = False

    def budget(self, cap: float) -> float:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise _Abort(LimitError.TIMEOUT, NOTE_DEADLINE)
        return min(cap, remaining)

    # -- workspace choice ----------------------------------------------------

    def choose_workspace(self) -> tuple[str, str | None]:
        """(workspace_id, cwd or None). Raises _Abort(no_space) when nowhere fits."""
        try:
            panes = self.seam.pane_list(self.budget(_CALL_S))
        except (SeamTimeout, SeamError):
            raise _Abort(LimitError.UNAVAILABLE, NOTE_PANE_LIST_FAILED) from None
        for pane in panes:
            if not isinstance(pane, dict) or pane.get("agent") != self.ctx.kind:
                continue
            workspace, cwd = pane.get("workspace_id"), pane.get("cwd")
            if (
                isinstance(workspace, str)
                and workspace
                and isinstance(cwd, str)
                and cwd.startswith("/")
                and "\x00" not in cwd
                and Path(cwd).is_dir()
            ):
                return workspace, cwd
        linked = self.ctx.linked_workspace
        if isinstance(linked, str) and linked and "\x00" not in linked:
            return linked, None
        raise _Abort(LimitError.UNAVAILABLE, NOTE_NO_SPACE)

    # -- tab lifecycle -----------------------------------------------------------

    def _journal(self, phase: str, tab: _Tab | None = None, note: str = "") -> None:
        entry: dict[str, Any] = {
            "attempt": self.ctx.attempt_id,
            "phase": phase,
            "label": self.label,
            "at_ms": self.ctx.now_ms,
        }
        if tab is not None:
            entry["tab"] = tab.tab_id
            entry["pane"] = tab.pane_id
            entry["workspace"] = tab.workspace
        if self.server is not None:
            entry["server"] = {
                "sock": self.bundle.socket_path,
                "dev": self.server[0],
                "ino": self.server[1],
            }
        if note:
            entry["note"] = note[:256]
        self.state.journal_append(entry)

    def _adopt_uncertain_create(self, workspace: str, before: set[str]) -> _Tab:
        """Reconcile a timed-out create: adopt exactly one new own-label tab."""
        try:
            tabs = self.seam.tab_list(workspace, self.budget(_CALL_S))
        except (SeamTimeout, SeamError):
            raise _Abort(LimitError.UNAVAILABLE, NOTE_TAB_LIST_FAILED) from None
        fresh = [
            tab
            for tab in tabs
            if isinstance(tab, dict)
            and tab.get("label") == self.label
            and isinstance(tab.get("tab_id"), str)
            and tab["tab_id"] not in before
        ]
        if len(fresh) != 1:
            self._journal("uncertain", note="create_reconcile_ambiguous")
            raise _Abort(LimitError.UNAVAILABLE, NOTE_CREATE_AMBIGUOUS)
        tab_id = fresh[0]["tab_id"]
        try:
            panes = self.seam.pane_list(self.budget(_CALL_S))
        except (SeamTimeout, SeamError):
            raise _Abort(LimitError.UNAVAILABLE, NOTE_PANE_LIST_FAILED) from None
        panes = [p for p in panes if isinstance(p, dict) and p.get("tab_id") == tab_id]
        if len(panes) != 1 or not isinstance(panes[0].get("pane_id"), str):
            self._journal("uncertain", note="create_pane_ambiguous")
            raise _Abort(LimitError.UNAVAILABLE, NOTE_CREATE_AMBIGUOUS)
        return _Tab(tab_id, panes[0]["pane_id"], workspace)

    def create_tab(self, workspace: str, cwd: str | None) -> _Tab:
        self._journal("intent", note=workspace)
        try:
            before = {
                tab["tab_id"]
                for tab in self.seam.tab_list(workspace, self.budget(_CALL_S))
                if isinstance(tab, dict)
                and tab.get("label") == self.label
                and isinstance(tab.get("tab_id"), str)
            }
        except (SeamTimeout, SeamError):
            raise _Abort(LimitError.UNAVAILABLE, NOTE_TAB_LIST_FAILED) from None
        try:
            tab_id, pane_id = self.seam.tab_create(workspace, cwd, self.label, self.budget(_CALL_S))
        except SeamTimeout:
            tab = self._adopt_uncertain_create(workspace, before)
            self._journal("created", tab, note="reconciled")
            return tab
        except SeamError:
            raise _Abort(LimitError.UNAVAILABLE, NOTE_CREATE_FAILED) from None
        tab = _Tab(tab_id, pane_id, workspace)
        self._journal("created", tab)
        return tab

    def verified_close(self, tab: _Tab) -> bool:
        """Close only a proven-ours tab. True when it is gone (or was)."""
        try:
            live = self.seam.tab_get(tab.tab_id, self.budget(_CALL_S))
        except (SeamTimeout, SeamError):
            self._journal("uncertain", tab, note="close_probe_failed")
            return False
        if live is None:
            self._journal("closed", tab, note="already_gone")
            return True
        if live.get("label") != self.label or live.get("workspace_id") != tab.workspace:
            self._journal("uncertain", tab, note="close_identity_mismatch")
            return False
        if self.server is None or socket_identity(self.bundle.socket_path) != self.server:
            self._journal("uncertain", tab, note="close_server_unknown")
            return False
        try:
            self.seam.tab_close(tab.tab_id, self.budget(_CALL_S))
        except (SeamTimeout, SeamError):
            self._journal("uncertain", tab, note="close_failed")
            return False
        self._journal("closed", tab)
        return True

    def close_quietly(self, tab: _Tab) -> None:
        try:
            ok = self.verified_close(tab)
        except _Abort:
            ok = False
        if not ok:
            self.cleanup_failed = True

    # -- foreground checks ----------------------------------------------------------

    def foreground(self, pane_id: str) -> str | None:
        info = self.seam.process_info(pane_id, self.budget(_CALL_S))
        return _foreground_basename(info)

    def require_foreground(self, pane_id: str, names: frozenset[str]) -> None:
        if not _matches_binary(self.foreground(pane_id), names):
            raise _Abort(LimitError.TIMEOUT, NOTE_FOREGROUND_LOST)

    # -- the interactive flow ----------------------------------------------------------

    def send_and_confirm(self, pane_id: str, text: str | None, marker: str, wait_ms: int) -> None:
        """Send text/enter, retrying Enter until the marker shows (every
        write is verified; screen text plus the pre-write foreground check)."""
        self.require_foreground(pane_id, frozenset({self.recipe.binary}))
        for attempt in range(_ENTER_RETRIES):
            self.require_foreground(pane_id, frozenset({self.recipe.binary}))
            if text is not None and (attempt == 0 or self.input_empty(pane_id)):
                # A command run before the tool finished loading prints a panel without the
                # marker; once the input is empty again, a fresh run prints the full one.
                self.seam.send_text(pane_id, text, self.budget(_CALL_S))
                time.sleep(_TYPED_SETTLE_S)
            self.seam.send_keys(pane_id, ("enter",), self.budget(_CALL_S))
            if self.seam.wait_output(
                pane_id,
                marker,
                _ENTER_WAIT_MS,
                self.recipe.max_lines,
                self.budget(_ENTER_WAIT_MS / 1000 + 5.0),
            ):
                return
        if not self.seam.wait_output(
            pane_id,
            marker,
            wait_ms,
            self.recipe.max_lines,
            self.budget(wait_ms / 1000 + 5.0),
        ):
            raise _Abort(LimitError.TIMEOUT, NOTE_NO_USAGE_MARKER)

    def input_empty(self, pane_id: str) -> bool:
        screen = self.seam.pane_read(pane_id, self.recipe.max_lines, self.budget(_CALL_S))
        marks = list(re.finditer(self.recipe.prompt_marker, screen))
        if not marks or self.is_refusal(screen):  # a refusal answered it; never ask twice
            return False
        return not screen[marks[-1].end() :].split("\n", 1)[0].strip()

    def scope(self, workspace: str) -> str:
        root = self.recipe.session_root or ""
        return session_scope(self.ctx.kind, root, workspace)

    def launch(self, tab: _Tab, resume_id: str | None) -> str | None:
        """Start the tool; returns the resumed id (or None for fresh). A
        definitely dead resumed id clears the saved id and raises _RetryFresh."""
        if self.foreground(tab.pane_id) not in SHELL_NAMES:
            raise _Abort(LimitError.UNAVAILABLE, NOTE_NOT_SHELL_AT_START)
        if resume_id is not None:
            if self.recipe.launch_resume is None:
                resume_id = None
            elif re.fullmatch(self.recipe.id_pattern, resume_id) is None:
                self.state.clear_session(self.scope(tab.workspace))
                resume_id = None
        template = self.recipe.launch_resume if resume_id is not None else self.recipe.launch_new
        if resume_id is not None and template is not None:
            argv = [item.replace("{id}", resume_id) for item in template]
        else:
            argv = list(self.recipe.launch_new)
            resume_id = None
        self.seam.pane_run(tab.pane_id, shlex.join(argv), self.budget(_CALL_S))
        if self.seam.wait_output(
            tab.pane_id,
            self.recipe.prompt_marker,
            int(_START_S * 1000),
            self.recipe.max_lines,
            self.budget(_START_S + 5.0),
        ):
            self.require_foreground(tab.pane_id, frozenset({self.recipe.binary}))
            return resume_id
        # No prompt: fast exit, dead id, login wall or hang. Read once to tell
        # a definite dead id from everything else (which aborts, id kept).
        screen = self.seam.pane_read(tab.pane_id, self.recipe.max_lines, self.budget(_CALL_S))
        if resume_id is not None and any(
            re.search(pattern, screen, re.MULTILINE) for pattern in self.recipe.not_found
        ):
            self.state.clear_session(self.scope(tab.workspace))
            raise _RetryFresh()
        if resume_id is not None and not self.session_file_present(resume_id):
            self.state.clear_session(self.scope(tab.workspace))
            raise _RetryFresh()
        if self.foreground(tab.pane_id) in SHELL_NAMES and resume_id is None:
            raise _Abort(LimitError.UNAVAILABLE, NOTE_EXITED_BEFORE_PROMPT)
        # A start-up or sign-in screen needs the user; never answer it.
        raise _Abort(LimitError.TIMEOUT, NOTE_NEEDS_ATTENTION)

    def session_root(self) -> Path | None:
        root = self.recipe.session_root
        if root is None:
            return None
        if root.startswith("~/"):
            return Path.home() / root[2:]
        return Path(root)

    def session_file_present(self, session_id: str) -> bool:
        root = self.session_root()
        if root is None or "/" in session_id or session_id in (".", ".."):
            return False
        try:
            return (root / f"{session_id}.json").is_file()
        except OSError:
            return False

    def parse_windows(self, screen: str) -> list[LimitWindow]:
        now_ms = self.ctx.now_ms
        screen = screen[-MAX_PARSE_CHARS:]
        if self.is_refusal(screen):
            # The tool answered as a model would: nothing was read, and asking again could bill.
            raise _Abort(LimitError.PARSE, NOTE_REFUSED)
        windows: list[LimitWindow] = []
        for spec in self.recipe.windows:
            window = self.parse_window(spec, screen, now_ms)
            if window is not None:
                windows.append(window)
        if not windows:
            if self.is_idle(screen):
                raise _Idle()
            if any(re.search(pattern, screen) for pattern in self.recipe.not_reported):
                raise _Abort(LimitError.UNAVAILABLE, NOTE_NOT_REPORTED)
            raise _Abort(LimitError.PARSE, NOTE_PARSE_NO_WINDOW)
        return windows

    def is_refusal(self, screen: str) -> bool:
        return any(
            re.search(pattern, screen[-MAX_PARSE_CHARS:], re.MULTILINE)
            for pattern in self.recipe.refuse
        )

    def is_idle(self, screen: str) -> bool:
        return any(re.search(pattern, screen[-MAX_PARSE_CHARS:]) for pattern in self.recipe.idle)

    def idle_windows(self) -> list[LimitWindow]:
        """Idle is 0 % of the first window with no reset; the other windows are not reported."""
        spec = self.recipe.windows[0]
        return [LimitWindow(spec.key, 0.0, spec.minutes, None, LimitStatus.FRESH, idle=True)]

    def parse_window(self, spec: TuiWindowSpec, screen: str, now_ms: int) -> LimitWindow | None:
        matches = list(re.finditer(spec.pattern, screen, re.MULTILINE))
        if not matches:
            return None
        groups = matches[-1].groupdict()
        percent: float | None = None
        used = limit = None
        if groups.get("percent") is not None:
            try:
                percent = float(groups["percent"])
            except ValueError:
                return None
            if not 0 <= percent <= 100:
                return None
        else:
            raw_used, raw_limit = groups.get("used"), groups.get("limit")
            if raw_used is None or raw_limit is None:
                return None
            used, limit = _parse_amount(raw_used), _parse_amount(raw_limit)
            if used is None or limit is None or limit <= 0:
                return None
            percent = used / limit * 100
        over = percent > 100
        if over:
            percent = 100.0
        resets_at = None
        if spec.reset_pattern is not None and spec.reset_kind is not None:
            found = list(re.finditer(spec.reset_pattern, screen, re.MULTILINE))
            if found:
                resets_at = _reset_to_ms(
                    found[-1].group("reset"), spec.reset_kind, now_ms, self.bundle.timezone
                )
        return LimitWindow(
            spec.key,
            percent,
            spec.minutes,
            resets_at,
            LimitStatus.FRESH,
            used_amount=used,
            limit_amount=limit,
            unit=spec.unit,
            over_allowance=over,
        )

    def parse_plan(self, screen: str) -> str | None:
        if self.recipe.plan_pattern is None:
            return None
        found = list(re.finditer(self.recipe.plan_pattern, screen[-MAX_PARSE_CHARS:], re.MULTILINE))
        if not found:
            return None
        return sanitize_plan_label(found[-1].group("plan"))

    def capture_hint(self, tab: _Tab, resumed_id: str | None) -> None:
        """Save the quit-hint id with corroboration, or clear a proven-wrong one."""
        if self.recipe.hint_pattern is None:
            return
        screen = self.seam.pane_read(tab.pane_id, self.recipe.max_lines, self.budget(_CALL_S))
        found = list(re.finditer(self.recipe.hint_pattern, screen, re.MULTILINE))
        scope = self.scope(tab.workspace)
        if not found:
            # No provenance for any id: a resumed id that prints no hint is
            # not proven to be ours.
            if resumed_id is not None:
                self.state.clear_session(scope)
            return
        hinted = found[-1].group("id")
        if (
            re.fullmatch(self.recipe.id_pattern, hinted) is None
            or "/" in hinted
            or hinted in (".", "..")
            or not self.session_file_present(hinted)
        ):
            if resumed_id is not None and hinted != resumed_id:
                self.state.clear_session(scope)
            return
        if resumed_id is None:
            if not self.session_birth_after_intent(hinted):
                return
            self.state.save_session(scope, hinted, "hint")
        elif hinted != resumed_id:
            # We landed in a different session than the one resumed: never
            # adopt it (it may be the owner's); drop the stale id.
            self.state.clear_session(scope)
        # else: resumed and confirmed; the saved id stands as is.

    def session_birth_after_intent(self, session_id: str) -> bool:
        root = self.session_root()
        if root is None:
            return False
        try:
            birth = getattr(os.stat(root / f"{session_id}.json"), "st_birthtime", None)
        except OSError:
            return False
        return birth is not None and birth * 1000 >= self.ctx.now_ms

    def run_in_tab(self, tab: _Tab, resume_id: str | None) -> list[LimitWindow]:
        launched = self.launch(tab, resume_id)
        try:
            self.send_and_confirm(
                tab.pane_id, "/usage", self.recipe.usage_marker, int(_USAGE_S * 1000)
            )
        except _Abort as abort:
            if abort.error is LimitError.TIMEOUT and self.recipe.refuse:
                # No usage panel: tell a model's answer from a slow start; neither is retried.
                screen = self.seam.pane_read(
                    tab.pane_id, self.recipe.max_lines, self.budget(_CALL_S)
                )
                if self.is_refusal(screen):
                    raise _Abort(LimitError.PARSE, NOTE_REFUSED) from None
            raise
        screen = self.seam.pane_read(tab.pane_id, self.recipe.max_lines, self.budget(_CALL_S))
        try:
            try:
                windows = self.parse_windows(screen)
            except _Idle:
                # One more look before believing it: right after start the screen can lag.
                time.sleep(_IDLE_RECHECK_S)
                self.send_and_confirm(
                    tab.pane_id, "/usage", self.recipe.usage_marker, int(_USAGE_S * 1000)
                )
                screen = self.seam.pane_read(
                    tab.pane_id, self.recipe.max_lines, self.budget(_CALL_S)
                )
                try:
                    windows = self.parse_windows(screen)
                except _Idle:
                    windows = self.idle_windows()
                    self.idle = True
        except _Abort:
            self.quit_cleanly(tab)
            raise
        plan_holder: list[str | None] = [self.parse_plan(screen)]
        if self.recipe.dismiss:
            self.require_foreground(tab.pane_id, frozenset({self.recipe.binary}))
            self.seam.send_keys(tab.pane_id, (self.recipe.dismiss,), self.budget(_CALL_S))
            if not self.seam.wait_output(
                tab.pane_id,
                self.recipe.prompt_marker,
                _SETTLE_WAIT_MS,
                self.recipe.max_lines,
                self.budget(_SETTLE_WAIT_MS / 1000 + 5.0),
            ):
                raise _Abort(LimitError.TIMEOUT, NOTE_DISMISS_FAILED)
        try:
            self.quit_cleanly(tab)
        except _Abort as abort:
            if abort.note != NOTE_QUIT_FAILED:
                raise
            # The reading is complete; closing the tab ends a tool that ignored the quit.
            self._journal("uncertain", tab, note=NOTE_QUIT_FAILED)
        self.capture_hint(tab, launched)
        if self.foreground(tab.pane_id) not in SHELL_NAMES:
            # Data complete and the tool was told to quit; a hung quit still
            # ends at tab close. Journal it, keep the reading.
            self._journal("uncertain", tab, note="shell_not_back")
        self.plan_label = plan_holder[0]
        return windows

    def poll_shell(self, pane_id: str, wait_s: float) -> bool:
        deadline = time.monotonic() + wait_s
        while time.monotonic() < deadline:
            try:
                if self.foreground(pane_id) in SHELL_NAMES:
                    return True
            except (SeamTimeout, SeamError):
                return False
            time.sleep(0.5)
        return False

    def quit_cleanly(self, tab: _Tab) -> None:
        """Quit with verification; unknown states get no quit text."""
        self.require_foreground(tab.pane_id, frozenset({self.recipe.binary}))
        self.seam.send_text(tab.pane_id, self.recipe.quit_text, self.budget(_CALL_S))
        time.sleep(_TYPED_SETTLE_S)
        for _ in range(_ENTER_RETRIES):
            self.require_foreground(tab.pane_id, frozenset({self.recipe.binary}))
            self.seam.send_keys(tab.pane_id, ("enter",), self.budget(_CALL_S))
            if self.recipe.quit_marker is not None:
                if self.seam.wait_output(
                    tab.pane_id,
                    self.recipe.quit_marker,
                    _ENTER_WAIT_MS,
                    self.recipe.max_lines,
                    self.budget(_ENTER_WAIT_MS / 1000 + 5.0),
                ):
                    return
            elif self.poll_shell(tab.pane_id, _ENTER_WAIT_MS / 1000):
                return
        if self.recipe.quit_marker is not None:
            if not self.seam.wait_output(
                tab.pane_id,
                self.recipe.quit_marker,
                int(_QUIT_S * 1000),
                self.recipe.max_lines,
                self.budget(_QUIT_S + 5.0),
            ):
                raise _Abort(LimitError.TIMEOUT, NOTE_QUIT_FAILED)
        elif not self.poll_shell(tab.pane_id, _QUIT_S):
            raise _Abort(LimitError.TIMEOUT, NOTE_QUIT_FAILED)

    def execute(self) -> tuple[tuple[LimitSnapshot, ...], str]:
        workspace, cwd = self.choose_workspace()
        tab: _Tab | None = None
        try:
            tab = self.create_tab(workspace, cwd)
            saved = self.state.saved_session(self.scope(workspace))
            resume_id = (
                saved.session_id
                if saved is not None and self.recipe.launch_resume is not None
                else None
            )
            try:
                windows = self.run_in_tab(tab, resume_id)
            except _RetryFresh:
                assert tab is not None
                self.close_quietly(tab)
                tab = self.create_tab(workspace, cwd)
                windows = self.run_in_tab(tab, None)
            snapshot = LimitSnapshot(
                self.ctx.kind,
                QUOTA_KEY,
                self.ctx.now_ms,
                tuple(windows),
                LimitStatus.FRESH,
                FORMAT_ID,
                # A pane shows no account id; kind + plan names a stable identity so a transient
                # failure keeps this reading instead of deleting it.
                account_ref_for(self.ctx.key_path, f"{self.ctx.kind}:{self.plan_label or ''}"),
                None,
                self.plan_label,
            )
            snapshots = (snapshot,)
        finally:
            if tab is not None:
                self.close_quietly(tab)
        note = NOTE_CLEANUP_FAILED if self.cleanup_failed else NOTE_IDLE if self.idle else ""
        return snapshots, note


class TuiUsagePaneProvider:
    """Stateless singleton: the scheduler supplies recipe, state and runner
    per fetch. The provider persists run-scoped journal/session records via
    the supplied refresh state (its only writes); snapshots carry the data."""

    format_id: str = FORMAT_ID

    def fetch(self, ctx: FetchContext) -> ProviderResult:
        bundle = ctx.tui
        if (
            bundle is None
            or not bundle.herdr
            or not ctx.attempt_id
            or not ctx.kind
            or ctx.timeout_s <= 0
        ):
            return ProviderResult((), LimitError.UNAVAILABLE, note=NOTE_NOT_CONFIGURED)
        try:
            snapshots, note = _Run(ctx).execute()
        except _Abort as abort:
            return ProviderResult((), abort.error, note=abort.note)
        except _RetryFresh:
            return ProviderResult((), LimitError.UNAVAILABLE, note=NOTE_SEAM_FAILED)
        except (SeamTimeout, SeamError):
            return ProviderResult((), LimitError.UNAVAILABLE, note=NOTE_SEAM_FAILED)
        return ProviderResult(snapshots, None, note=note)


def recover_stale_tabs(
    state: RefreshState,
    herdr: tuple[str, ...],
    socket_path: str,
    run: Any = None,
    *,
    seam: HerdrSeam | None = None,
    now_ms: int = 0,
    max_entries: int = RECOVERY_MAX_ENTRIES,
) -> dict[str, int]:
    """Close proven-ours leftover tabs (restart recovery). Never touches an
    unverifiable tab. Returns outcome counts."""
    outcome = {"closed": 0, "recovered": 0, "replaced": 0, "left": 0}
    if seam is None:
        if not herdr or run is None:
            return outcome
        seam = CliHerdrSeam(herdr, run)
    current = socket_identity(socket_path)
    for entry in state.unresolved()[:max_entries]:
        attempt = entry.get("attempt")
        tab_id = entry.get("tab")
        label = entry.get("label")
        workspace = entry.get("workspace")
        if not (
            isinstance(attempt, str)
            and attempt
            and isinstance(tab_id, str)
            and tab_id
            and isinstance(label, str)
            and label
            and isinstance(workspace, str)
            and workspace
        ):
            outcome["left"] += 1
            continue
        journaled = entry.get("server")
        journaled_id = (
            (journaled.get("dev"), journaled.get("ino")) if isinstance(journaled, dict) else None
        )
        base = {"attempt": attempt, "tab": tab_id, "at_ms": now_ms}
        if (
            current is None
            or journaled_id is None
            or tuple(journaled_id) != current
            or not all(type(v) is int for v in journaled_id)
        ):
            state.journal_append({**base, "phase": "server_replaced"})
            outcome["replaced"] += 1
            continue
        try:
            live = seam.tab_get(tab_id, _CALL_S)
        except (SeamTimeout, SeamError):
            outcome["left"] += 1
            continue
        if live is None:
            state.journal_append({**base, "phase": "recovered", "note": "already_gone"})
            outcome["recovered"] += 1
            continue
        if live.get("label") != label or live.get("workspace_id") != workspace:
            outcome["left"] += 1
            continue
        try:
            seam.tab_close(tab_id, _CALL_S)
        except (SeamTimeout, SeamError):
            outcome["left"] += 1
            continue
        state.journal_append({**base, "phase": "closed", "note": "recovered"})
        outcome["closed"] += 1
    return outcome
