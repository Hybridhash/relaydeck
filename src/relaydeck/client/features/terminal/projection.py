"""Headless VT projection oracle. Flet-free.

A small incremental screen model used ONLY to assert byte-stream
behavior headlessly: ordering, split-sequence buffering, parser-state
preservation, and fixture rendering as expected screen cells/text. It is
deliberately not a full emulator: the display path stays xterm2.
Anything outside the documented subset below is ignored
(never misrendered as content), and the subset is pinned by the VT
fixtures plus these explicit limits:

- colors/SGR tracked nowhere (colors never alter text)
- no scroll regions (DECSTBM ignored), no insert/delete line/char
- no mouse, clipboard, or device-report replies
- unknown CSI/ESC sequences are skipped, never emitted as text
"""

from __future__ import annotations

import codecs
import unicodedata
from dataclasses import dataclass, field

_DEFAULT_COLS = 80
_DEFAULT_ROWS = 24


def _char_width(char: str) -> int:
    if unicodedata.combining(char):
        return 0
    if unicodedata.category(char) == "Cf":
        return 0
    return 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1


@dataclass
class _Screen:
    cols: int
    rows: int
    grid: list[list[str]] = field(init=False)
    wide2: set[tuple[int, int]] = field(default_factory=set)
    row: int = 0
    col: int = 0
    saved_row: int = 0
    saved_col: int = 0
    wrap_pending: bool = False
    last_base: tuple[int, int] | None = None

    def __post_init__(self) -> None:
        self.grid = [["" for _ in range(self.cols)] for _ in range(self.rows)]

    # -- primitives ----------------------------------------------------
    def clear_cell(self, row: int, col: int) -> None:
        if (row, col) in self.wide2:
            self.wide2.discard((row, col))
            if col - 1 >= 0:
                self.grid[row][col - 1] = ""
        elif col + 1 < self.cols and (row, col + 1) in self.wide2:
            self.wide2.discard((row, col + 1))
            self.grid[row][col + 1] = ""
        self.grid[row][col] = ""

    def row_text(self, row: int) -> str:
        cells: list[str] = []
        for col, cell in enumerate(self.grid[row]):
            if (row, col) in self.wide2:
                continue
            cells.append(cell if cell else " ")
        return "".join(cells).rstrip()

    def clear(self) -> None:
        for row in range(self.rows):
            for col in range(self.cols):
                self.grid[row][col] = ""
        self.wide2.clear()
        self.wrap_pending = False
        self.last_base = None


