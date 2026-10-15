"""Private local state; reject unsafe existing paths instead of repairing them."""

from __future__ import annotations

import json
import os
import stat
import tempfile
from pathlib import Path
from typing import Any


def private_dir(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    check_private(path, directory=True)


def check_private(path: Path, *, directory: bool = False) -> None:
    info = path.lstat()
    kind = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if not kind or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError(
            f"{path.name}: expected an owner-only {'directory' if directory else 'file'}"
        )


def read_private(path: Path) -> str:
    check_private(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, encoding="utf-8") as handle:
        if os.fstat(handle.fileno()).st_size > 1024 * 1024:
            raise ValueError(f"{path.name}: file too large")
        return handle.read()


def write_json(path: Path, body: dict[str, Any]) -> None:
    if path.exists() or path.is_symlink():
        check_private(path)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".companion-", suffix=".tmp")
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(body, handle, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
