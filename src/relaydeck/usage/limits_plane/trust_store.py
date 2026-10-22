"""Read-only reader for a tool's own folder-trust file (`schema_version` 1, `projects` map).

Anything else, or any read problem, is "unknown" (None) and the caller behaves as before."""

from __future__ import annotations

import json
import os
from pathlib import Path

MAX_BYTES = 1024 * 1024
SUPPORTED_VERSION = 1


def trusted_folders(store: str) -> tuple[Path, ...] | None:
    """Existing real folders the file marks trusted, shortest path first, or None when the file
    is unreadable or not the version-1 shape."""
    try:
        path = Path(os.path.expanduser(store))
        if path.stat().st_size > MAX_BYTES:
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("schema_version") != SUPPORTED_VERSION:
        return None
    projects = data.get("projects")
    if not isinstance(projects, dict):
        return None
    out: set[Path] = set()
    for folder, entry in projects.items():
        if (
            isinstance(folder, str)
            and folder.startswith("/")
            and "\x00" not in folder
            and isinstance(entry, dict)
            and entry.get("decision") == "trusted"
        ):
            try:
                real = Path(folder).resolve(strict=True)
            except (OSError, RuntimeError):
                continue
            if real.is_dir():
                out.add(real)
    return tuple(sorted(out, key=lambda item: (len(str(item)), str(item))))


def is_trusted(folder: str, trusted: tuple[Path, ...]) -> bool:
    try:
        return Path(folder).resolve(strict=True) in trusted
    except (OSError, RuntimeError):
        return False


STATE_DIR_TOKEN = "{state_dir}"


def probe_folder(template: str | None, state_dir: Path) -> Path | None:
    """The recipe's own empty folder, `{state_dir}/name` made concrete."""
    if not template:
        return None
    return Path(template.replace(STATE_DIR_TOKEN, str(state_dir), 1))


def ensure_probe_folder(folder: Path) -> bool:
    """Create the folder (0700) when missing. Nothing is ever written into it. False on failure."""
    try:
        if folder.is_symlink():
            return False
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        return folder.is_dir()
    except OSError:
        return False
