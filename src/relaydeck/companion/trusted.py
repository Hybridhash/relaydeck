"""Executable trust checks shared by local setup and diagnostic probes."""

from __future__ import annotations

import os
import stat
from collections import deque
from collections.abc import Mapping, Sequence
from pathlib import Path

_WRITE = stat.S_IWGRP | stat.S_IWOTH
_ENV = ("HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "TMPDIR")
_SYSTEM_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"


def _resolve(path: Path, *, directory: bool = False) -> Path | None:
    if not path.is_absolute():
        return None
    pending = deque(path.parts[1:])
    current = Path(path.anchor)
    links = 0
    try:
        while pending:
            part = pending.popleft()
            if part in ("", "."):
                continue
            if part == "..":
                current = current.parent
                continue
            candidate = current / part
            info = candidate.lstat()
            if info.st_uid not in (0, os.getuid()):
                return None
            if stat.S_ISLNK(info.st_mode):
                links += 1
                if links > 40:
                    return None
                target = Path(os.readlink(candidate))
                if target.is_absolute():
                    current = Path(target.anchor)
                    parts = target.parts[1:]
                else:
                    parts = target.parts
                pending.extendleft(reversed(parts))
                continue
            if pending or directory:
                if not stat.S_ISDIR(info.st_mode):
                    return None
                if info.st_mode & _WRITE and (not pending or not info.st_mode & stat.S_ISVTX):
                    return None
            else:
                if not stat.S_ISREG(info.st_mode) or info.st_mode & (
                    _WRITE | stat.S_ISUID | stat.S_ISGID
                ):
                    return None
                if current.stat().st_mode & _WRITE or not os.access(candidate, os.X_OK):
                    return None
            current = candidate
        info = current.lstat()
        if directory:
            return current if stat.S_ISDIR(info.st_mode) and not info.st_mode & _WRITE else None
        return current if stat.S_ISREG(info.st_mode) and os.access(current, os.X_OK) else None
    except (OSError, ValueError):
        return None


def trusted_executable(program: str, *, directories: Sequence[str] | None = None) -> str | None:
    """Resolve every link hop, checking its original and target ancestry before execution."""
    if os.path.isabs(program):
        resolved = _resolve(Path(program))
        return str(resolved) if resolved is not None else None
    if not program or os.sep in program:
        return None
    paths = (
        os.environ.get("PATH", _SYSTEM_PATH).split(os.pathsep)
        if directories is None
        else directories
    )
    for directory in paths:
        if not os.path.isabs(directory):
            continue
        resolved = _resolve(Path(directory) / program)
        if resolved is not None:
            return str(resolved)
    return None


def probe_environment(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """Keep user/locale context without loader, interpreter or shell injection variables."""
    source = os.environ if environ is None else environ
    env = {name: source[name] for name in _ENV if name in source}
    paths: list[str] = []
    for directory in (*source.get("PATH", "").split(os.pathsep), *_SYSTEM_PATH.split(os.pathsep)):
        resolved = _resolve(Path(directory), directory=True) if directory else None
        if resolved is not None and str(resolved) not in paths:
            paths.append(str(resolved))
    env["PATH"] = os.pathsep.join(paths)
    return env
