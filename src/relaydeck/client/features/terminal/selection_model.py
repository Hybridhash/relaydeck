"""Output selection and copy domain. Flet-free.

Anchor/focus selection over projected screen+scrollback text, explicit
copy through the clipboard port, and the live-selection pull against the
terminal extension. Pure functions plus one clipboard write; nothing here
touches the PTY input path, the composer draft, or any socket.

Behaviour:
- the sheet shows the full output text with an optional anchor range;
- a nil or out-of-bounds anchor selects ALL text (`normalize_range`);
- content is read-only, selectable, and monospaced (see selection_sheet);
- dismissal is explicit (Done), never a background action.

Wrap rule: `TerminalProjection` exposes physical rows without wrap flags,
so a row whose display width fills the full terminal width is treated as
soft-wrapped and joins with the next row WITHOUT a newline. A hard newline
immediately after a full-width row is indistinguishable from a wrap and
joins too; that accepted difference is pinned by
`test_full_row_hard_newline_joins_documented`.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Protocol

from relaydeck.client.adapters.ports import ClipboardPort
from relaydeck.client.features.terminal.models import SelectionState, TerminalViewState
from relaydeck.client.features.terminal.projection import TerminalProjection

if TYPE_CHECKING:
    from relaydeck_terminal.terminal import RelayDeckTerminal


def cell_width(char: str) -> int:
    """Display width of one character. Mirrors the projection oracle rule
    (`projection._char_width`): combining and format marks occupy no cell,
    East-Asian wide/fullwidth occupy two, everything else one."""
    if unicodedata.combining(char):
        return 0
    if unicodedata.category(char) == "Cf":
        return 0
    return 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1


def display_width(text: str) -> int:
    return sum(cell_width(char) for char in text)


def _continues_cluster(text: str, index: int) -> bool:
    """True when `text[index]` belongs to the previous grapheme cluster:
    a zero-width mark following a base character on the same line. Wide
    and emoji characters are single code points and always start their
    own cluster, so str slicing can never corrupt them."""
    if index <= 0 or index >= len(text):
        return False
    if text[index - 1] == "\n" or text[index] == "\n":
        return False
    return cell_width(text[index]) == 0


def snap_down(text: str, offset: int) -> int:
    """Snap `offset` down to the containing cluster start so a selection
    edge never splits a base character from its combining marks."""
    offset = max(0, min(len(text), offset))
    while _continues_cluster(text, offset):
        offset -= 1
    return offset


def snap_up(text: str, offset: int) -> int:
    """Snap `offset` up past any combining marks that follow the boundary,
    so a selection edge never leaves a trailing mark behind."""
    offset = max(0, min(len(text), offset))
    while offset < len(text) and _continues_cluster(text, offset):
        offset += 1
    return offset


@dataclass(frozen=True)
class SelectionDocument:
    """Flattened selectable text: logical lines plus the flat offsets at
    which each logical line starts. Soft-wrapped physical rows are already
    joined, so extracting a range never invents phantom newlines."""

    logical_lines: tuple[str, ...]
    starts: tuple[int, ...]
    text: str
    cols: int
    alt_screen: bool = False

    @property
    def length(self) -> int:
        return len(self.text)

    @property
    def line_count(self) -> int:
        return len(self.logical_lines)


def document_from_rows(
    rows: Sequence[str], cols: int, *, alt_screen: bool = False
) -> SelectionDocument:
    """Fold physical rows into logical lines: a row continues the previous
    logical line exactly when the previous row is full-width (soft wrap)."""
    logical: list[str] = []
    prev_full = False
    for row in rows:
        if logical and prev_full:
            logical[-1] = logical[-1] + row
        else:
            logical.append(row)
        prev_full = cols > 0 and display_width(row) >= cols
    text = "\n".join(logical)
    starts: list[int] = []
    offset = 0
    for line in logical:
        starts.append(offset)
        offset += len(line) + 1
    return SelectionDocument(
        logical_lines=tuple(logical),
        starts=tuple(starts),
        text=text,
        cols=cols,
        alt_screen=alt_screen,
    )


def document_from_projection(projection: TerminalProjection) -> SelectionDocument:
    """Selection source: scrollback plus visible screen as one document.
    On the alternate screen there is no scrollback, so only visible lines
    are selectable."""
    rows = (
        projection.lines if projection.alt_active else [*projection.scrollback, *projection.lines]
    )
    return document_from_rows(rows, projection.cols, alt_screen=projection.alt_active)


@dataclass(frozen=True)
class SelectionRange:
    """Anchor/focus pair in flat str offsets. Reversed pairs are legal and
    normalize on extraction; offsets snap to grapheme boundaries."""

    anchor: int
    focus: int

    @property
    def ordered(self) -> tuple[int, int]:
        if self.anchor <= self.focus:
            return (self.anchor, self.focus)
        return (self.focus, self.anchor)

    @property
    def start(self) -> int:
        return self.ordered[0]

    @property
    def end(self) -> int:
        return self.ordered[1]

    def extract(self, text: str) -> str:
        start = snap_down(text, self.start)
        end = snap_up(text, self.end)
        return text[start:end]


def normalize_range(anchor: int | None, focus: int | None, text: str) -> SelectionRange:
    """Normalize a selection: a missing or out-of-bounds
    anchor selects the whole text; otherwise the anchor stands as given."""
    length = len(text)
    if anchor is None or focus is None:
        return SelectionRange(0, length)
    if not (0 <= anchor <= length and 0 <= focus <= length):
        return SelectionRange(0, length)
    return SelectionRange(snap_down(text, anchor), snap_up(text, focus))


def offset_at(document: SelectionDocument, line: int, col: int) -> int:
    """Flat offset of a (line, col) position, clamped into the document."""
    if document.line_count == 0:
        return 0
    line = max(0, min(document.line_count - 1, line))
    col = max(0, min(len(document.logical_lines[line]), col))
    return document.starts[line] + col


def range_for_lines(document: SelectionDocument, first: int, last: int) -> SelectionRange:
    """Whole logical lines `first..last` (inclusive), internal newlines
    included, no trailing newline appended."""
    if document.line_count == 0:
        return SelectionRange(0, 0)
    first = max(0, min(document.line_count - 1, first))
    last = max(0, min(document.line_count - 1, last))
    if first > last:
        first, last = last, first
    start = document.starts[first]
    end = document.starts[last] + len(document.logical_lines[last])
    return SelectionRange(start, end)


def str_to_utf16_offset(text: str, offset: int) -> int:
    """Map a str offset to UTF-16 code units (Flutter selection offsets and
    NSRange units are UTF-16; astral characters count double)."""
    offset = max(0, min(len(text), offset))
    return len(text[:offset].encode("utf-16-le")) // 2


def utf16_to_str_offset(text: str, offset: int) -> int:
    """Map UTF-16 code units back to a str offset, clamped."""
    units = 0
    for index, char in enumerate(text):
        char_units = 2 if ord(char) > 0xFFFF else 1
        if units + char_units > offset:
            return index
        units += char_units
    return len(text)


# -- explicit copy path (clipboard port only) -------------------------------


async def copy_text(value: str, clipboard: ClipboardPort) -> str:
    """Write exactly `value` to the clipboard. Returns what was written so
    callers and tests can assert byte-exact content."""
    await clipboard.set_text(value)
    return value


async def copy_range(text: str, selection: SelectionRange, clipboard: ClipboardPort) -> str:
    return await copy_text(selection.extract(text), clipboard)


async def copy_all(text: str, clipboard: ClipboardPort) -> str:
    return await copy_text(text, clipboard)


# -- live selection (extension-supported; no Dart changes) -------------------


class LiveSelectionSource(Protocol):
    """Structural surface of `RelayDeckTerminal` used here: request the
    live emulator selection text (packet 0x06/0x83) or clear it (0x07)."""

    async def get_selection_text(self, timeout: float = 5.0) -> str: ...
    def clear_selection(self) -> None: ...


async def copy_live_selection(
    term: LiveSelectionSource | RelayDeckTerminal,
    clipboard: ClipboardPort,
    *,
    timeout: float = 5.0,
) -> str:
    """Copy the live emulator selection: ask the extension for the current
    xterm2 selection text, then write it verbatim to the clipboard. Emits
    no PTY input: the request is a selection query (packet 0x06), and the
    protocol has no Python->Dart input packet at all."""
    text = await term.get_selection_text(timeout=timeout)
    await clipboard.set_text(text)
    return text


def clear_live_selection(term: LiveSelectionSource | RelayDeckTerminal) -> None:
    term.clear_selection()


# -- selection state reducers (pure; input/draft untouched) ------------------


def open_selection(
    state: TerminalViewState,
    *,
    anchor: int | None = None,
    focus: int | None = None,
) -> TerminalViewState:
    """Enter selection mode. Only the selection slice changes; scroll, lease,
    stream offsets, and any future draft state are preserved untouched."""
    return replace(state, selection=SelectionState(open=True, anchor=anchor, focus=focus))


def close_selection(state: TerminalViewState) -> TerminalViewState:
    return replace(state, selection=SelectionState(open=False))


def move_focus(state: TerminalViewState, focus: int) -> TerminalViewState:
    current = state.selection
    return replace(state, selection=replace(current, focus=focus))


def move_anchor(state: TerminalViewState, anchor: int) -> TerminalViewState:
    current = state.selection
    return replace(state, selection=replace(current, anchor=anchor))


def resolved_range(state: TerminalViewState, text: str) -> SelectionRange:
    current = state.selection
    return normalize_range(current.anchor, current.focus, text)


def mark_copied(state: TerminalViewState, value: str) -> TerminalViewState:
    current = state.selection
    return replace(state, selection=replace(current, last_copied=value, error=None))


def set_selection_error(state: TerminalViewState, message: str) -> TerminalViewState:
    current = state.selection
    return replace(state, selection=replace(current, error=message))
