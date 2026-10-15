"""Mock design tokens (dark acceptance target).

Exact values come from the design. This module is pure data plus tiny
pure helpers (status ordering, initials, width classification) so the application
can reuse the same rules when mapping real models.

Deliberate deviations from the HTML, all required by the adoption
assignment (MOCK_ADOPTION_ASSIGNMENT.md, AI working
material kept outside the tracked tree):
- 48px minimum hit regions everywhere (mock draws 44px buttons).
- Phone top spacing uses the real SafeArea/insets, never the mock's
  fixed 48px top pad or a drawn status bar.
- Idle/Unknown have no full mock: Idle uses the design's dark card
  with a muted filled pill; Unknown is visibly unknown (muted outline
  pill, "Unknown" word, sorted last).
- Selected-card 3px light outline is drawn with a zero-blur spread
  shadow so layout dimensions do not change (Flet has no
  outline-offset); visually adjacent rather than 2px offset.
- Icons are Material glyphs (Flet has no inline SVG text spans);
  glyph shapes differ from the mock SVGs, placement/sizing match.
"""

from __future__ import annotations

from typing import Any

from relaydeck.client import theme as _theme

# Adapter: every colour below resolves from client/theme.toml via
# client.theme (the single source). The dependency is one-way:
# theme no longer imports this module, so this import cannot cycle.
_CONSOLE = _theme.CONSOLE
_SCALES = _theme.CONSOLE_SCALES

# -- page, text, actions, status (values in theme.toml [console]) -------
GROUND = _CONSOLE["ground"]
DOT = _CONSOLE["dot"]
CARD = _CONSOLE["card"]
RAISED = _CONSOLE["raised"]
PANEL = _CONSOLE["panel"]
NAV = _CONSOLE["nav"]
TERMINAL_BG = _CONSOLE["terminal_bg"]
BORDER = _CONSOLE["border"]
BORDER_DASHED_OFFLINE = _CONSOLE["border_dashed_offline"]
TEXT = _CONSOLE["text"]
TEXT_SECONDARY = _CONSOLE["text_secondary"]
TEXT_MUTED = _CONSOLE["text_muted"]
TEXT_TERMINAL_MUTED = _CONSOLE["text_terminal_muted"]
PRIMARY_FILL = _CONSOLE["primary_fill"]
PRIMARY_TEXT = _CONSOLE["primary_text"]
PRIMARY_TEXT_LIGHT = _CONSOLE["primary_text_light"]
BLOCKED = _CONSOLE["blocked"]
BLOCKED_TEXT = _CONSOLE["blocked_text"]
BLOCKED_SMALL_TEXT = _CONSOLE["blocked_small_text"]
BLOCKED_PILL_BG = _CONSOLE["blocked_pill_bg"]
BLOCKED_PILL_TEXT = _CONSOLE["blocked_pill_text"]
WORKING = _CONSOLE["working"]
WORKING_TEXT = _CONSOLE["working_text"]
DONE = _CONSOLE["done"]
DONE_TEXT = _CONSOLE["done_text"]
IDLE_FILL = _CONSOLE["idle_fill"]
IDLE_TEXT = _CONSOLE["idle_text"]
UNKNOWN_TEXT = _CONSOLE["unknown_text"]
# Roles that replaced scattered literals (values in theme.toml [console]).
FOCUS_WASH = _CONSOLE["focus_wash"]
ON_PRIMARY = _CONSOLE["on_primary"]
CHIP_INK = _CONSOLE["chip_ink"]
LIVE_INK = _CONSOLE["live_ink"]
TERMINAL_TEXT = _CONSOLE["terminal_text"]
STATUS_RULE = _CONSOLE["status_rule"]
PRESENCE_WARN = _CONSOLE["presence_warn"]
SCRIM_GRADIENT_START = _CONSOLE["scrim_gradient_start"]
LIGHT_INK = _CONSOLE["light_ink"]
LIGHT_SURFACE = _CONSOLE["light_surface"]
SCROLL_THUMB_BG = _CONSOLE["scroll_thumb_bg"]
SHELL_DISABLED = _CONSOLE["shell_disabled"]

STATUS_BLOCKED = "blocked"
STATUS_WORKING = "working"
STATUS_DONE = "done"
STATUS_IDLE = "idle"
STATUS_UNKNOWN = "unknown"

STATUS_LABELS = {
    STATUS_BLOCKED: "Blocked",
    STATUS_WORKING: "Working",
    STATUS_DONE: "Done",
    STATUS_IDLE: "Idle",
    STATUS_UNKNOWN: "Unknown",
}

STATUS_RANK = {
    STATUS_BLOCKED: 0,
    STATUS_WORKING: 1,
    STATUS_DONE: 2,
    STATUS_IDLE: 3,
    STATUS_UNKNOWN: 4,
}

# -- shape -------------------------------------------------------------------
SURFACE_RADIUS = _theme.SURFACE_RADIUS

