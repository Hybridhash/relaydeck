"""Map native selected text back to the frozen terminal snapshot."""

from __future__ import annotations


def selected_offsets(text: str, selected: str) -> tuple[int, int] | None:
    if not selected:
        return None
    start = text.find(selected)
    if start < 0:
        selected = selected.replace("\n", "\r\n")
        start = text.find(selected)
    return (start, start + len(selected)) if start >= 0 else None
