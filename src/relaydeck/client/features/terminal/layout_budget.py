"""Pure terminal-priority layout budget for the agent detail panel.

Inputs are the measured detail-panel height and the terminal font size;
no Flet. The terminal takes priority over input chrome:

* keyboard closed: the terminal keeps at least 12 rows; the dock yields
  (shrinks, then becomes an overlay sheet above the key row);
* keyboard open (short visible height): only header, terminal and composer
  stay in the column; key row, dock, picture and status collapse behind
  the "Keys" toggle (the dock is then an overlay). The terminal gets every
  remaining row (at least 6 when physically possible).

Chrome heights are layout estimates of the current components; the row
height follows the engine's line metrics (font size x ROW_FACTOR).
"""

from __future__ import annotations

from dataclasses import dataclass

from relaydeck.client.theme import WIDE_BREAKPOINT

KEYBOARD_OPEN_BELOW = 600.0
ROW_FACTOR = 16.0 / 14.0  # observed engine line height at the default font
# Terminal host box = rows x row height + top padding + status line.
HOST_OVERHEAD = 44.0
DEFAULT_FONT_SIZE = 14.0
MIN_ROWS_CLOSED = 12
MIN_ROWS_KEYBOARD = 6
HEADER_HEIGHT = 90.0
KEY_ROW_HEIGHT = 116.0
COMPOSER_HEIGHT = 130.0
PICTURE_HEIGHT = 56.0
PANEL_SPACING = 12.0
DOCK_MIN_INLINE = 120.0
DOCK_MAX_FRACTION = 0.40
DOCK_CONTENT_HEIGHT = 260.0  # switch + two wrapped key rows + dismiss + padding
OVERLAY_MAX_FRACTION = 0.60
OVERLAY_MAX_HEIGHT = 360.0
OVERLAY_TOP_MARGIN = 8.0
# Overlay sits above the key row and composer that stay in the column.
OVERLAY_BOTTOM_NORMAL = COMPOSER_HEIGHT + KEY_ROW_HEIGHT + 2 * PANEL_SPACING
OVERLAY_BOTTOM_KEYBOARD = COMPOSER_HEIGHT + PANEL_SPACING


# Landscape tablet typing mode (terminal | input column).
TYPING_SPLIT_GAP = 12.0
TYPING_SPLIT_INPUT_SHARE = 0.30
TYPING_SPLIT_HEADER_HEIGHT = 28.0  # slim header: title + status dot + more
TYPING_SPLIT_FALLBACK_WIDTH = 800.0
PHONE_DOCK_HEIGHT = 84.0  # tab row + one-line composer, until measured
PHONE_FALLBACK_WIDTH = 390.0
TYPING_SPLIT_DOCK_FRACTION = 0.34
WINDOW_PADDING = 20.0  # WindowFrame pane padding


def typing_split_active(
    *,
    focused: bool,
    viewport_width: float,
    viewport_height: float,
    touch: bool | None = None,
) -> bool:
    """Composer focused in landscape (the typing mode). `touch` is the real device class
    (page.platform iOS/Android or a desktop-mode iPad): True = any touch device, any width
    (iPad, landscape phones); False = never (a desktop browser is not a keyboard); None =
    size-only rule (desktop breakpoint, short window), kept for callers that do not know
    the platform.

    Touch devices use focus because browsers may not report a reduced height
    while the keyboard is open. Use viewport width rather than detail width:
    hiding the list column must not feed back into this decision.
    """
    if not focused or viewport_width <= viewport_height:
        return False
    if touch is None:
        return viewport_width >= WIDE_BREAKPOINT and viewport_height < KEYBOARD_OPEN_BELOW
    return touch


def typing_split_widths(
    detail_width: float, *, padding: float = WINDOW_PADDING, gap: float = TYPING_SPLIT_GAP
) -> tuple[float, float]:
    """(terminal, input) px: an exact 70/30 split of the usable width."""
    usable = max(0.0, (detail_width or TYPING_SPLIT_FALLBACK_WIDTH) - 2 * padding)
    usable = max(0.0, usable - gap)
    side = round(usable * TYPING_SPLIT_INPUT_SHARE)
    return usable - side, float(side)


FIELD_LINE_HEIGHT = 24.0  # 16 px draft text, Flutter's ~1.4 line box
FIELD_MAX_LINES = 40


