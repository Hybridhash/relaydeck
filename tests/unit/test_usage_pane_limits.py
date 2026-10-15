"""on-demand TUI reads through Herdr panes.

A fake herdr transport scripts tabs, panes, foreground processes and screens;
no real panes are touched. Tool fixtures use invented names and markers loaded
from inline recipe DATA (never product literals): the shipped catalog recipes
are validated generically in `test_catalog_*` below.
"""

from __future__ import annotations

import json
import re
import socket
import sqlite3
import sys
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from relaydeck.companion import config_edit
from relaydeck.companion.config import load_config, usage_from
from relaydeck.companion.detect import DetectReport, load_catalog
from relaydeck.companion.setup import LimitsCandidate, limits_candidates, offer_limits
from relaydeck.usage.contracts import (
    LimitError,
    LimitSnapshot,
    LimitStatus,
    LimitWindow,
    UsageQuery,
    UsageUnavailable,
    sanitize_plan_label,
    sanitize_unit,
)
from relaydeck.usage.limits_plane.contracts import (
    FetchContext,
    ProviderResult,
    ProviderSpec,
    TuiBundle,
    tui_recipe_from,
)
from relaydeck.usage.limits_plane.herdr_seam import SeamError
from relaydeck.usage.limits_plane.providers.tui_usage_pane import (
    FORMAT_ID,
    TuiUsagePaneProvider,
    _reset_to_ms,
    recover_stale_tabs,
)
from relaydeck.usage.limits_plane.refresh_state import (
    RefreshState,
    session_scope,
    socket_identity,
)
from relaydeck.usage.limits_plane.scheduler import COOLDOWN_MS, LimitsScheduler
from relaydeck.usage.store import SqliteUsageStore
from tests.support.companion.setup_sources import Answers
from tests.support.companion.setup_sources import row as support_row

NOW = int(time.time() * 1000)
KIND = "kind-a"
PROVIDER_KEY = f"h1/{KIND}/{FORMAT_ID}"


def _recipe(session_root: str | None = None, **over: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "launch_new": ["tool-a", "--fresh"],
        "launch_resume": ["tool-a", "--resume", "{id}"],
        "quit_text": "/quit",
        "quit_marker": "SESSION DONE",
        "dismiss": "esc",
        "prompt_marker": "TOOL PROMPT",
        "usage_marker": "ALLOWANCE",
        "hint_pattern": "resume with (?P<id>\\S+)",
        "id_pattern": "[a-z0-9-]{1,64}",
        "session_root": session_root,
        "not_found": ["no such session"],
        "binary": "tool-a",
        "plan_pattern": "PLAN (?P<plan>[A-Z ]+)",
        "windows": [
            {
                "key": "month",
                "pattern": "USED (?P<used>[0-9]+(?:\\.[0-9]+)?) OF (?P<limit>[0-9]+(?:\\.[0-9]+)?)",
                "unit": "credit",
                "reset_pattern": "RESET (?P<reset>[0-9]{4}-[0-9]{2}-[0-9]{2})",
                "reset_kind": "date",
            }
        ],
    }
    if session_root is None:
        del data["hint_pattern"]
        del data["id_pattern"]
        del data["session_root"]
    data.update(over)
    return data


def _fresh_recipe(**over: Any) -> dict[str, Any]:
    """A never-resume recipe (no id tracking), like the no-session-log shape."""
    data = _recipe(None)
    del data["launch_resume"]
    del data["quit_marker"]
    del data["dismiss"]
    data.update(over)
    return data


class FakeHerdr:
    """Scripted herdr CLI over in-memory tabs/panes. `run` matches the
    scheduler runner signature (argv, timeout, extra)."""

    def __init__(
        self,
        *,
        prompt: str = "TOOL PROMPT",
        usage: tuple[str, ...] = ("ALLOWANCE HEADER", "USED 3 OF 50"),
        quit_lines: tuple[str, ...] = ("SESSION DONE", "resume with sess-1"),
        tool: str = "tool-a",
    ) -> None:
        self.prompt = prompt
        self.usage = list(usage)
        self.quit_lines = list(quit_lines)
        self.tool = tool
        self.tabs: dict[str, dict[str, Any]] = {}
        self.panes: dict[str, dict[str, Any]] = {}
        self.inventory: list[dict[str, Any]] = []
        self.sent: list[tuple[str, str, str]] = []
        self.failed: set[str] = set()  # "tab close", "wait", ...
        self.slow_create = False
        self._counter = 0

    # -- transport ---------------------------------------------------------

    def run(self, argv: Any, timeout_s: float, extra: Any = None) -> bytes:
        args = [a for a in argv[1:]]
        group, verb = args[0], args[1]
        rest = args[2:]
        if group == "tab" and verb == "create":
            return self._create(rest)
        if group == "tab" and verb == "close":
            return self._close(rest[0])
        if group == "tab" and verb == "get":
            return self._get(rest[0])
        if group == "tab" and verb == "list":
            return self._tab_list(rest)
        if group == "pane" and verb == "run":
            return self._pane_run(rest[0], rest[1])
        if group == "pane" and verb == "send-text":
            return self._send_text(rest[0], rest[1])
        if group == "pane" and verb == "send-keys":
            return self._send_keys(rest[0], rest[1:])
        if group == "pane" and verb == "wait-output":
            return self._wait(rest)
        if group == "pane" and verb == "read":
            return self._read(rest[0])
        if group == "pane" and verb == "list":
            return self._pane_list()
        if group == "pane" and verb == "process-info":
            return self._process_info(rest[1])
        raise AssertionError(f"unexpected herdr call: {args}")

    # -- tab ops -----------------------------------------------------------

    def _create(self, args: list[str]) -> bytes:
        if self.slow_create:
            raise TimeoutError("slow create")
        workspace = args[args.index("--workspace") + 1]
        label = args[args.index("--label") + 1]
        assert args[-1] == "--no-focus"
        self._counter += 1
        tab_id, pane_id = f"t{self._counter}", f"p{self._counter}"
        self.tabs[tab_id] = {"tab_id": tab_id, "label": label, "workspace_id": workspace}
        self.panes[pane_id] = {"tab": tab_id, "fg": "shell", "typed": "", "screen": []}
        self.sent.append((pane_id, "create", label))
        return json.dumps(
            {
                "result": {
                    "tab": self.tabs[tab_id],
                    "root_pane": {"pane_id": pane_id},
                    "type": "tab_created",
                }
            }
        ).encode()

    def _close(self, tab_id: str) -> bytes:
        if "tab close" in self.failed:
            return b""
        tab = self.tabs.pop(tab_id, None)
        if tab is None:
            return b""
        for pane_id, pane in list(self.panes.items()):
            if pane["tab"] == tab_id:
                del self.panes[pane_id]
        return json.dumps({"result": {"type": "ok"}}).encode()

    def _get(self, tab_id: str) -> bytes:
        tab = self.tabs.get(tab_id)
        if tab is None:
            return b""
        return json.dumps({"result": {"tab": tab, "type": "tab_info"}}).encode()

    def _tab_list(self, args: list[str]) -> bytes:
        workspace = args[args.index("--workspace") + 1]
        tabs = [t for t in self.tabs.values() if t["workspace_id"] == workspace]
        return json.dumps({"result": {"tabs": tabs}}).encode()

    # -- pane ops ----------------------------------------------------------

    def _pane_run(self, pane_id: str, command: str) -> bytes:
        pane = self.panes[pane_id]
        self.sent.append((pane_id, "run", command))
        if self.on_launch is not None:
            self.on_launch(pane_id, command)
        else:
            pane["fg"] = "tool"
            pane["screen"] = [self.prompt]
        return b""

    on_launch: Any = None

    def _send_text(self, pane_id: str, text: str) -> bytes:
        self.panes[pane_id]["typed"] += text
        self.sent.append((pane_id, "text", text))
        return b""

    def _send_keys(self, pane_id: str, keys: list[str]) -> bytes:
        pane = self.panes[pane_id]
        self.sent.append((pane_id, "keys", "+".join(keys)))
        if pane["fg"] != "tool":
            pane["typed"] = ""
            return b""
        if "esc" in keys:
            pane["typed"] = ""
            pane["screen"] = [self.prompt]
            return b""
        typed = pane["typed"]
        pane["typed"] = ""
        if typed == "/usage":
            pane["screen"] = list(self.usage)
        elif typed == "/quit":
            pane["screen"] = list(self.quit_lines)
            pane["fg"] = "shell"
        return b""

    def _wait(self, args: list[str]) -> bytes:
        if "wait" in self.failed:
            return b""
        pane_id = args[0]
        regex = args[args.index("--regex") + 1]
        screen = "\n".join(self.panes[pane_id]["screen"])
        if re.search(regex, screen):
            return json.dumps({"result": {"type": "output_matched"}}).encode()
        return b""

    def _read(self, pane_id: str) -> bytes:
        return "\n".join(self.panes[pane_id]["screen"]).encode()

    def _pane_list(self) -> bytes:
        return json.dumps({"result": {"panes": self.inventory}}).encode()

    def _process_info(self, pane_id: str) -> bytes:
        pane = self.panes.get(pane_id)
        if pane is None:
            return b""
        if pane["fg"] == "tool":
            procs = [{"pid": 200, "name": self.tool, "argv0": f"/usr/bin/{self.tool}"}]
            group = 200
        else:
            procs = [{"pid": 100, "name": "zsh", "argv0": "/bin/zsh"}]
            group = 100
        return json.dumps(
            {
                "result": {
                    "process_info": {
                        "pane_id": pane_id,
                        "shell_pid": 100,
                        "foreground_process_group_id": group,
                        "foreground_processes": procs,
                    }
                }
            }
        ).encode()


