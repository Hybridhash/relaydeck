"""Bounded owner-file operations for the opt-in limits-hook transaction."""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import secrets
import stat
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MAX_BYTES = 1024 * 1024


class HookFileError(Exception):
    """Fixed, content-free outcome; never stores a path or exception text."""


def absolute(path: Path) -> None:
    if not path.is_absolute() or ".." in path.parts:
        raise HookFileError("invalid")


def object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise HookFileError("invalid")
        value[key] = item
    return value


def document(raw: bytes) -> dict[str, Any]:
    def reject_constant(_: str) -> None:
        raise HookFileError("invalid")

    value = json.loads(raw, object_pairs_hook=object_pairs, parse_constant=reject_constant)
    if not isinstance(value, dict):
        raise HookFileError("invalid")
    return value


def encode(value: dict[str, Any]) -> bytes:
    raw = (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode()
    if len(raw) > MAX_BYTES:
        raise HookFileError("invalid")
    return raw


@contextmanager
def directory(
    path: Path, *, private: bool = False, create: bool = False, missing_state: str | None = None
) -> Iterator[int]:
    """Pin every ancestor via O_NOFOLLOW, including parents of the leaf file."""
    absolute(path)
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            try:
                following = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    if missing_state is not None:
                        raise HookFileError(missing_state) from None
                    raise
                os.mkdir(part, 0o700, dir_fd=fd)
                os.fsync(fd)
                following = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = following
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or info.st_mode & (0o077 if private else 0o022):
            raise HookFileError("invalid")
        yield fd
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            raise HookFileError("invalid") from None
        raise
    finally:
        os.close(fd)


@dataclass(frozen=True)
class Image:
    raw: bytes | None = None
    identity: tuple[int, int, int, int, int] | None = None

    @property
    def digest(self) -> str | None:
        return hashlib.sha256(self.raw).hexdigest() if self.raw is not None else None


def read(fd: int, name: str, *, private: bool = False, bound: int = MAX_BYTES) -> Image:
    try:
        file_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    except FileNotFoundError:
        return Image()
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise HookFileError("invalid") from None
        raise
    with os.fdopen(file_fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_nlink != 1
            or info.st_mode & (0o077 if private else 0o022)
            or info.st_size > bound
        ):
            raise HookFileError("invalid")
        raw = stream.read(bound + 1)
        after = os.fstat(stream.fileno())

        def identity(row: os.stat_result) -> tuple[int, int, int, int, int]:
            return (row.st_dev, row.st_ino, row.st_mtime_ns, row.st_size, row.st_mode)

        if len(raw) > bound or identity(info) != identity(after):
            raise HookFileError("concurrent_edit")
        return Image(raw, identity(info))


@contextmanager
def lock(fd: int, name: str, *, deadline: float | None = None) -> Iterator[None]:
    """An exclusive lock wait bounded by one absolute monotonic deadline.

    A transaction that takes several locks passes the same deadline to each, so the
    whole transaction's lock waiting shares one bound. File I/O itself is bounded
    only by the read size caps, never by this deadline."""
    file_fd = os.open(
        name, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=fd
    )
    try:
        info = os.fstat(file_fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_nlink != 1
            or info.st_mode & 0o077
        ):
            raise HookFileError("invalid")
        end = time.monotonic() + 0.2 if deadline is None else deadline
        while True:
            try:
                fcntl.flock(file_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= end:
                    raise HookFileError("busy") from None
                time.sleep(0.005)
        yield
    finally:
        os.close(file_fd)


def atomic(
    path: Path,
    fd: int,
    raw: bytes,
    expected: Image,
    *,
    private: bool,
    committed: Callable[[], None] = lambda: None,
) -> None:
    """fsync temp → recheck digest/identity + directory → rename → directory fsync."""
    temporary = f".limits-{secrets.token_hex(16)}.tmp"
    file_fd = os.open(
        temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd
    )
    try:
        with os.fdopen(file_fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        with directory(path.parent, private=private) as current_fd:
            before, current = os.fstat(fd), os.fstat(current_fd)
            if (before.st_dev, before.st_ino) != (current.st_dev, current.st_ino):
                raise HookFileError("concurrent_edit")
        if read(fd, path.name, private=private) != expected:
            raise HookFileError("concurrent_edit")
        os.replace(temporary, path.name, src_dir_fd=fd, dst_dir_fd=fd)
        committed()
        os.fsync(fd)
    finally:
        try:
            os.unlink(temporary, dir_fd=fd)
        except FileNotFoundError:
            pass