RADIUS_CARD = _SCALES["radius_card"]
RADIUS_TILE = _SCALES["radius_tile"]
RADIUS_PANE = _SCALES["radius_pane"]
RADIUS_TERMINAL = _SCALES["radius_terminal"]
RADIUS_KEY = _SCALES["radius_key"]
SELECT_OUTLINE_WIDTH = _SCALES["select_outline_width"]

# -- spacing ------------------------------------------------------------------
PAD_PHONE = _SCALES["pad_phone"]
PAD_WEB = _SCALES["pad_web"]
GAP_CARD = _SCALES["gap_card"]
GAP_SECTION = _SCALES["gap_section"]
GAP_CHIP = _SCALES["gap_chip"]
CARD_PAD_V = _SCALES["card_pad_v"]
CARD_PAD_H = _SCALES["card_pad_h"]
PANE_PAD = _SCALES["pane_pad"]
PANE_GAP = _SCALES["pane_gap"]

# -- touch targets ------------------------------------------------------------
# Canonical 48px hit-region floor, defined here (not in client.theme)
# so this package has no downward dependency on the app theme module
# (theme imports these tokens; the reverse import cycled).
MIN_TOUCH = _theme.MIN_TOUCH

# -- responsive shell  ---------------------------
BREAKPOINT_RAIL = 600  # <600: bottom nav; >=600: side rail
BREAKPOINT_SPLIT = 1100  # >=1100: rail + list + live detail
RAIL_WIDTH = 96
LIST_WIDTH = 380
COLLAPSED_WIDTH = 80
MIN_DETAIL_WIDTH = 320

LAYOUT_STACKED = "stacked"  # one screen at a time (phone + mid widths)
LAYOUT_SPLIT = "split"  # list + live detail side by side


def layout_class(width: float) -> str:
    """One component set at all widths; only the shell arrangement changes."""
    return LAYOUT_SPLIT if width >= BREAKPOINT_SPLIT else LAYOUT_STACKED


def has_rail(width: float) -> bool:
    """Side rail at >=600, bottom navigation below."""
    return width >= BREAKPOINT_RAIL


# -- type scale ---------------------------------------------------------------
TYPE_TITLE = dict(_theme.TYPE_SCALES["type_title"])
TYPE_AGENT_TITLE = dict(_theme.TYPE_SCALES["type_agent_title"])
TYPE_CARD_TITLE = dict(_theme.TYPE_SCALES["type_card_title"])
TYPE_CARD_LINE = dict(_theme.TYPE_SCALES["type_card_line"])
TYPE_CARD_LINE_STRONG = dict(_theme.TYPE_SCALES["type_card_line_strong"])
TYPE_META = dict(_theme.TYPE_SCALES["type_meta"])
TYPE_EYEBROW = dict(_theme.TYPE_SCALES["type_eyebrow"])
TYPE_NAV_LABEL = dict(_theme.TYPE_SCALES["type_nav_label"])
TYPE_NAV_LABEL_ACTIVE = dict(_theme.TYPE_SCALES["type_nav_label_active"])
TYPE_BUTTON = dict(_theme.TYPE_SCALES["type_button"])
TYPE_BUTTON_IDLE = dict(_theme.TYPE_SCALES["type_button_idle"])
TYPE_PILL = dict(_theme.TYPE_SCALES["type_pill"])
TYPE_COUNT_NUMBER = dict(_theme.TYPE_SCALES["type_count_number"])
TYPE_COUNT_LABEL = dict(_theme.TYPE_SCALES["type_count_label"])
TYPE_TILE_INITIALS = dict(_theme.TYPE_SCALES["type_tile_initials"])


def status_rank(status: str) -> int:
    """Blocked -> Working -> Done -> Idle, Unknown visibly last.

    Unrecognized values sort with Unknown (never implied live).
    """
    return STATUS_RANK.get(status, STATUS_RANK[STATUS_UNKNOWN])


def sort_keys(items: list[Any], status_of: Any) -> list[Any]:
    """Stable console ordering: status rank first, input order within a group.

    Python's sort is stable, so equal ranks keep their original order.
    """
    return sorted(items, key=lambda item: status_rank(status_of(item)))


def initials(name: str) -> str:
    """Two-letter collapsed-tile initials from a display name.

    First two characters, uppercased (mock: fx-rounding-fix -> FX,
    onboarding-copy -> ON). Empty names show "??".
    """
    return (name[:2] or "??").upper()


# -- Usage screen ---------------------------------------------------------
# Usage-scoped tokens only: no existing token above is recoloured, and no other screen reads these.
# The light set's text pairs are chosen so that
# every text pair in it is checked to clear 4.5:1 (tests/ui/test_usage_redesign_tokens.py).
USAGE_DARK: dict[str, str] = dict(_theme.USAGE_DARK)
USAGE_LIGHT: dict[str, str] = dict(_theme.USAGE_LIGHT)


def usage_palette(dark: bool = True) -> dict[str, str]:
    """A copy of the Usage token set for the theme, so callers cannot mutate the shared table."""
    return dict(USAGE_DARK if dark else USAGE_LIGHT)
