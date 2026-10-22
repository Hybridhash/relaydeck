"""Setup for tools that read their usage in RelayDeck's own empty folder (a `probe_dir` recipe).

The folder is created 0700 and left empty. Trusting it is the user's own choice, made in the
tool's own prompt in their terminal: nothing here answers that prompt, sends input to the tool,
or writes the tool's trust file."""

from __future__ import annotations

import shutil
import subprocess
import sys
import tomllib
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from relaydeck.usage.limits_plane.trust_store import (
    ensure_probe_folder,
    is_trusted,
    probe_folder,
    trusted_folders,
)

TRUSTED = "trusted"
NOT_TRUSTED = "not trusted"
NOT_INSTALLED = "not installed"
FIX_HINT = "run: relaydeck-companion usage install"
Spawn = Callable[[Sequence[str], Path], int]


@dataclass(frozen=True)
class ProbeTarget:
    kind: str
    binary: str
    folder: Path
    trust_store: str | None


def load_probe_targets(config_path: Path) -> tuple[ProbeTarget, ...]:
    """Enabled `[[usage.limits]]` blocks whose catalog recipe names a probe folder. Any problem
    reading the config or catalog means none: setup then behaves exactly as before."""
    from relaydeck.companion.config import DEFAULT_STATE
    from relaydeck.companion.detect import CatalogError, load_catalog

    try:
        body = tomllib.loads(config_path.read_text(encoding="utf-8"))
        entries = {entry.id: entry for entry in load_catalog().entries}
    except (OSError, ValueError, CatalogError):
        return ()
    usage = body.get("usage")
    blocks = usage.get("limits", []) if isinstance(usage, dict) else []
    state = body.get("state_dir")
    state_dir = Path(str(state if isinstance(state, str) and state else DEFAULT_STATE))
    state_dir = state_dir.expanduser()
    targets: list[ProbeTarget] = []
    for block in blocks if isinstance(blocks, list) else []:
        if not isinstance(block, dict) or block.get("enabled", True) is not True:
            continue
        recipe = getattr(entries.get(str(block.get("kind"))), "tui", None)
        folder = probe_folder(getattr(recipe, "probe_dir", None), state_dir)
        if recipe is None or folder is None:
            continue
        target = ProbeTarget(str(block["kind"]), recipe.binary, folder, recipe.trust_store)
        if all(t.kind != target.kind for t in targets):
            targets.append(target)
    return tuple(targets)


def _on_path(name: str) -> str | None:
    return shutil.which(name)


def probe_state(target: ProbeTarget, which: Callable[[str], Any] = _on_path) -> str:
    if which(target.binary) is None:
        return NOT_INSTALLED
    trusted = trusted_folders(target.trust_store) if target.trust_store else None
    return TRUSTED if trusted and is_trusted(str(target.folder), trusted) else NOT_TRUSTED


def doctor_line(target: ProbeTarget, state: str) -> str:
    detail = f"{NOT_TRUSTED} ({FIX_HINT})" if state == NOT_TRUSTED else state
    if state == NOT_INSTALLED:
        detail = f"{target.binary} not installed"
    return f"{target.binary} probe folder {target.folder}: {detail}"


def _run_in_terminal(argv: Sequence[str], cwd: Path) -> int:
    # Inherits stdin, stdout and stderr: the user answers the tool's prompt themselves.
    return subprocess.run(list(argv), cwd=cwd, check=False).returncode


def offer_trust(
    target: ProbeTarget,
    *,
    ask: Callable[[str], bool],
    spawn: Spawn = _run_in_terminal,
    interactive: bool | None = None,
    which: Callable[[str], Any] = _on_path,
    say: Callable[[str], None] | None = None,
) -> str:
    """Create the folder, and when it isn't trusted offer to open the tool there. Returns the
    state afterwards. Without a terminal it only prints the one-line instruction."""
    out = say or (lambda text: print(text, file=sys.stderr))
    if which(target.binary) is None:
        return NOT_INSTALLED
    ensure_probe_folder(target.folder)
    state = probe_state(target, which)
    if state == TRUSTED:
        return state
    live = sys.stdin.isatty() if interactive is None else interactive
    if not live:
        name = target.binary
        out(f"Trust RelayDeck's {name} folder once: run `{name}` in {target.folder}, choose Trust.")
        return state
    out(
        f"RelayDeck reads {target.binary} usage in its own empty folder; "
        f"{target.binary} asks once whether to trust it."
    )
    if not ask(f"Open {target.binary} there now so you can choose Trust? [y/N] "):
        out(f"Skipped. Later: run `{target.binary}` in {target.folder} and choose Trust.")
        return state
    out(f"Choose Trust in {target.binary}, then type /quit.")
    spawn((target.binary,), target.folder)
    state = probe_state(target, which)
    out(f"{target.binary} probe folder is now {state}.")
    return state


def ask_on_terminal(prompt: str) -> bool:
    print(prompt, end="", file=sys.stderr, flush=True)
    return sys.stdin.readline().strip().lower() in ("y", "yes")
