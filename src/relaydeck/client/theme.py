"""RelayDeck application tokens, resolved once at import from theme.toml.

theme.toml (next to this module) is the single source of the app's look:
palette roles, status/banner colours, agent identity colours, the design
system (console) constants, usage palettes, scales and type sizes. The
file is parsed and validated once here at import (unknown or missing
key, non-hex colour: ValueError); palette() never re-parses.

Seasonal themes are overlays in [themes]: each may replace only decorative
colours (grounds, surfaces, borders, accent and primary fills), never status,
text or terminal colours. One theme applies to the whole process, chosen at
import from [themes] active ("auto" follows each overlay's date window) or the
RELAYDECK_THEME environment variable, because screens read these tokens as
module constants.
"""

from __future__ import annotations

import copy
import os
import re
import tomllib
import zlib
from dataclasses import dataclass
from datetime import date
from importlib.resources import files
from types import MappingProxyType
from typing import Any

_HEX = re.compile(r"^#[0-9A-Fa-f]{6}([0-9A-Fa-f]{2})?$")
_PALETTE_ROLES = frozenset(
    {
        "bg",
        "surface",
        "raised",
        "line",
        "outline",
        "text",
        "muted",
        "accent",
        "on_accent",
        "accent_text",
        "teal",
        "amber",
        "danger",
        "success",
        "disabled_text",
        "error_bg",
        "rail_bg",
        "active_row_bg",
        "active_row_border",
        "terminal_bg",
        "terminal_text",
        "terminal_muted",
        "terminal_attention",
    }
)
_CONSOLE_KEYS = frozenset(
    {
        "ground",
        "dot",
        "card",
        "card_selected",
        "raised",
        "panel",
        "nav",
        "terminal_bg",
        "terminal_canvas",
        "border",
        "border_dashed_offline",
        "text",
        "text_secondary",
        "text_muted",
        "text_terminal_muted",
        "primary_fill",
        "primary_text",
        "primary_text_light",
        "blocked",
        "blocked_text",
        "blocked_small_text",
        "blocked_pill_bg",
        "blocked_pill_text",
        "working",
        "working_text",
        "done",
        "done_text",
        "idle_fill",
        "idle_text",
        "unknown_text",
        "focus_wash",
        "on_primary",
        "chip_ink",
        "live_ink",
        "terminal_text",
        "status_rule",
        "presence_warn",
        "scrim_gradient_start",
        "light_ink",
        "light_surface",
        "scroll_thumb_bg",
        "shell_disabled",
        "toast_action",
    }
)
_USAGE_KEYS = frozenset(
    {
        "bg",
        "bg_deep",
        "surface",
        "surface_sheet",
        "surface_raised",
        "surface_accent",
        "nav",
        "border",
        "border_strong",
        "text",
        "text_2",
        "text_muted",
        "text_faint",
        "accent",
        "accent_on",
        "link",
        "link_hover",
        "series_1",
        "series_1_dim",
        "series_2",
        "series_other",
        "warn",
        "warn_bg",
        "danger",
        "danger_bg",
    }
)


def _fail(where: str, problem: str) -> ValueError:
    return ValueError(f"theme.toml [{where}]: {problem}")


def _table(doc: dict[str, Any], *path: str) -> dict[str, Any]:
    node: Any = doc
    for part in path:
        if not isinstance(node, dict) or part not in node:
            raise _fail(".".join(path), "missing section")
        node = node[part]
    if not isinstance(node, dict):
        raise _fail(".".join(path), "section is not a table")
    return node


def _colours(table: dict[str, Any], keys: frozenset[str], where: str) -> MappingProxyType[str, str]:
    if frozenset(table) != keys:
        missing = sorted(keys - frozenset(table))
        extra = sorted(frozenset(table) - keys)
        raise _fail(where, f"key mismatch missing={missing} unknown={extra}")
    for key, value in table.items():
        if not isinstance(value, str) or _HEX.fullmatch(value) is None:
            raise _fail(where, f"{key} is not a hex colour: {value!r}")
    return MappingProxyType(dict(table))


# What an overlay may replace: backgrounds, surfaces, borders and brand fills. Text, status and
# terminal colours stay fixed so meaning and readability never depend on the season.
_OVERLAY_CONSOLE = frozenset(
    {
        "ground",
        "dot",
        "card",
        "card_selected",
        "raised",
        "panel",
        "nav",
        "border",
        "status_rule",
        "primary_fill",
        "primary_text",
        "primary_text_light",
    }
)
_OVERLAY_PALETTE = frozenset(
    {
        "bg",
        "surface",
        "raised",
        "line",
        "accent",
        "on_accent",
        "accent_text",
        "rail_bg",
        "active_row_bg",
    }
)
_MONTH_DAY = re.compile(r"^(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])$")
DEFAULT_THEME = "default"