def typing_split_lines(column_height: float, chrome: float) -> int:
    """Visible draft lines that fit a column of `column_height` after
    `chrome` px of other controls (rounded down, so an estimate that is a
    little high leaves a gap instead of overflowing); at least 2."""
    lines = int((column_height - chrome) // FIELD_LINE_HEIGHT)
    return max(2, min(FIELD_MAX_LINES, lines))


@dataclass(frozen=True)
class DetailBudget:
    keyboard_open: bool
    collapse: bool  # key row, status, dock and picture hidden behind "Keys"
    dock_inline: float  # inline dock height (0 = none)
    dock_overlay: float  # overlay sheet height (0 = none)
    overlay_bottom: float
    terminal_rows: float  # estimated rows left for the terminal


def row_height(font_size: float | None, measured: float | None = None) -> float:
    """Measured px per row (host height / engine rows) beats the estimate."""
    return measured or (font_size or DEFAULT_FONT_SIZE) * ROW_FACTOR


def measured_row_height(host_height: float, rows: int) -> float | None:
    """Row px from the terminal host box and the engine's size report.

    The host box includes padding and the status line (HOST_OVERHEAD), which
    is removed so the value does not drift with the row count. Needs a
    plausible report (>= 4 rows)."""
    if rows < 4 or host_height <= HOST_OVERHEAD:
        return None
    return (host_height - HOST_OVERHEAD) / rows


OVERLAY_MIN_USEFUL = 96.0


def _overlay(detail_height: float, bottom: float, row: float) -> tuple[float, float]:
    """(height, bottom) of an overlay that never covers the header or the
    top MIN_ROWS_KEYBOARD terminal rows. If it cannot fit above its anchor
    it sits over the composer area (anchor 0) instead."""
    top_limit = HEADER_HEIGHT + PANEL_SPACING + MIN_ROWS_KEYBOARD * row
    bottom = max(0.0, min(bottom, detail_height - OVERLAY_TOP_MARGIN))
    room = detail_height - bottom - top_limit
    if room < OVERLAY_MIN_USEFUL:
        bottom, room = 0.0, detail_height - top_limit
    height = min(OVERLAY_MAX_FRACTION * detail_height, OVERLAY_MAX_HEIGHT, room)
    return max(0.0, height), bottom


def plan_detail(
    *,
    detail_height: float,
    viewport_height: float,
    font_size: float | None,
    dock_open: bool,
    picture_visible: bool = False,
    status_visible: bool = True,
    measured_row: float | None = None,
) -> DetailBudget:
    row = row_height(font_size, measured_row)
    picture = PICTURE_HEIGHT if picture_visible else 0.0
    full_chrome = HEADER_HEIGHT + KEY_ROW_HEIGHT + COMPOSER_HEIGHT + picture + 4 * PANEL_SPACING
    full_rows = (detail_height - full_chrome) / row
    keyboard_open = viewport_height < KEYBOARD_OPEN_BELOW
    # Secondary chrome yields before the terminal: with the keyboard open,
    # or whenever the full chrome would leave fewer than 12 rows.
    if keyboard_open or full_rows < MIN_ROWS_CLOSED:
        left = detail_height - HEADER_HEIGHT - COMPOSER_HEIGHT - 2 * PANEL_SPACING
        height, bottom = (
            _overlay(detail_height, OVERLAY_BOTTOM_KEYBOARD, row) if dock_open else (0.0, 0.0)
        )
        return DetailBudget(
            keyboard_open=keyboard_open,
            collapse=True,
            dock_inline=0.0,
            dock_overlay=height,
            overlay_bottom=bottom,
            terminal_rows=max(0.0, left) / row,
        )
    left = detail_height - full_chrome
    if not dock_open:
        return DetailBudget(False, False, 0.0, 0.0, 0.0, max(0.0, left) / row)
    room = left - MIN_ROWS_CLOSED * row
    if room >= DOCK_MIN_INLINE:
        dock = min(room, DOCK_CONTENT_HEIGHT, DOCK_MAX_FRACTION * detail_height)
        return DetailBudget(False, False, 0.0 + dock, 0.0, 0.0, (left - dock) / row)
    height, bottom = _overlay(detail_height, OVERLAY_BOTTOM_NORMAL + picture, row)
    return DetailBudget(False, False, 0.0, height, bottom, max(0.0, left) / row)
