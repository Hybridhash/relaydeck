"""The size a terminal should attach at: what this page's terminal area last measured.

A host terminal attached at a guessed 80x24 is resized moments later to the real size, and a
full-screen program redraws twice (and can lose its place at the end). Every terminal on a page
shares one terminal area, so the size a mounted terminal last reported is the right size for the
next attach on that page. Only applied reports count (`_last_view_size` is set after verification),
so a size seen while a viewer was unverified never reaches a host.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

DEFAULT_GEOMETRY = (80, 24)
_MAX_CELLS = 1024


def reported_geometry(term: Any) -> tuple[int, int] | None:
    size = getattr(term, "_last_view_size", None)
    if (
        isinstance(size, tuple)
        and len(size) == 2
        and all(type(value) is int and 1 <= value <= _MAX_CELLS for value in size)
    ):
        return size
    return None


def page_geometry(terms: Iterable[Any]) -> tuple[int, int] | None:
    """The first valid size among `terms` (pass the most relevant first)."""
    for term in terms:
        size = reported_geometry(term)
        if size is not None:
            return size
    return None