def _socket(tmp_path: Path) -> str:
    import tempfile

    sock = socket.socket(socket.AF_UNIX)
    short = Path(tempfile.mkdtemp(prefix="hs")) / "h.sock"  # AF_UNIX paths are short
    sock.bind(str(short))
    sock.close()
    return str(short)  # the file remains; stat works


def _ctx(
    tmp_path: Path,
    fake: FakeHerdr,
    recipe_data: dict[str, Any] | None = None,
    *,
    attempt: str = "attempt-1",
    kind: str = KIND,
    timeout: float = 90.0,
    linked: str | None = None,
    state: RefreshState | None = None,
) -> FetchContext:
    recipe = tui_recipe_from(recipe_data if recipe_data is not None else _recipe())
    bundle = TuiBundle(
        recipe, ("/usr/bin/herdr",), tmp_path / "refresh.json", "UTC", _socket(tmp_path)
    )
    ctx = FetchContext(
        PROVIDER_KEY,
        kind,
        FORMAT_ID,
        NOW,
        timeout,
        fake.run,
        command=(),
        attempt_id=attempt,
        tui=bundle,
        linked_workspace=linked,
        refresh=state or RefreshState(tmp_path / "refresh.json"),
    )
    return ctx


def _live_pane(workspace: str, kind: str, cwd: str) -> dict[str, Any]:
    return {"agent": kind, "workspace_id": workspace, "cwd": cwd, "agent_status": "idle"}


# -- provider happy paths --------------------------------------------------------


