"""Whole-file read for a descriptor already opened and size-checked by the caller."""

from __future__ import annotations

import os


def read_all(fd: int, size: int) -> bytes:
    """Read exactly `size` bytes (or up to end of file, if it shrank) in 1 MiB chunks."""
    chunks: list[bytes] = []
    offset = 0
    while offset < size:
        chunk = os.pread(fd, min(size - offset, 1 << 20), offset)
        if not chunk:
            break
        chunks.append(chunk)
        offset += len(chunk)
    return b"".join(chunks)