def _overlay_colours(table: Any, allowed: frozenset[str], where: str) -> dict[str, str]:
    if not isinstance(table, dict):
        raise _fail(where, "section is not a table")
    unknown = sorted(frozenset(table) - allowed)
    if unknown:
        raise _fail(where, f"an overlay may not replace {unknown}")
    for key, value in table.items():
        if not isinstance(value, str) or _HEX.fullmatch(value) is None:
            raise _fail(where, f"{key} is not a hex colour: {value!r}")
    return dict(table)


def _overlays(doc: dict[str, Any]) -> dict[str, dict[str, Any]]:
    themes = _table(doc, "themes")
    active = themes.get("active", "auto")
    found: dict[str, dict[str, Any]] = {}
    for name, body in themes.items():
        if name == "active":
            continue
        where = f"themes.{name}"
        if name in (DEFAULT_THEME, "auto") or not isinstance(body, dict):
            raise _fail(where, "overlay names must be tables other than default/auto")
        if frozenset(body) - {"window", "console", "palette"}:
            raise _fail(where, "an overlay holds only window, console and palette")
        window = body.get("window")
        if window is not None and (
            not isinstance(window, list)
            or len(window) != 2
            or not all(isinstance(day, str) and _MONTH_DAY.fullmatch(day) for day in window)
        ):
            raise _fail(where, 'window must be ["MM-DD", "MM-DD"]')
        palettes = body.get("palette", {})
        if not isinstance(palettes, dict) or frozenset(palettes) - {"dark", "light"}:
            raise _fail(where, "palette holds only dark and light")
        found[name] = {
            "window": window,
            "console": _overlay_colours(body.get("console", {}), _OVERLAY_CONSOLE, where),
            "palette": {
                mode: _overlay_colours(table, _OVERLAY_PALETTE, f"{where}.palette.{mode}")
                for mode, table in palettes.items()
            },
        }
    if active not in ("auto", DEFAULT_THEME, *found):
        raise _fail("themes", f"active {active!r} is not auto, default or an overlay name")
    return found


def _in_window(window: list[str] | None, today: date) -> bool:
    if window is None:
        return False
    start, end = window
    mark = today.strftime("%m-%d")
    # A window may wrap the new year (12-15 to 01-02).
    return start <= mark <= end if start <= end else mark >= start or mark <= end


def resolve_theme_name(choice: str, overlays: dict[str, dict[str, Any]], today: date) -> str:
    """An explicit overlay name, or under "auto" the first overlay whose window holds today."""
    if choice == "auto":
        for name, body in overlays.items():
            if _in_window(body["window"], today):
                return name
        return DEFAULT_THEME
    return choice if choice in overlays else DEFAULT_THEME


def apply_overlay(doc: dict[str, Any], overlay: dict[str, Any] | None) -> dict[str, Any]:
    """A copy of the theme document with the overlay's colours merged in."""
    merged = copy.deepcopy(doc)
    if overlay is None:
        return merged
    merged["console"].update(overlay["console"])
    for mode, table in overlay["palette"].items():
        merged["palette"][mode].update(table)
    return merged


def _load() -> dict[str, Any]:
    raw = files("relaydeck.client").joinpath("theme.toml").read_bytes()
    try:
        doc = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"theme.toml: unparseable ({exc})") from exc
    expected = {"meta", "palette", "status", "agent", "console", "scales", "type", "a11y", "themes"}
    if frozenset(doc) != expected:
        raise _fail("top level", f"sections {sorted(doc)} != {sorted(expected)}")
    if doc["meta"] != {"version": 1}:
        raise _fail("meta", "only version = 1 is supported")
    return doc


_BASE_DOC = _load()
OVERLAYS = MappingProxyType(_overlays(_BASE_DOC))
ACTIVE_THEME = resolve_theme_name(
    os.environ.get("RELAYDECK_THEME") or str(_table(_BASE_DOC, "themes").get("active", "auto")),
    dict(OVERLAYS),
    date.today(),
)
_DOC = apply_overlay(_BASE_DOC, OVERLAYS.get(ACTIVE_THEME))
_DARK_MAP = _colours(_table(_DOC, "palette", "dark"), _PALETTE_ROLES, "palette.dark")
_LIGHT_MAP = _colours(_table(_DOC, "palette", "light"), _PALETTE_ROLES, "palette.light")
_STATUS = _table(_DOC, "status")
CONSOLE = _colours(
    {k: v for k, v in _table(_DOC, "console").items() if k != "usage"},
    _CONSOLE_KEYS,
    "console",
)
USAGE_DARK = _colours(_table(_DOC, "console", "usage", "dark"), _USAGE_KEYS, "console.usage.dark")
USAGE_LIGHT = _colours(
    _table(_DOC, "console", "usage", "light"), _USAGE_KEYS, "console.usage.light"
)
_SCALES = _table(_DOC, "scales")