def test_tracked_tool_full_run_parses_amounts_and_saves_hint(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    root.mkdir()
    (root / "sess-1.json").write_text("{}")  # born during the run
    fake = FakeHerdr(usage=("ALLOWANCE", "PLAN MONTHLY MAX", "USED 3 OF 50", "RESET 2026-11-01"))
    fake.inventory = [_live_pane("w9", KIND, str(tmp_path))]
    ctx = _ctx(tmp_path, fake, _recipe(str(root)))
    result = TuiUsagePaneProvider().fetch(ctx)
    assert result.error is None and result.note == ""
    (snapshot,) = result.snapshots
    assert snapshot.source == FORMAT_ID and snapshot.account_ref is None
    assert snapshot.plan_label == "MONTHLY MAX"
    (window,) = snapshot.windows
    assert (window.used_amount, window.limit_amount, window.unit) == (3.0, 50.0, "credit")
    assert window.used_percent == pytest.approx(6.0)
    assert window.resets_at_ms == 1793491200000  # 2026-11-01T00:00Z
    assert window.over_allowance is False
    assert fake.tabs == {}  # closed
    assert ctx.refresh is not None
    saved = ctx.refresh.saved_session(session_scope(KIND, str(root), "w9"))
    assert saved is not None and saved.session_id == "sess-1"


def test_saved_id_is_resumed_and_confirmed(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    root.mkdir()
    (root / "sess-9.json").write_text("{}")
    fake = FakeHerdr(quit_lines=("SESSION DONE", "resume with sess-9"))
    fake.inventory = [_live_pane("w9", KIND, str(tmp_path))]
    state = RefreshState(tmp_path / "refresh.json")
    scope = session_scope(KIND, str(root), "w9")
    state.save_session(scope, "sess-9", "hint")
    ctx = _ctx(tmp_path, fake, _recipe(str(root)), state=state)
    result = TuiUsagePaneProvider().fetch(ctx)
    assert result.error is None
    runs = [payload for _, kind, payload in fake.sent if kind == "run"]
    assert runs == ["tool-a --resume sess-9"]
    saved = state.saved_session(scope)
    assert saved is not None and saved.session_id == "sess-9"


def test_fresh_recipe_never_resumes_and_tracks_nothing(tmp_path: Path) -> None:
    fake = FakeHerdr(usage=("ALLOWANCE HEADER", "USED 1 OF 10"))
    fake.inventory = [_live_pane("w9", KIND, str(tmp_path))]
    state = RefreshState(tmp_path / "refresh.json")
    state.save_session(session_scope(KIND, "", "w9"), "sess-old", "hint")
    ctx = _ctx(tmp_path, fake, _fresh_recipe(), state=state)
    result = TuiUsagePaneProvider().fetch(ctx)
    assert result.error is None
    runs = [payload for _, kind, payload in fake.sent if kind == "run"]
    assert runs == ["tool-a --fresh"]
    kept = state.saved_session(session_scope(KIND, "", "w9"))
    assert kept is not None and kept.session_id == "sess-old"  # untouched


# -- resume failure branches -------------------------------------------------------


def test_definite_dead_id_retries_fresh_in_a_new_tab(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    root.mkdir()
    (root / "sess-new.json").write_text("{}")
    launched: list[str] = []
    fake = FakeHerdr(quit_lines=("SESSION DONE", "resume with sess-new"))
    real_run = fake._pane_run

    def pane_run(pane_id: str, command: str) -> bytes:
        launched.append(command)
        # The resumed id is gone: the tool prints its not-found text and exits.
        if "--resume sess-gone" in command:
            fake.panes[pane_id]["screen"] = ["no such session: sess-gone"]
            return b""
        return real_run(pane_id, command)

    fake._pane_run = pane_run  # type: ignore[method-assign]
    fake.inventory = [_live_pane("w9", KIND, str(tmp_path))]
    state = RefreshState(tmp_path / "refresh.json")
    scope = session_scope(KIND, str(root), "w9")
    state.save_session(scope, "sess-gone", "hint")
    ctx = _ctx(tmp_path, fake, _recipe(str(root)), state=state)
    result = TuiUsagePaneProvider().fetch(ctx)
    assert result.error is None  # recovered on a fresh tab
    assert launched == ["tool-a --resume sess-gone", "tool-a --fresh"]
    assert fake.tabs == {}
    saved = state.saved_session(scope)
    assert saved is not None and saved.session_id == "sess-new"


def test_missing_session_file_is_definite_without_markers(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    root.mkdir()
    (root / "sess-new.json").write_text("{}")
    fake = FakeHerdr(quit_lines=("SESSION DONE", "resume with sess-new"))
    real_run = fake._pane_run

    def pane_run(pane_id: str, command: str) -> bytes:
        if "--resume sess-gone" in command:
            fake.panes[pane_id]["screen"] = ["something odd, no prompt"]
            return b""
        return real_run(pane_id, command)

    fake._pane_run = pane_run  # type: ignore[method-assign]
    fake.inventory = [_live_pane("w9", KIND, str(tmp_path))]
    state = RefreshState(tmp_path / "refresh.json")
    scope = session_scope(KIND, str(root), "w9")
    state.save_session(scope, "sess-gone", "hint")  # no sess-gone.json on disk
    ctx = _ctx(tmp_path, fake, _recipe(str(root)), state=state)
    assert TuiUsagePaneProvider().fetch(ctx).error is None
    saved = state.saved_session(scope)
    assert saved is not None and saved.session_id == "sess-new"


def test_resume_timeout_with_file_present_aborts_and_keeps_id(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    root.mkdir()
    (root / "sess-9.json").write_text("{}")
    fake = FakeHerdr()
    real_run = fake._pane_run

    def pane_run(pane_id: str, command: str) -> bytes:
        if "--resume sess-9" in command:
            fake.panes[pane_id]["fg"] = "tool"  # hung: tool up, no prompt
            fake.panes[pane_id]["screen"] = ["spinning..."]
            return b""
        return real_run(pane_id, command)

    fake._pane_run = pane_run  # type: ignore[method-assign]
    fake.inventory = [_live_pane("w9", KIND, str(tmp_path))]
    state = RefreshState(tmp_path / "refresh.json")
    scope = session_scope(KIND, str(root), "w9")
    state.save_session(scope, "sess-9", "hint")
    ctx = _ctx(tmp_path, fake, _recipe(str(root)), state=state)
    result = TuiUsagePaneProvider().fetch(ctx)
    assert result.error == LimitError.TIMEOUT
    saved = state.saved_session(scope)
    assert saved is not None and saved.session_id == "sess-9"  # kept
    assert fake.tabs == {}


def test_hint_mismatch_clears_and_keeps_snapshot(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    root.mkdir()
    (root / "sess-9.json").write_text("{}")
    (root / "sess-x.json").write_text("{}")
    fake = FakeHerdr(quit_lines=("SESSION DONE", "resume with sess-x"))
    fake.inventory = [_live_pane("w9", KIND, str(tmp_path))]
    state = RefreshState(tmp_path / "refresh.json")
    scope = session_scope(KIND, str(root), "w9")
    state.save_session(scope, "sess-9", "hint")
    ctx = _ctx(tmp_path, fake, _recipe(str(root)), state=state)
    result = TuiUsagePaneProvider().fetch(ctx)
    assert result.error is None and len(result.snapshots) == 1  # account data stands
    assert state.saved_session(scope) is None  # the stranger is never adopted


def test_missing_hint_after_resume_clears(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    root.mkdir()
    (root / "sess-9.json").write_text("{}")
    fake = FakeHerdr(quit_lines=("SESSION DONE",))  # no hint line
    fake.inventory = [_live_pane("w9", KIND, str(tmp_path))]
    state = RefreshState(tmp_path / "refresh.json")
    scope = session_scope(KIND, str(root), "w9")
    state.save_session(scope, "sess-9", "hint")
    ctx = _ctx(tmp_path, fake, _recipe(str(root)), state=state)
    assert TuiUsagePaneProvider().fetch(ctx).error is None
    assert state.saved_session(scope) is None


def test_hint_for_missing_file_is_not_saved(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    root.mkdir()
    fake = FakeHerdr()  # quit hint names sess-1, but no sess-1.json exists
    fake.inventory = [_live_pane("w9", KIND, str(tmp_path))]
    ctx = _ctx(tmp_path, fake, _recipe(str(root)))
    assert TuiUsagePaneProvider().fetch(ctx).error is None
    assert ctx.refresh is not None
    assert ctx.refresh.saved_session(session_scope(KIND, str(root), "w9")) is None


# -- aborts ------------------------------------------------------------------------


def test_prompt_never_arrives_aborts_and_closes(tmp_path: Path) -> None:
    fake = FakeHerdr(prompt="SOMETHING ELSE")  # never the prompt marker
    fake.inventory = [_live_pane("w9", KIND, str(tmp_path))]
    ctx = _ctx(tmp_path, fake)
    result = TuiUsagePaneProvider().fetch(ctx)
    assert result.error == LimitError.TIMEOUT
    assert fake.tabs == {}
    assert all(payload != "/quit" for _, _, payload in fake.sent)


def test_fresh_pane_not_shell_is_unavailable(tmp_path: Path) -> None:
    """A brand-new pane owned by a stranger was never usable: definite, fast."""
    fake = FakeHerdr()
    fake.inventory = [_live_pane("w9", KIND, str(tmp_path))]
    real_info = fake._process_info

    def info(pane_id: str) -> bytes:
        raw = real_info(pane_id)
        if not raw:
            return b""
        doc = json.loads(raw)
        # A stranger owns even the fresh pane.
        for proc in doc["result"]["process_info"]["foreground_processes"]:
            proc["argv0"], proc["name"] = "/bin/evil", "evil"
        return json.dumps(doc).encode()

    fake._process_info = info  # type: ignore[method-assign]
    ctx = _ctx(tmp_path, fake)
    result = TuiUsagePaneProvider().fetch(ctx)
    assert result.error == LimitError.UNAVAILABLE
    assert fake.tabs == {}
    assert all(payload != "/quit" for _, _, payload in fake.sent)


def test_foreground_takeover_mid_run_sends_no_quit_text(tmp_path: Path) -> None:
    """The tool was verified, then the foreground changed: uncertain, no write."""
    fake = FakeHerdr()
    fake.inventory = [_live_pane("w9", KIND, str(tmp_path))]
    real_info = fake._process_info

    def info(pane_id: str) -> bytes:
        raw = real_info(pane_id)
        if not raw:
            return b""
        if fake.panes[pane_id]["fg"] == "shell":
            return raw  # pre-launch: the fresh pane is honestly a shell
        doc = json.loads(raw)
        # An impostor takes the foreground after launch.
        for proc in doc["result"]["process_info"]["foreground_processes"]:
            proc["argv0"], proc["name"] = "/bin/evil", "evil"
        return json.dumps(doc).encode()

    fake._process_info = info  # type: ignore[method-assign]
    ctx = _ctx(tmp_path, fake)
    result = TuiUsagePaneProvider().fetch(ctx)
    assert result.error == LimitError.TIMEOUT
    assert fake.tabs == {}
    assert all(payload != "/quit" for _, _, payload in fake.sent)


def test_parse_miss_quits_cleanly_and_reports_parse(tmp_path: Path) -> None:
    fake = FakeHerdr(usage=("ALLOWANCE", "nothing shaped like numbers here"))
    fake.inventory = [_live_pane("w9", KIND, str(tmp_path))]
    ctx = _ctx(tmp_path, fake)
    result = TuiUsagePaneProvider().fetch(ctx)
    assert result.error == LimitError.PARSE
    assert fake.tabs == {}
    assert any(payload == "/quit" for _, kind, payload in fake.sent if kind == "text")


def test_a_tool_without_limits_to_show_is_not_reported_rather_than_a_parse_miss(
    tmp_path: Path,
) -> None:
    fake = FakeHerdr(usage=("ALLOWANCE", "Currently unavailable"))
    fake.inventory = [_live_pane("w9", KIND, str(tmp_path))]
    recipe = _recipe(not_reported=["ALLOWANCE[^\\n]*\\n[^\\n]*Currently unavailable"])
    ctx = _ctx(tmp_path, fake, recipe)
    result = TuiUsagePaneProvider().fetch(ctx)
    assert result.error == LimitError.UNAVAILABLE
    assert result.note == "not_reported"
    assert fake.tabs == {}


def test_fast_launch_failure_is_unavailable(tmp_path: Path) -> None:
    fake = FakeHerdr()
    fake.inventory = [_live_pane("w9", KIND, str(tmp_path))]

    def pane_run(pane_id: str, command: str) -> bytes:
        fake.panes[pane_id]["screen"] = ["command not found: tool-a"]
        return b""  # shell stays in the foreground

    fake._pane_run = pane_run  # type: ignore[method-assign]
    ctx = _ctx(tmp_path, fake, _fresh_recipe())
    result = TuiUsagePaneProvider().fetch(ctx)
    assert result.error == LimitError.UNAVAILABLE
    assert fake.tabs == {}


def test_provider_journals_the_attempt_id_on_every_phase(tmp_path: Path) -> None:
    """the journal ties every tab phase to the attempt that made it."""
    fake = FakeHerdr()
    fake.inventory = [_live_pane("w9", KIND, str(tmp_path))]
    ctx = _ctx(tmp_path, fake, _fresh_recipe(), attempt="attempt-9")
    assert TuiUsagePaneProvider().fetch(ctx).error is None
    assert ctx.refresh is not None
    entries = ctx.refresh.journal_entries()
    assert entries and all(entry["attempt"] == "attempt-9" for entry in entries)
    assert {entry["phase"] for entry in entries} >= {"intent", "created", "closed"}


# -- recipe validation -----------------------------------------------------------


def _window(**over: Any) -> list[dict[str, Any]]:
    win = dict(_recipe()["windows"][0])
    win.update(over)
    return [win]


def _rejected(data: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        tui_recipe_from(data)


def test_recipe_rejects_unrunnable_launches() -> None:
    _rejected(_recipe(launch_new=[]))
    _rejected(_recipe(launch_new=["tool-a", "bad\x01arg"]))
    _rejected(_recipe(launch_new=["tool-a", "{id}"]))  # the id goes nowhere fresh
    _rejected(_recipe(launch_resume=["tool-a", "--resume"]))  # ...nor without a slot
    _rejected(_recipe(binary="tool/a"))
    _rejected(_recipe(binary=""))
    _rejected(_recipe(dismiss="ESC"))
    _rejected(_recipe(quit_text=""))
    _rejected(_recipe(prompt_marker="("))
    _rejected(_recipe(max_lines=9))
    _rejected(_recipe(max_lines=201))
    _rejected(_recipe(not_found="nope"))
    _rejected(_recipe(windows=[]))


def test_recipe_rejects_broken_session_tracking() -> None:
    root = "/tmp/sessions"
    _rejected(_recipe(root, id_pattern=None))  # hint without an id shape
    _rejected(_recipe(root, hint_pattern=None))  # an id shape without a hint
    data = _recipe(root)
    data["session_root"] = None  # a hint without a root
    _rejected(data)
    _rejected(_recipe("/a/../b"))
    _rejected(_recipe("relative/path"))
    _rejected(_recipe(root, hint_pattern="resume with no group"))


def test_recipe_rejects_broken_windows() -> None:
    _rejected(_recipe(windows=_window(pattern="USED (?P<used>[0-9]+)")))
    _rejected(_recipe(windows=_window(pattern="(?P<percent>[0-9]+)% of (?P<used>[0-9]+)")))
    _rejected(_recipe(windows=_window(pattern="nothing shaped", key="bad key")))
    _rejected(_recipe(windows=_window(reset_pattern=None)))  # a kind without a pattern
    _rejected(_recipe(windows=_window(reset_kind="hourly")))
    _rejected(_recipe(windows=_window(unit="Credit")))
    _rejected(_recipe(windows=_window(minutes=0)))


def test_recipe_accepts_percent_and_amount_windows() -> None:
    percent = tui_recipe_from(
        _recipe(
            windows=_window(pattern="(?P<percent>[0-9]+)%", reset_pattern=None, reset_kind=None)
        )
    )
    assert percent.windows[0].unit == "credit"
    assert percent.windows[0].reset_kind is None
    amounts = tui_recipe_from(_recipe(windows=_window(minutes=60)))
    assert amounts.windows[0].minutes == 60


def test_sanitize_unit_accepts_snake_and_rejects_the_rest() -> None:
    assert sanitize_unit("credit") == "credit"
    assert sanitize_unit("gpu_hour") == "gpu_hour"
    assert sanitize_unit("Credit") is None
    assert sanitize_unit("") is None
    assert sanitize_unit("a" * 17) is None
    assert sanitize_unit(None) is None
    assert sanitize_unit(7) is None


def test_reset_kinds_parse_to_host_zone_instants() -> None:
    assert _reset_to_ms("2026-11-01", "date", NOW, "UTC") == 1793491200000
    assert _reset_to_ms("2026-13-01", "date", NOW, "UTC") is None
    noon = int(datetime(2026, 10, 11, 12, 0, tzinfo=UTC).timestamp() * 1000)
    assert _reset_to_ms("3:00 PM", "clock", noon, "UTC") == noon + 3 * 3_600_000
    assert _reset_to_ms("11:00 AM", "clock", noon, "UTC") == noon - 3_600_000
    assert _reset_to_ms("13:00 PM", "clock", noon, "UTC") is None
    oct19 = int(datetime(2026, 10, 19, 3, 0, tzinfo=UTC).timestamp() * 1000)
    assert _reset_to_ms("Oct 19 at 3:00 AM", "monthday", noon, "UTC") == oct19
    assert _reset_to_ms("Bog 19 at 3:00 AM", "monthday", noon, "UTC") is None
    assert _reset_to_ms("whatever", "hourly", noon, "UTC") is None
    assert _reset_to_ms("2026-11-01", "date", noon, "No/Such-Zone") is None


def test_monthday_rolls_back_a_far_future_year_but_keeps_the_past() -> None:
    jan5 = int(datetime(2027, 1, 5, 12, 0, tzinfo=UTC).timestamp() * 1000)
    dec28 = int(datetime(2026, 12, 28, 3, 0, tzinfo=UTC).timestamp() * 1000)
    assert _reset_to_ms("Dec 28 at 3:00 AM", "monthday", jan5, "UTC") == dec28
    oct11 = int(datetime(2026, 10, 11, 12, 0, tzinfo=UTC).timestamp() * 1000)
    sep1 = int(datetime(2026, 9, 1, 3, 0, tzinfo=UTC).timestamp() * 1000)
    assert _reset_to_ms("Sep 1 at 3:00 AM", "monthday", oct11, "UTC") == sep1


def test_sanitize_plan_label_normalises_and_truncates() -> None:
    assert sanitize_plan_label("Monthly Max") == "MONTHLY MAX"
    assert sanitize_plan_label("  pro-1_x.y  ") == "PRO-1_X.Y"
    assert sanitize_plan_label("x" * 40) == "X" * 32
    assert sanitize_plan_label("!!!") is None
    assert sanitize_plan_label("") is None
    assert sanitize_plan_label("y" * 257) is None


# -- store amounts and plan labels --------------------------------------------


def _limit_snapshot(window: LimitWindow, *, plan_label: str | None = None) -> LimitSnapshot:
    return LimitSnapshot(
        KIND, "quota-a", NOW, (window,), LimitStatus.FRESH, FORMAT_ID, None, None, plan_label
    )


def _utc_day(when_ms: int) -> str:
    return datetime.fromtimestamp(when_ms / 1000, tz=UTC).date().isoformat()


def _good_window() -> LimitWindow:
    return LimitWindow("m", 6.0, None, None, LimitStatus.FRESH, 3.0, 50.0, "credit", False)


def test_amounts_and_plan_label_round_trip_through_the_store(tmp_path: Path) -> None:
    store = SqliteUsageStore(tmp_path / "usage.db", "UTC")
    try:
        store.apply_limits(
            KIND, PROVIDER_KEY, (_limit_snapshot(_good_window(), plan_label="P"),), NOW
        )
        day = _utc_day(NOW)
        (snapshot,) = store.query(UsageQuery("h1", day, day), NOW).limits
        assert snapshot.plan_label == "P"
        (back,) = snapshot.windows
        assert (back.used_amount, back.limit_amount, back.unit) == (3.0, 50.0, "credit")
        assert back.used_percent == pytest.approx(6.0)
        assert back.over_allowance is False
    finally:
        store.close()


def test_bad_amounts_and_labels_are_rejected_at_the_store(tmp_path: Path) -> None:
    store = SqliteUsageStore(tmp_path / "usage.db", "UTC")
    try:
        good = _good_window()
        for window in (
            replace(good, used_amount=-1.0),
            replace(good, limit_amount=float("nan")),
            replace(good, unit="Credit"),
            replace(good, over_allowance=1),  # type: ignore[arg-type]
        ):
            with pytest.raises(UsageUnavailable):
                store.apply_limits(KIND, PROVIDER_KEY, (_limit_snapshot(window),), NOW)
        for label in ("x" * 33, "lower case", "", "has\ttab"):
            with pytest.raises(UsageUnavailable):
                store.apply_limits(
                    KIND, PROVIDER_KEY, (_limit_snapshot(good, plan_label=label),), NOW
                )
    finally:
        store.close()


def test_plan_label_column_is_added_to_an_older_database(tmp_path: Path) -> None:
    path = tmp_path / "usage.db"
    SqliteUsageStore(path, "UTC").close()
    with sqlite3.connect(path) as db:
        db.execute("ALTER TABLE limits DROP COLUMN plan_label")
        assert "plan_label" not in {row[1] for row in db.execute("PRAGMA table_info(limits)")}
    store = SqliteUsageStore(path, "UTC")
    try:
        with sqlite3.connect(path) as db:
            assert "plan_label" in {row[1] for row in db.execute("PRAGMA table_info(limits)")}
        store.apply_limits(
            KIND, PROVIDER_KEY, (_limit_snapshot(_good_window(), plan_label="P"),), NOW
        )
    finally:
        store.close()


# -- scheduler on-demand -----------------------------------------------------------


class _ScriptedLimits:
    """Replays results in order; records every context (attempt ids included)."""

    format_id = FORMAT_ID

    def __init__(self, results: list[ProviderResult]) -> None:
        self.results = results
        self.calls: list[FetchContext] = []

    def fetch(self, ctx: FetchContext) -> ProviderResult:
        self.calls.append(ctx)
        return self.results[min(len(self.calls) - 1, len(self.results) - 1)]


class _Clock:
    def __init__(self, now: int) -> None:
        self.now = now

    def __call__(self) -> int:
        return self.now


def _od_scheduler(
    tmp_path: Path,
    results: list[ProviderResult],
    *,
    herdr: tuple[str, ...] = ("/usr/bin/herdr",),
    on_demand: bool = True,
    with_tui: bool = True,
) -> tuple[LimitsScheduler, _ScriptedLimits, SqliteUsageStore, RefreshState, _Clock]:
    store = SqliteUsageStore(tmp_path / "usage.db", "UTC")
    state = RefreshState(tmp_path / "refresh.json")
    bundle = (
        TuiBundle(
            tui_recipe_from(_fresh_recipe()),
            herdr,
            tmp_path / "refresh.json",
            "UTC",
            str(tmp_path / "herdr.sock"),
        )
        if with_tui
        else None
    )
    spec = ProviderSpec(
        PROVIDER_KEY, KIND, FORMAT_ID, 300_000, 90.0, on_demand=on_demand, tui=bundle
    )
    provider = _ScriptedLimits(results)
    clock = _Clock(NOW)
    scheduler = LimitsScheduler(
        store,
        {PROVIDER_KEY: spec},
        {PROVIDER_KEY: provider},
        clock=clock,
        jitter=lambda: 0.5,
        refresh=state,
    )
    return scheduler, provider, store, state, clock


def _ok_result() -> ProviderResult:
    return ProviderResult((_limit_snapshot(_good_window(), plan_label="P"),))


def test_on_demand_start_join_run_cooldown(tmp_path: Path) -> None:
    scheduler, provider, store, state, clock = _od_scheduler(tmp_path, [_ok_result()])
    try:
        first = scheduler.request_on_demand(PROVIDER_KEY)
        assert first.state == "started" and first.attempt_id
        second = scheduler.request_on_demand(PROVIDER_KEY)
        assert (second.state, second.attempt_id) == ("in_progress", first.attempt_id)
        assert scheduler.refresh_status()[KIND]["in_progress"] is True
        assert scheduler.run_once(clock()) == 1
        (ctx,) = provider.calls
        assert ctx.attempt_id == first.attempt_id  # the id follows the attempt
        assert state.cooldown_until(KIND) == NOW + COOLDOWN_MS
        last = state.last_attempt(KIND)
        assert last is not None
        assert (last.attempt_id, last.finished_ms, last.result) == (first.attempt_id, NOW, "ok")
        third = scheduler.request_on_demand(PROVIDER_KEY)
        assert third.state == "cooldown" and third.retry_after_s == COOLDOWN_MS // 1000
        status = scheduler.refresh_status()[KIND]
        assert status["available"] is True and status["in_progress"] is False
        assert status["cooldown_until_ms"] == NOW + COOLDOWN_MS
        assert status["last_attempt"] == {
            "id": first.attempt_id,
            "finished_ms": NOW,
            "result": "ok",
        }
        assert status["last_error"] is None
    finally:
        store.close()


def test_on_demand_failure_clears_the_attempt_without_a_cooldown(tmp_path: Path) -> None:
    scheduler, _, store, state, clock = _od_scheduler(
        tmp_path, [ProviderResult((), LimitError.TIMEOUT)]
    )
    try:
        first = scheduler.request_on_demand(PROVIDER_KEY)
        assert scheduler.run_once(clock()) == 1
        assert state.cooldown_until(KIND) == 0
        last = state.last_attempt(KIND)
        assert last is not None and (last.attempt_id, last.result) == (first.attempt_id, "timeout")
        status = scheduler.refresh_status()[KIND]
        assert status["last_error"] == "timeout" and status["in_progress"] is False
        again = scheduler.request_on_demand(PROVIDER_KEY)
        assert again.state == "started" and again.attempt_id != first.attempt_id
    finally:
        store.close()


def test_visibility_refresh_never_triggers_on_demand(tmp_path: Path) -> None:
    scheduler, provider, store, _, clock = _od_scheduler(tmp_path, [_ok_result()])
    try:
        scheduler.request_refresh()
        assert scheduler.run_once(clock()) == 0
        assert provider.calls == []
        assert scheduler.run_once(clock() + 3_600_000) == 0  # never time-due either
    finally:
        store.close()


def test_request_on_demand_rejects_unknown_and_timer_specs(tmp_path: Path) -> None:
    scheduler, _, store, _, _ = _od_scheduler(tmp_path, [_ok_result()])
    try:
        assert scheduler.request_on_demand("h1/kind-a/nope-v1").state == "not_configured"
    finally:
        store.close()
    timer, _, store, _, _ = _od_scheduler(tmp_path, [_ok_result()], on_demand=False)
    try:
        assert timer.request_on_demand(PROVIDER_KEY).state == "not_configured"
    finally:
        store.close()
    bare, _, store, _, _ = _od_scheduler(tmp_path, [_ok_result()], with_tui=False)
    try:
        assert bare.request_on_demand(PROVIDER_KEY).state == "not_configured"
    finally:
        store.close()


def test_refresh_status_reports_availability_reasons(tmp_path: Path) -> None:
    scheduler, _, store, _, _ = _od_scheduler(tmp_path, [_ok_result()])
    try:
        status = scheduler.refresh_status()[KIND]
        assert (status["available"], status["reason"]) == (True, None)
        assert status["cooldown_until_ms"] is None and status["last_attempt"] is None
    finally:
        store.close()
    bare, _, store, _, _ = _od_scheduler(tmp_path, [_ok_result()], with_tui=False)
    try:
        status = bare.refresh_status()[KIND]
        assert (status["available"], status["reason"]) == (False, "not_configured")
    finally:
        store.close()
    no_herdr, _, store, _, _ = _od_scheduler(tmp_path, [_ok_result()], herdr=())
    try:
        status = no_herdr.refresh_status()[KIND]
        assert (status["available"], status["reason"]) == (False, "no_herdr")
    finally:
        store.close()


def test_scheduler_runs_the_real_provider_and_stores_the_reading(tmp_path: Path) -> None:
    """Request, fetch through the fake Herdr, commit, cooldown, session saved."""
    root = tmp_path / "sessions"
    root.mkdir()
    (root / "sess-1.json").write_text("{}")
    fake = FakeHerdr(usage=("ALLOWANCE", "USED 3 OF 50"))
    fake.inventory = [_live_pane("w9", KIND, str(tmp_path))]
    store = SqliteUsageStore(tmp_path / "usage.db", "UTC")
    try:
        state = RefreshState(tmp_path / "refresh.json")
        bundle = TuiBundle(
            tui_recipe_from(_recipe(str(root))),
            ("/usr/bin/herdr",),
            tmp_path / "refresh.json",
            "UTC",
            _socket(tmp_path),
        )
        spec = ProviderSpec(
            PROVIDER_KEY, KIND, FORMAT_ID, 300_000, 90.0, on_demand=True, tui=bundle
        )
        clock = _Clock(NOW)
        scheduler = LimitsScheduler(
            store,
            {PROVIDER_KEY: spec},
            {PROVIDER_KEY: TuiUsagePaneProvider()},
            clock=clock,
            jitter=lambda: 0.5,
            refresh=state,
            run=fake.run,
        )
        decision = scheduler.request_on_demand(PROVIDER_KEY)
        assert decision.state == "started"
        assert scheduler.run_once(clock()) == 1
        day = _utc_day(NOW)
        (snapshot,) = store.query(UsageQuery("h1", day, day), NOW).limits
        assert snapshot.windows[0].used_amount == 3.0
        assert state.cooldown_until(KIND) == NOW + COOLDOWN_MS
        saved = state.saved_session(session_scope(KIND, str(root), "w9"))
        assert saved is not None and saved.session_id == "sess-1"
    finally:
        store.close()


# -- restart recovery ----------------------------------------------------------------


def _journal_intent(
    state: RefreshState,
    sock: str,
    *,
    tab: str = "t1",
    label: str = "kind-a-relaydeck",
    workspace: str = "w9",
    server: dict[str, int] | None = None,
) -> None:
    if server is None:
        identity = socket_identity(sock)
        assert identity is not None
        server = {"dev": identity[0], "ino": identity[1]}
    state.journal_append(
        {
            "attempt": "a1",
            "tab": tab,
            "phase": "intent",
            "at_ms": NOW,
            "label": label,
            "workspace": workspace,
            "server": server,
        }
    )


def test_recovery_closes_a_proven_ours_leftover(tmp_path: Path) -> None:
    state = RefreshState(tmp_path / "refresh.json")
    sock = _socket(tmp_path)
    _journal_intent(state, sock)
    fake = FakeHerdr()
    fake.tabs["t1"] = {"tab_id": "t1", "label": "kind-a-relaydeck", "workspace_id": "w9"}
    outcome = recover_stale_tabs(state, ("/usr/bin/herdr",), sock, fake.run, now_ms=NOW)
    assert outcome == {"closed": 1, "recovered": 0, "replaced": 0, "left": 0}
    assert fake.tabs == {}
    assert state.latest_phase("a1", "t1") == "closed"


def test_recovery_marks_an_already_gone_tab(tmp_path: Path) -> None:
    state = RefreshState(tmp_path / "refresh.json")
    sock = _socket(tmp_path)
    _journal_intent(state, sock)
    fake = FakeHerdr()
    outcome = recover_stale_tabs(state, ("/usr/bin/herdr",), sock, fake.run, now_ms=NOW)
    assert outcome == {"closed": 0, "recovered": 1, "replaced": 0, "left": 0}
    assert state.latest_phase("a1", "t1") == "recovered"


def test_recovery_leaves_a_tab_it_cannot_prove(tmp_path: Path) -> None:
    state = RefreshState(tmp_path / "refresh.json")
    sock = _socket(tmp_path)
    _journal_intent(state, sock)
    fake = FakeHerdr()
    fake.tabs["t1"] = {"tab_id": "t1", "label": "something-else", "workspace_id": "w9"}
    outcome = recover_stale_tabs(state, ("/usr/bin/herdr",), sock, fake.run, now_ms=NOW)
    assert outcome == {"closed": 0, "recovered": 0, "replaced": 0, "left": 1}
    assert "t1" in fake.tabs  # untouched
    assert state.latest_phase("a1", "t1") == "intent"


def test_recovery_leaves_tabs_from_a_replaced_server(tmp_path: Path) -> None:
    state = RefreshState(tmp_path / "refresh.json")
    sock = _socket(tmp_path)
    _journal_intent(state, sock, server={"dev": 0, "ino": 0})
    fake = FakeHerdr()
    fake.tabs["t1"] = {"tab_id": "t1", "label": "kind-a-relaydeck", "workspace_id": "w9"}
    outcome = recover_stale_tabs(state, ("/usr/bin/herdr",), sock, fake.run, now_ms=NOW)
    assert outcome == {"closed": 0, "recovered": 0, "replaced": 1, "left": 0}
    assert "t1" in fake.tabs  # untouched: another server may own it
    assert state.latest_phase("a1", "t1") == "server_replaced"


def test_recovery_without_herdr_or_entries_is_a_noop(tmp_path: Path) -> None:
    state = RefreshState(tmp_path / "refresh.json")
    sock = _socket(tmp_path)
    fake = FakeHerdr()
    fake.tabs["t1"] = {"tab_id": "t1", "label": "kind-a-relaydeck", "workspace_id": "w9"}
    assert recover_stale_tabs(state, (), sock, None) == {
        "closed": 0,
        "recovered": 0,
        "replaced": 0,
        "left": 0,
    }
    _journal_intent(state, sock)
    assert recover_stale_tabs(state, (), sock, None) == {
        "closed": 0,
        "recovered": 0,
        "replaced": 0,
        "left": 0,
    }
    assert "t1" in fake.tabs


def test_recovery_bounds_its_work_and_survives_seam_failures(tmp_path: Path) -> None:
    state = RefreshState(tmp_path / "refresh.json")
    sock = _socket(tmp_path)
    _journal_intent(state, sock, tab="t1")
    _journal_intent(state, sock, tab="t2")
    fake = FakeHerdr()
    outcome = recover_stale_tabs(
        state, ("/usr/bin/herdr",), sock, fake.run, now_ms=NOW, max_entries=1
    )
    assert sum(outcome.values()) == 1

    class _FailingSeam:
        def tab_get(self, tab_id: str, timeout_s: float) -> None:
            raise SeamError("boom")

        def tab_close(self, tab_id: str, timeout_s: float) -> None:
            raise AssertionError("must not close what it cannot see")

    state2 = RefreshState(tmp_path / "refresh2.json")
    _journal_intent(state2, sock)
    outcome = recover_stale_tabs(state2, (), sock, None, seam=_FailingSeam())  # type: ignore[arg-type]
    assert outcome == {"closed": 0, "recovered": 0, "replaced": 0, "left": 1}


# -- refresh state ---------------------------------------------------------------------


def test_state_round_trips_through_a_reload(tmp_path: Path) -> None:
    path = tmp_path / "refresh.json"
    state = RefreshState(path)
    scope = session_scope(KIND, "/r", "w9")
    state.save_session(scope, "sess-1", "hint")
    state.set_cooldown(KIND, NOW + 1)
    state.record_attempt(KIND, "a1", NOW, "ok")
    reloaded = RefreshState(path)
    saved = reloaded.saved_session(scope)
    assert saved is not None and saved.session_id == "sess-1"
    assert reloaded.cooldown_until(KIND) == NOW + 1
    last = reloaded.last_attempt(KIND)
    assert last is not None and (last.attempt_id, last.finished_ms, last.result) == (
        "a1",
        NOW,
        "ok",
    )
    state.clear_session(scope)
    assert RefreshState(path).saved_session(scope) is None


def test_malformed_journal_entries_are_dropped(tmp_path: Path) -> None:
    state = RefreshState(tmp_path / "refresh.json")
    state.journal_append({})
    state.journal_append({"attempt": "a", "phase": "nope", "at_ms": NOW})
    assert state.journal_entries() == []
    state.journal_append({"attempt": "a", "tab": "t", "phase": "intent", "at_ms": NOW})
    assert len(state.journal_entries()) == 1


def test_session_scope_is_a_json_triple() -> None:
    assert json.loads(session_scope("kind-a", "/r", "w9")) == ["kind-a", "/r", "w9"]


# -- config ------------------------------------------------------------------------------


def _usage_raw(tmp_path: Path, limits: list[dict[str, Any]]) -> dict[str, Any]:
    root = tmp_path.resolve() / "logs"
    root.mkdir(mode=0o700, exist_ok=True)
    return {
        "timezone": "UTC",
        "sources": [{"kind": "kind-a", "format": "jsonl-message-usage-v1", "root": str(root)}],
        "limits": limits,
    }


def test_on_demand_block_needs_no_command(tmp_path: Path) -> None:
    config = usage_from(
        _usage_raw(
            tmp_path,
            [{"kind": "kind-a", "format": FORMAT_ID, "on_demand": True, "timeout_seconds": 90}],
        ),
        ("kind-a",),
    )
    assert config is not None
    (block,) = config.limits
    assert (block.command, block.on_demand, block.timeout_seconds) == ((), True, 90)


def test_on_demand_timeout_is_bounded(tmp_path: Path) -> None:
    for timeout in (30, 300):
        config = usage_from(
            _usage_raw(
                tmp_path,
                [
                    {
                        "kind": "kind-a",
                        "format": FORMAT_ID,
                        "on_demand": True,
                        "timeout_seconds": timeout,
                    }
                ],
            ),
            ("kind-a",),
        )
        assert config is not None
    for bad in (15, 29, 301, "90"):
        assert (
            usage_from(
                _usage_raw(
                    tmp_path,
                    [
                        {
                            "kind": "kind-a",
                            "format": FORMAT_ID,
                            "on_demand": True,
                            "timeout_seconds": bad,
                        }
                    ],
                ),
                ("kind-a",),
            )
            is None
        )


def test_timer_block_still_requires_a_command(tmp_path: Path) -> None:
    assert (
        usage_from(_usage_raw(tmp_path, [{"kind": "kind-a", "format": "timer-v1"}]), ("kind-a",))
        is None
    )
    config = usage_from(
        _usage_raw(
            tmp_path, [{"kind": "kind-a", "format": "timer-v1", "command": [sys.executable, "-x"]}]
        ),
        ("kind-a",),
    )
    assert config is not None and config.limits[0].command == (sys.executable, "-x")


def test_limits_rows_reject_bad_shapes(tmp_path: Path) -> None:
    rows = [
        {"kind": "kind-a", "format": FORMAT_ID, "on_demand": "yes", "timeout_seconds": 90},
        {
            "kind": "kind-a",
            "format": FORMAT_ID,
            "on_demand": True,
            "timeout_seconds": 90,
            "bogus": 1,
        },
        {"kind": "kind-b", "format": FORMAT_ID, "on_demand": True, "timeout_seconds": 90},
    ]
    for row in rows:
        assert usage_from(_usage_raw(tmp_path, [row]), ("kind-a",)) is None
    dup = {"kind": "kind-a", "format": FORMAT_ID, "on_demand": True, "timeout_seconds": 90}
    assert usage_from(_usage_raw(tmp_path, [dup, dup]), ("kind-a",)) is None


# -- catalog -------------------------------------------------------------------------------


def test_shipped_tui_recipes_validate_without_naming_tools() -> None:
    entries = [entry for entry in load_catalog().entries if entry.limits == FORMAT_ID]
    assert len(entries) == 2
    recipes = [entry.tui for entry in entries]
    assert all(recipe is not None for recipe in recipes)
    for recipe in recipes:
        assert recipe is not None
        assert recipe.windows and recipe.launch_new and "/" not in recipe.binary
        assert recipe.prompt_marker and recipe.usage_marker
    tracked = [
        recipe for recipe in recipes if recipe is not None and recipe.session_root is not None
    ]
    assert len(tracked) == 1
    assert tracked[0] is not None
    assert tracked[0].launch_resume is not None and tracked[0].hint_pattern is not None
    fresh = [recipe for recipe in recipes if recipe is not None and recipe.session_root is None]
    assert len(fresh) == 1
    assert fresh[0] is not None
    assert fresh[0].launch_resume is None and fresh[0].hint_pattern is None


# -- setup ---------------------------------------------------------------------------------


def _limits_row(kind: str = "kind-a", name: str = "Kind A", **over: Any) -> Any:
    return replace(support_row(kind, name, next_step="can be added"), limits=FORMAT_ID, **over)


def test_limits_candidates_need_a_recipe_a_binary_and_a_source() -> None:
    rows = (
        _limits_row(),
        _limits_row("kind-b", "Kind B", binary=None),
        _limits_row("kind-c", "Kind C"),
        replace(_limits_row("kind-d", "Kind D"), limits="none"),
    )
    report = DetectReport(rows=rows, herdr_status="ok", config_status="ok")
    candidates = limits_candidates(report, {"kind-a", "kind-b", "kind-d"})
    assert [(c.kind, c.display_name, c.format) for c in candidates] == [
        ("kind-a", "Kind A", FORMAT_ID)
    ]
    assert isinstance(candidates[0], LimitsCandidate)


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    fake = tmp_path / "home"
    fake.mkdir(mode=0o700)
    monkeypatch.setenv("HOME", str(fake))
    return fake


def _limits_config(home: Path) -> Path:
    """A fully loadable companion file: the appended block must validate too."""
    (home / "state").mkdir(mode=0o700, exist_ok=True)
    logs = home / "logs"
    logs.mkdir(mode=0o700, exist_ok=True)
    config = home / "companion.toml"
    config.write_text(
        "\n".join(
            [
                'advertised_origin = "https://mac.example-tailnet.ts.net"',
                'host_id = "home-mac"',
                f'state_dir = "{home / "state"}"',
                f'cookie_secret_file = "{home / "cookie.secret"}"',
                f'cursor_secret_file = "{home / "cursor.secret"}"',
                f'connector_credentials = "{home / "connector.json"}"',
                f'herdr_socket = "{home / "herdr.sock"}"',
                'agent_kinds = ["kind-a"]',
                "[usage]",
                "[[usage.sources]]",
                'kind = "kind-a"',
                'format = "jsonl-message-usage-v1"',
                f'root = "{logs.resolve()}"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    config.chmod(0o600)
    return config


def test_offer_limits_appends_on_consent(home: Path) -> None:
    config = _limits_config(home)
    report = DetectReport(rows=(_limits_row(),), herdr_status="ok", config_status="ok")
    confirm = Answers(True)
    _lines, code = offer_limits(config, confirm=confirm, detect_fn=lambda _path: report)
    assert code == 0
    assert confirm.asked == ["Add Kind A plan limits? [y/N] "]
    text = config.read_text(encoding="utf-8")
    assert "[[usage.limits]]" in text and 'kind = "kind-a"' in text
    assert "on_demand = true" in text and "timeout_seconds = 90" in text
    config_edit.verify_limits(text, [config_edit.LimitsEntry("kind-a", FORMAT_ID)])
    loaded = load_config(config)
    assert loaded.usage is not None
    (block,) = loaded.usage.limits
    assert (block.kind, block.format, block.on_demand, block.timeout_seconds) == (
        "kind-a",
        FORMAT_ID,
        True,
        90,
    )


def test_offer_limits_declined_writes_nothing(home: Path) -> None:
    config = _limits_config(home)
    before = config.read_bytes()
    report = DetectReport(rows=(_limits_row(),), herdr_status="ok", config_status="ok")
    lines, code = offer_limits(config, confirm=Answers(False), detect_fn=lambda _path: report)
    assert code == 0 and config.read_bytes() == before
    assert any("not added" in line for line in lines)


def test_offer_limits_skips_present_blocks(home: Path) -> None:
    config = _limits_config(home)
    report = DetectReport(rows=(_limits_row(),), herdr_status="ok", config_status="ok")
    offer_limits(config, confirm=Answers(True), detect_fn=lambda _path: report)
    confirm = Answers(True)
    lines, code = offer_limits(config, confirm=confirm, detect_fn=lambda _path: report)
    assert code == 0 and confirm.asked == []
    assert any("nothing to add" in line for line in lines)


def test_append_limits_round_trips_through_verify() -> None:
    text = config_edit.append_limits("", [config_edit.LimitsEntry("kind-a", FORMAT_ID)])
    assert "[[usage.limits]]" in text
    config_edit.verify_limits(text, [config_edit.LimitsEntry("kind-a", FORMAT_ID)])


def test_an_idle_on_demand_spec_lets_the_worker_sleep(tmp_path: Path) -> None:
    """An on-demand spec has no due time; the worker must not spin waiting for it."""
    from relaydeck.usage.limits_plane.scheduler import _IDLE_SLEEP_S

    scheduler, _, store, _, clock = _od_scheduler(tmp_path, [_ok_result()])
    try:
        assert scheduler.run_once(clock()) == 0
        assert scheduler._run_loop_sleep_s(clock()) == _IDLE_SLEEP_S
    finally:
        store.close()


def test_a_versioned_launcher_binary_counts_as_the_tool() -> None:
    from relaydeck.usage.limits_plane.providers.tui_usage_pane import _matches_binary

    names = frozenset({"tool"})
    assert _matches_binary("tool", names)
    assert _matches_binary("tool-bin-1.4.6-R6000.1", names)
    assert not _matches_binary("toolbox", names)
    assert not _matches_binary("zsh", names)
    assert not _matches_binary(None, names)