class TerminalProjection:
    """Incremental VT screen model over raw bytes. Split multibyte and
    escape sequences are buffered across feeds, so any chunking of the
    same stream yields the same screen."""

    def __init__(
        self, cols: int = _DEFAULT_COLS, rows: int = _DEFAULT_ROWS, max_scrollback: int = 5000
    ) -> None:
        self.cols = cols
        self.rows = rows
        self.max_scrollback = max_scrollback
        self._main = _Screen(cols, rows)
        self._alt = _Screen(cols, rows)
        self._alt_active = False
        self._bracketed_paste = False
        self._scrollback: list[str] = []
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._text = ""

    # -- observables ----------------------------------------------------
    @property
    def _screen(self) -> _Screen:
        return self._alt if self._alt_active else self._main

    @property
    def alt_active(self) -> bool:
        return self._alt_active

    @property
    def bracketed_paste(self) -> bool:
        return self._bracketed_paste

    @property
    def cursor(self) -> tuple[int, int]:
        return (self._screen.row, self._screen.col)

    @property
    def lines(self) -> list[str]:
        screen = self._screen
        out = [screen.row_text(r) for r in range(self.rows)]
        while out and not out[-1]:
            out.pop()
        return out

    @property
    def scrollback(self) -> list[str]:
        return list(self._scrollback)

    def feed(self, data: bytes) -> None:
        self._text += self._decoder.decode(data)
        self._parse()

    def flush(self) -> None:
        """End of stream: decode remaining bytes and drop a trailing
        incomplete escape sequence (a truncated tail is never presented
        as content)."""
        self._text += self._decoder.decode(b"", final=True)
        self._decoder.reset()
        self._parse(final=True)
        self._text = ""

    # -- incremental parser ----------------------------------------------
    def _parse(self, final: bool = False) -> None:
        text = self._text
        pos = 0
        while pos < len(text):
            char = text[pos]
            if char == "\x1b":
                consumed = self._parse_escape(text, pos, final)
                if consumed is None:
                    break
                pos += consumed
            elif char == "\r":
                screen = self._screen
                screen.wrap_pending = False
                screen.last_base = None
                screen.col = 0
                pos += 1
            elif char in ("\n", "\x0b", "\x0c"):
                self._newline(full=False)
                pos += 1
            elif char == "\t":
                screen = self._screen
                screen.wrap_pending = False
                screen.col = min(self.cols - 1, (screen.col // 8 + 1) * 8)
                pos += 1
            elif char == "\x08":
                screen = self._screen
                screen.wrap_pending = False
                screen.col = max(0, screen.col - 1)
                pos += 1
            elif char < " " or char == "\x7f":
                pos += 1  # BEL and other controls carry no cells
            else:
                self._put(char)
                pos += 1
        self._text = text[pos:]

    def _parse_escape(self, text: str, pos: int, final: bool) -> int | None:
        """Consume one escape sequence at `pos`. None means incomplete:
        hold back for more bytes (or drop when `final`)."""
        if pos + 1 >= len(text):
            return len(text) - pos if final else None
        intro = text[pos + 1]
        if intro == "[":
            return self._parse_csi(text, pos, final)
        if intro == "]":
            return self._parse_st_terminated(text, pos, final)
        if intro in ("P", "_", "^"):
            return self._parse_st_terminated(text, pos, final)
        if intro in ("(", ")", "#"):
            if pos + 2 >= len(text):
                return len(text) - pos if final else None
            return 3
        if intro == "7":
            screen = self._screen
            screen.saved_row, screen.saved_col = screen.row, screen.col
            return 2
        if intro == "8":
            screen = self._screen
            screen.row, screen.col = screen.saved_row, screen.saved_col
            screen.wrap_pending = False
            return 2
        if intro == "M":
            self._reverse_index()
            return 2
        if intro in ("=", ">", "c"):
            return 2
        return 2  # unknown ESC + char: skip, never emit

    def _parse_csi(self, text: str, pos: int, final: bool) -> int | None:
        end = pos + 2
        while end < len(text) and not ("@" <= text[end] <= "~"):
            end += 1
        if end >= len(text):
            return len(text) - pos if final else None
        body = text[pos + 2 : end]
        self._dispatch_csi(body, text[end])
        return end - pos + 1

    def _parse_st_terminated(self, text: str, pos: int, final: bool) -> int | None:
        idx = pos + 2
        while idx < len(text):
            if text[idx] == "\x07":
                return idx - pos + 1
            if text[idx] == "\x1b" and idx + 1 < len(text) and text[idx + 1] == "\\":
                return idx - pos + 2
            if text[idx] == "\x1b" and idx + 1 >= len(text):
                return len(text) - pos if final else None
            idx += 1
        return len(text) - pos if final else None

    # -- CSI dispatch ------------------------------------------------------
    def _dispatch_csi(self, body: str, command: str) -> None:
        private = body.startswith("?")
        if private:
            body = body[1:]
        params = body.split(";") if body else []
        args = [self._param(p) for p in params]

        def arg(index: int, default: int) -> int:
            if index < len(args):
                value = args[index]
                if value is not None:
                    return value
            return default

        screen = self._screen
        if command in ("H", "f"):
            screen.wrap_pending = False
            screen.row = max(0, min(self.rows - 1, arg(0, 1) - 1))
            screen.col = max(0, min(self.cols - 1, arg(1, 1) - 1))
        elif command == "A":
            screen.wrap_pending = False
            screen.row = max(0, screen.row - arg(0, 1))
        elif command == "B":
            screen.wrap_pending = False
            screen.row = min(self.rows - 1, screen.row + arg(0, 1))
        elif command == "C":
            screen.wrap_pending = False
            screen.col = min(self.cols - 1, screen.col + arg(0, 1))
        elif command == "D":
            screen.wrap_pending = False
            screen.col = max(0, screen.col - arg(0, 1))
        elif command == "E":
            screen.wrap_pending = False
            screen.row = min(self.rows - 1, screen.row + arg(0, 1))
            screen.col = 0
        elif command == "F":
            screen.wrap_pending = False
            screen.row = max(0, screen.row - arg(0, 1))
            screen.col = 0
        elif command == "G":
            screen.wrap_pending = False
            screen.col = max(0, min(self.cols - 1, arg(0, 1) - 1))
        elif command == "d":
            screen.wrap_pending = False
            screen.row = max(0, min(self.rows - 1, arg(0, 1) - 1))
        elif command == "J":
            self._erase_display(arg(0, 0))
        elif command == "K":
            self._erase_line(arg(0, 0))
        elif command == "s":
            screen.saved_row, screen.saved_col = screen.row, screen.col
        elif command == "u":
            screen.row, screen.col = screen.saved_row, screen.saved_col
            screen.wrap_pending = False
        elif command in ("h", "l") and private:
            self._set_private(command == "h", args)
        # SGR (m), scroll regions (r), reports (c/n), tabs and vendor
        # sequences are outside the oracle subset: ignored by design.

    @staticmethod
    def _param(raw: str) -> int | None:
        digits = "".join(ch for ch in raw if ch.isdigit())
        if not digits:
            return None
        try:
            return int(digits)
        except ValueError:
            return None

    def _set_private(self, enable: bool, args: list[int | None]) -> None:
        for value in args:
            if value == 1049:
                if enable:
                    main = self._main
                    main.saved_row, main.saved_col = main.row, main.col
                    self._alt.clear()
                    self._alt.row = self._alt.col = 0
                    self._alt_active = True
                else:
                    self._alt_active = False
                    main = self._main
                    main.row, main.col = main.saved_row, main.saved_col
                    main.wrap_pending = False
            elif value == 1047:
                self._alt_active = enable
            elif value == 1048:
                main = self._main
                if enable:
                    main.saved_row, main.saved_col = main.row, main.col
                else:
                    main.row, main.col = main.saved_row, main.saved_col
                    main.wrap_pending = False
            elif value == 2004:
                self._bracketed_paste = enable

    # -- screen operations ---------------------------------------------------
    def _put(self, char: str) -> None:
        screen = self._screen
        width = _char_width(char)
        if width == 0:
            if screen.last_base is not None:
                row, col = screen.last_base
                if (row, col) not in screen.wide2:
                    screen.grid[row][col] += char
                return
            self._ensure_space(screen, 1)
            screen.grid[screen.row][screen.col] += char
            screen.last_base = (screen.row, screen.col)
            screen.col += 1
            if screen.col >= self.cols:
                screen.wrap_pending = True
                screen.col = self.cols - 1
            return
        self._ensure_space(screen, width)
        if width == 2 and screen.col + 1 >= self.cols:
            self._newline(full=True)
            screen = self._screen
        screen.clear_cell(screen.row, screen.col)
        screen.grid[screen.row][screen.col] = char
        screen.last_base = (screen.row, screen.col)
        if width == 2:
            screen.grid[screen.row][screen.col + 1] = ""
            screen.wide2.add((screen.row, screen.col + 1))
            screen.col += 2
        else:
            screen.col += 1
        if screen.col >= self.cols:
            screen.wrap_pending = True
            screen.col = self.cols - 1

    def _ensure_space(self, screen: _Screen, width: int) -> None:
        if screen.wrap_pending:
            self._newline(full=True)
            screen = self._screen
        if width == 1 or screen.col + 1 < self.cols:
            screen.wrap_pending = False

    def _newline(self, full: bool) -> None:
        screen = self._screen
        screen.wrap_pending = False
        screen.last_base = None
        if full:
            screen.col = 0
        if screen.row == self.rows - 1:
            self._scroll()
        else:
            screen.row += 1

    def _scroll(self) -> None:
        screen = self._screen
        if not self._alt_active:
            self._scrollback.append(screen.row_text(0))
            if len(self._scrollback) > self.max_scrollback:
                del self._scrollback[: len(self._scrollback) - self.max_scrollback]
        screen.grid.pop(0)
        screen.grid.append(["" for _ in range(self.cols)])
        kept = {(r - 1, c) for (r, c) in screen.wide2 if r > 0}
        screen.wide2 = kept
        if screen.last_base is not None and screen.last_base[0] == 0:
            screen.last_base = None
        elif screen.last_base is not None:
            screen.last_base = (screen.last_base[0] - 1, screen.last_base[1])

    def _reverse_index(self) -> None:
        screen = self._screen
        screen.wrap_pending = False
        if screen.row == 0:
            screen.grid.pop()
            screen.grid.insert(0, ["" for _ in range(self.cols)])
            screen.wide2 = {(r + 1, c) for (r, c) in screen.wide2 if r + 1 < self.rows}
        else:
            screen.row -= 1

    def _erase_display(self, mode: int) -> None:
        screen = self._screen
        if mode == 2:
            screen.clear()
        elif mode == 3:
            screen.clear()
            self._scrollback.clear()
        elif mode == 1:
            for row in range(screen.row):
                for col in range(self.cols):
                    screen.clear_cell(row, col)
            for col in range(screen.col + 1):
                screen.clear_cell(screen.row, col)
        else:
            for col in range(screen.col, self.cols):
                screen.clear_cell(screen.row, col)
            for row in range(screen.row + 1, self.rows):
                for col in range(self.cols):
                    screen.clear_cell(row, col)

    def _erase_line(self, mode: int) -> None:
        screen = self._screen
        if mode == 2:
            for col in range(self.cols):
                screen.clear_cell(screen.row, col)
        elif mode == 1:
            for col in range(screen.col + 1):
                screen.clear_cell(screen.row, col)
        else:
            for col in range(screen.col, self.cols):
                screen.clear_cell(screen.row, col)