def _int_tuple(key: str) -> tuple[int, ...]:
    value = _SCALES.get(key)
    if not isinstance(value, list) or not value or not all(type(v) is int for v in value):
        raise _fail("scales", f"{key} must be a non-empty list of ints")
    return tuple(value)


SPACING: tuple[int, ...] = _int_tuple("spacing")
RADIUS: tuple[int, ...] = _int_tuple("radius")
if type(_SCALES.get("min_touch")) is not int:
    raise _fail("scales", "min_touch must be an int")
MIN_TOUCH: int = _SCALES["min_touch"]
CONSOLE_SCALES = MappingProxyType(dict(_table(_DOC, "scales", "console")))
_TYPE = _table(_DOC, "type")
if not all(type(v) is int for v in _TYPE["font_size"].values()):
    raise _fail("type.font_size", "sizes must be ints")
TYPE_SCALES = MappingProxyType(
    {name: MappingProxyType(dict(t)) for name, t in _TYPE.items() if name != "font_size"}
)
_A11Y = _table(_DOC, "a11y")
CONTRAST_SURFACE_LIGHT_REF: str = _A11Y["surface_light_ref"]
CONTRAST_SURFACE_DARK_REF: str = _A11Y["surface_dark_ref"]
for _ref in (CONTRAST_SURFACE_LIGHT_REF, CONTRAST_SURFACE_DARK_REF):
    if not isinstance(_ref, str) or _HEX.fullmatch(_ref) is None:
        raise _fail("a11y", f"reference is not a hex colour: {_ref!r}")
ACCENT: str = _LIGHT_MAP["accent"]
PHONE_MAX_WIDTH = 599
WIDE_BREAKPOINT = 1100
COMPACT_MAX_WIDTH = WIDE_BREAKPOINT - 1
RAIL_WIDTH = 96
RAIL_WIDTH_COMPACT = 96
LIST_WIDTH = 380
LIST_WIDTH_COMPACT = 380
SHEET_MAX_WIDTH = 520
SHEET_MIN_WIDTH = 280
SCRIM_OPACITY = 0.5
PHONE = "phone"
COMPACT = "compact"
DESKTOP = "desktop"


def layout_mode(width: float) -> str:
    """Shared route/presentation classification in logical pixels."""
    if width <= PHONE_MAX_WIDTH:
        return PHONE
    if width < WIDE_BREAKPOINT:
        return COMPACT
    return DESKTOP


# Legacy graphic-only tokens retained for isolated terminal notices. Chrome
# uses palette-aware foregrounds below; every status also carries a word.
STATUS_COLORS: dict[str, str] = dict(_STATUS["colors"])
STATUS_LABELS = {
    "idle": "Idle",
    "working": "Working",
    "blocked": "Blocked",
    "done": "Done",
    "unknown": "Unknown",
}
BANNER_TONES: dict[str, str] = dict(_STATUS["banner_tones"])


DARK: dict[str, str] = dict(_DARK_MAP)
LIGHT: dict[str, str] = dict(_LIGHT_MAP)
THEMES = {"dark": DARK, "light": LIGHT}


def palette(dark: bool = True) -> dict[str, str]:
    """Copy the selected table so callers cannot mutate shared tokens."""
    return dict(DARK if dark else LIGHT)


def status_color(status: str, dark: bool = True) -> str:
    tokens = palette(dark)
    role = {"working": "accent_text", "blocked": "amber", "done": "success"}.get(status, "muted")
    return tokens[role]


@dataclass(frozen=True)
class AgentIdentity:
    label: str
    color: str


# a neutral palette of (dark colour, light colour, light ink) triples. A
# kind's colour is picked by a stable hash of its name, so no kind name lives in code.
# Light inks are darkened to clear 4.5:1 against the 16% identity wash composited over
# every tile state (normal/selected/hover). Dark theme uses the identity colour itself.
_AGENT_TABLE = _table(_DOC, "agent")
_AGENT_PALETTE: tuple[tuple[str, str, str], ...] = tuple(
    (str(a), str(b), str(c)) for a, b, c in _AGENT_TABLE["triples"]
)
_AGENT_FALLBACK_DARK: str = _AGENT_TABLE["fallback_dark"]
_AGENT_FALLBACK_LIGHT: str = _AGENT_TABLE["fallback_light"]
_AGENT_LIGHT_INK_FALLBACK: str = _AGENT_TABLE["light_ink_fallback"]
_SPACE_TINTS: tuple[str, ...] = tuple(str(tint) for tint in _AGENT_TABLE["space_tints"])
if not _SPACE_TINTS:
    raise _fail("agent", "space_tints must not be empty")


