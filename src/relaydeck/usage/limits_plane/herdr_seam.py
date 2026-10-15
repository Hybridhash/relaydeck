"""Bounded Herdr CLI seam for on-demand TUI limit reads.

All pane/tab control flows through `HerdrSeam`; tests inject fakes. The
production seam shells out to the absolute herdr binary via the scheduler's
bounded runner (no shell, hard timeouts, stderr discarded). Every response is
strictly validated: ids are short strings, lists are capped, and anything
unexpected is an error, never a guess.

Two sharp edges are handled by design, not by trust:
- `send-text`/`send-keys`/`pane run` answer empty output on success, which is
  indistinguishable from a CLI failure under a stdout-only runner. Writes are
  therefore NEVER trusted alone: every write is followed by a `wait-output`
  for the expected marker (send, verify, bounded retry, else abort).
- `wait-output` answers JSON only when matched; anything else (including a
  herdr-side timeout) reads as "not matched". The abort path then re-probes
  the tab, so a sick herdr surfaces as unavailable rather than a tool timeout.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Protocol

MAX_ID_CHARS = 256
MAX_LIST_ENTRIES = 4096
WAIT_MARGIN_S = 5.0


class SeamTimeout(TimeoutError):
    """A herdr call overran its own timeout."""


class SeamError(Exception):
    """A herdr call failed or answered something unusable."""


# argv + timeout seconds -> stdout bytes. Raises TimeoutError, OSError,
# ValueError (mirrors the scheduler's FetchContext.run minus the env slot).
Runner = Callable[[Sequence[str], float], bytes]


class HerdrSeam(Protocol):
    """One answered question per method; no hidden retries or state."""

    def tab_create(
        self, workspace: str, cwd: str | None, label: str, timeout_s: float
    ) -> tuple[str, str]:
        """(tab_id, root pane_id) for the new `--no-focus` tab."""
        ...

    def tab_close(self, tab_id: str, timeout_s: float) -> None: ...
    def tab_get(self, tab_id: str, timeout_s: float) -> dict[str, Any] | None:
        """The tab document, or None when it is gone."""
        ...

    def tab_list(self, workspace: str, timeout_s: float) -> list[dict[str, Any]]: ...
    def pane_run(self, pane_id: str, command: str, timeout_s: float) -> None:
        """Type `command` plus Enter (unverified: the caller must confirm)."""
        ...

    def wait_output(
        self, pane_id: str, regex: str, timeout_ms: int, lines: int, timeout_s: float
    ) -> bool:
        """True when the pattern matched in time (Rust regex syntax)."""
        ...

    def send_text(self, pane_id: str, text: str, timeout_s: float) -> None: ...
    def send_keys(self, pane_id: str, keys: Sequence[str], timeout_s: float) -> None: ...
    def pane_read(self, pane_id: str, lines: int, timeout_s: float) -> str:
        """Visible text (possibly empty on error: match failure, never proof)."""
        ...

    def pane_list(self, timeout_s: float) -> list[dict[str, Any]]:
        """Every pane on the server (caller filters by workspace/agent)."""
        ...

    def process_info(self, pane_id: str, timeout_s: float) -> dict[str, Any] | None:
        """The `result.process_info` document, or None when unusable."""
        ...


def _clean_id(value: Any) -> str | None:
    if not isinstance(value, str) or not 0 < len(value) <= MAX_ID_CHARS:
        return None
    if any(ord(c) < 32 for c in value):
        return None
    return value


def _document(raw: bytes) -> dict[str, Any] | None:
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


class CliHerdrSeam:
    """Production seam over one absolute herdr argv prefix."""

    def __init__(self, herdr: Sequence[str], run: Runner) -> None:
        self._herdr = tuple(herdr)
        self._run = run

    def _call(self, args: Sequence[str], timeout_s: float) -> bytes:
        if not self._herdr or timeout_s <= 0:
            raise SeamError("seam_unavailable")
        try:
            return self._run((*self._herdr, *args), timeout_s)
        except TimeoutError as exc:
            raise SeamTimeout("herdr_timeout") from exc
        except (OSError, ValueError) as exc:
            raise SeamError("herdr_failed") from exc

    def tab_create(
        self, workspace: str, cwd: str | None, label: str, timeout_s: float
    ) -> tuple[str, str]:
        if _clean_id(workspace) is None or _clean_id(label) is None:
            raise SeamError("bad_tab_target")
        args: list[str] = ["tab", "create", "--workspace", workspace, "--label", label]
        if cwd is not None:
            if not cwd or any(ord(c) < 32 for c in cwd):
                raise SeamError("bad_tab_target")
            args += ["--cwd", cwd]
        args.append("--no-focus")
        doc = _document(self._call(args, timeout_s))
        result = doc.get("result") if doc else None
        tab = result.get("tab") if isinstance(result, dict) else None
        pane = result.get("root_pane") if isinstance(result, dict) else None
        tab_id = _clean_id(tab.get("tab_id")) if isinstance(tab, dict) else None
        pane_id = _clean_id(pane.get("pane_id")) if isinstance(pane, dict) else None
        echoed = tab.get("label") if isinstance(tab, dict) else None
        if tab_id is None or pane_id is None or echoed != label:
            raise SeamError("bad_tab_create")
        return tab_id, pane_id

    def tab_close(self, tab_id: str, timeout_s: float) -> None:
        if _clean_id(tab_id) is None:
            raise SeamError("bad_tab_id")
        doc = _document(self._call(["tab", "close", tab_id], timeout_s))
        result = doc.get("result") if doc else None
        if not isinstance(result, dict) or result.get("type") != "ok":
            raise SeamError("tab_close_failed")

    def tab_get(self, tab_id: str, timeout_s: float) -> dict[str, Any] | None:
        if _clean_id(tab_id) is None:
            raise SeamError("bad_tab_id")
        try:
            doc = _document(self._call(["tab", "get", tab_id], timeout_s))
        except (SeamTimeout, SeamError):
            return None
        result = doc.get("result") if doc else None
        tab = result.get("tab") if isinstance(result, dict) else None
        return tab if isinstance(tab, dict) else None

    def tab_list(self, workspace: str, timeout_s: float) -> list[dict[str, Any]]:
        if _clean_id(workspace) is None:
            raise SeamError("bad_workspace")
        doc = _document(self._call(["tab", "list", "--workspace", workspace], timeout_s))
        result = doc.get("result") if doc else None
        tabs = result.get("tabs") if isinstance(result, dict) else None
        if not isinstance(tabs, list):
            raise SeamError("bad_tab_list")
        return [tab for tab in tabs[:MAX_LIST_ENTRIES] if isinstance(tab, dict)]

    def pane_run(self, pane_id: str, command: str, timeout_s: float) -> None:
        if _clean_id(pane_id) is None or not command or any(ord(c) < 32 for c in command):
            raise SeamError("bad_pane_run")
        self._call(["pane", "run", pane_id, command], timeout_s)

    def wait_output(
        self, pane_id: str, regex: str, timeout_ms: int, lines: int, timeout_s: float
    ) -> bool:
        if (
            _clean_id(pane_id) is None
            or not regex
            or timeout_ms <= 0
            or lines <= 0
            or timeout_s <= timeout_ms / 1000
        ):
            raise SeamError("bad_wait")
        try:
            raw = self._call(
                [
                    "pane",
                    "wait-output",
                    pane_id,
                    "--regex",
                    regex,
                    "--timeout",
                    str(timeout_ms),
                    "--lines",
                    str(lines),
                ],
                timeout_s,
            )
        except (SeamTimeout, SeamError):
            return False
        doc = _document(raw)
        result = doc.get("result") if doc else None
        return isinstance(result, dict) and result.get("type") == "output_matched"

    def send_text(self, pane_id: str, text: str, timeout_s: float) -> None:
        if _clean_id(pane_id) is None or not text or "\x00" in text:
            raise SeamError("bad_send_text")
        self._call(["pane", "send-text", pane_id, text], timeout_s)

    def send_keys(self, pane_id: str, keys: Sequence[str], timeout_s: float) -> None:
        if (
            _clean_id(pane_id) is None
            or not keys
            or any(not isinstance(key, str) or not key for key in keys)
        ):
            raise SeamError("bad_send_keys")
        self._call(["pane", "send-keys", pane_id, *keys], timeout_s)

    def pane_read(self, pane_id: str, lines: int, timeout_s: float) -> str:
        if _clean_id(pane_id) is None or lines <= 0:
            raise SeamError("bad_pane_read")
        try:
            raw = self._call(
                ["pane", "read", pane_id, "--source", "visible", "--lines", str(lines)],
                timeout_s,
            )
        except (SeamTimeout, SeamError):
            return ""
        return raw.decode("utf-8", "replace")

    def pane_list(self, timeout_s: float) -> list[dict[str, Any]]:
        doc = _document(self._call(["pane", "list"], timeout_s))
        result = doc.get("result") if doc else None
        panes = result.get("panes") if isinstance(result, dict) else None
        if not isinstance(panes, list):
            raise SeamError("bad_pane_list")
        return [pane for pane in panes[:MAX_LIST_ENTRIES] if isinstance(pane, dict)]

    def process_info(self, pane_id: str, timeout_s: float) -> dict[str, Any] | None:
        if _clean_id(pane_id) is None:
            raise SeamError("bad_pane_id")
        try:
            doc = _document(self._call(["pane", "process-info", "--pane", pane_id], timeout_s))
        except (SeamTimeout, SeamError):
            return None
        result = doc.get("result") if doc else None
        info = result.get("process_info") if isinstance(result, dict) else None
        return info if isinstance(info, dict) else None


def run_from_context(
    run: Callable[[Sequence[str], float, Mapping[str, str] | None], bytes],
) -> Runner:
    """Adapt a FetchContext runner (with env slot) to the seam's Runner."""

    def invoke(argv: Sequence[str], timeout_s: float) -> bytes:
        return run(argv, timeout_s, None)

    return invoke