def space_tint(label: str) -> str:
    """Stable chip colour for a space name: the same space always gets the same tint."""
    index = zlib.crc32(label.strip().casefold().encode("utf-8")) % len(_SPACE_TINTS)
    return _SPACE_TINTS[index]


def _palette_entry(kind: str) -> tuple[str, str, str]:
    """Deterministic colour triple for a non-empty kind (stable across runs)."""
    index = zlib.crc32(kind.strip().casefold().encode("utf-8")) % len(_AGENT_PALETTE)
    return _AGENT_PALETTE[index]


def agent_identity(kind: str, dark: bool) -> AgentIdentity:
    """Identity color for a run card: label + theme ink.

    Case-insensitive; unknown/empty kinds fall back to a neutral ink with
    the capitalized kind (or "Shell"). Every color clears 4.5:1 against
    the card background token in its theme (see test_agent_identity).
    """
    name = (kind or "").strip()
    if not name:
        return AgentIdentity("Shell", _AGENT_FALLBACK_DARK if dark else _AGENT_FALLBACK_LIGHT)
    dark_color, light_color, _ink = _palette_entry(name)
    return AgentIdentity(name.capitalize(), dark_color if dark else light_color)


def agent_badge_ink(kind: str, dark: bool) -> str:
    """Label ink for the identity pill (contrast fix).

    The stripe and wash keep the identity colour, but the 11px label sits
    on the 16% wash composited over the tile — not on the bare card — so
    the light theme needs a darker ink to clear 4.5:1 on normal, selected
    and hover tiles alike. Dark-theme identity inks already pass on the
    composited wash and are returned unchanged.
    """
    if dark:
        return agent_identity(kind, True).color
    name = (kind or "").strip()
    if not name:
        return _AGENT_LIGHT_INK_FALLBACK
    return _palette_entry(name)[2]


def agent_subtitle_rest(subtitle: str, agent: str) -> str:
    """Subtitle remainder after the leading agent word.

    Selectors compose "agent · project · activity"; the card renders the
    agent as a pill and this remainder as muted text. Anything that does
    not start with the agent word is returned whole (defensive).
    """
    if agent and subtitle == agent:
        return ""
    prefix = f"{agent} · " if agent else ""
    if prefix and subtitle.startswith(prefix):
        return subtitle[len(prefix) :]
    return subtitle


# Foreground/background pairs actually used by the chrome and terminal.
CONTRAST_PAIRS: tuple[tuple[str, str, str, float], ...] = (
    ("text/bg", "text", "bg", 4.5),
    ("text/surface", "text", "surface", 4.5),
    ("text/raised", "text", "raised", 4.5),
    ("text/selected", "text", "active_row_bg", 4.5),
    ("muted/bg", "muted", "bg", 4.5),
    ("muted/surface", "muted", "surface", 4.5),
    ("muted/raised", "muted", "raised", 4.5),
    ("muted/selected", "muted", "active_row_bg", 4.5),
    ("on_accent/accent", "on_accent", "accent", 4.5),
    ("accent/surface", "accent_text", "surface", 4.5),
    ("accent/raised", "accent_text", "raised", 4.5),
    ("accent/selected", "accent_text", "active_row_bg", 4.5),
    ("amber/surface", "amber", "surface", 4.5),
    ("amber/selected", "amber", "active_row_bg", 4.5),
    ("success/surface", "success", "surface", 4.5),
    ("success/selected", "success", "active_row_bg", 4.5),
    ("danger/error", "danger", "error_bg", 4.5),
    ("disabled/raised", "disabled_text", "raised", 4.5),
    ("outline/surface", "outline", "surface", 3.0),
    ("outline/raised", "outline", "raised", 3.0),
    ("focus/surface", "accent_text", "surface", 3.0),
    ("focus/raised", "accent_text", "raised", 3.0),
    ("terminal/text", "terminal_text", "terminal_bg", 4.5),
    ("terminal/muted", "terminal_muted", "terminal_bg", 4.5),
    ("terminal/attention", "terminal_attention", "terminal_bg", 4.5),
)

FONT_SIZE: dict[str, int] = dict(_TYPE["font_size"])
MONO_STACK = ("ui-monospace", "SFMono-Regular", "Consolas", "monospace")
MOTION_HOVER_MS = 140
MOTION_MODAL_MS = 160
FOCUS_RING_WIDTH = 2
FOCUS_RING_OFFSET = 2
SURFACE_RADIUS = 8
# Glyph fallback for UI text: the bundled faces lack the dingbats, shapes and braille agents emit.
# Styles pass list(TEXT_FONT_FALLBACK) as font_family_fallback. Deferred import: design_system
# imports tokens, which imports this module (see tokens.py).
from relaydeck.client.components.design_system.fonts import FAMILY_SYMBOLS  # noqa: E402

TEXT_FONT_FALLBACK: tuple[str, ...] = (FAMILY_SYMBOLS, "Noto Sans Symbols")
