"""Agent identity colours: palette pick, fallback, and WCAG contrast.

colours come from a neutral palette by a stable hash of the kind, so
these tests use synthetic kinds and check every palette entry.
"""

import zlib

import pytest

from relaydeck.client import theme
from relaydeck.client.components.design_system import tokens as console
from relaydeck.client.features.a11y.contrast import contrast_ratio
from relaydeck.client.theme import (
    LIGHT,
    AgentIdentity,
    agent_badge_ink,
    agent_identity,
    agent_subtitle_rest,
)
from tests.support.companion.setup_sources import CUM_CLI, MSG_CLI

PALETTE = theme._AGENT_PALETTE


def _index(kind: str) -> int:
    return zlib.crc32(kind.casefold().encode("utf-8")) % len(PALETTE)


def _kind_for(index: int) -> str:
    for number in range(10_000):
        name = f"agent-{number}"
        if _index(name) == index:
            return name
    raise AssertionError("no synthetic kind hashes to this palette entry")


KIND_FOR = {index: _kind_for(index) for index in range(len(PALETTE))}

DARK_CARD = console.CARD
LIGHT_TILES = {
    "normal": LIGHT["surface"],
    "selected": LIGHT["active_row_bg"],
    "hover": LIGHT["raised"],
}
BLOCKED_CARD = console.BLOCKED
WASH_ALPHA = 41 / 255  # Flet with_opacity(0.16) rounds to 8-bit alpha 41


def composite(fg: str, bg: str) -> str:
    """sRGB alpha composite of fg wash over bg, as hex."""
    f = tuple(int(fg[i : i + 2], 16) for i in (1, 3, 5))
    b = tuple(int(bg[i : i + 2], 16) for i in (1, 3, 5))
    mixed = tuple(round(f[i] * WASH_ALPHA + b[i] * (1 - WASH_ALPHA)) for i in range(3))
    return "#{:02X}{:02X}{:02X}".format(*mixed)


CASES = tuple(
    (KIND_FOR[index], KIND_FOR[index].capitalize(), dark, light)
    for index, (dark, light, _ink) in enumerate(PALETTE)
)


@pytest.mark.parametrize(("kind", "label", "dark", "light"), CASES)
def test_known_identities(kind: str, label: str, dark: str, light: str) -> None:
    assert agent_identity(kind, True) == AgentIdentity(label, dark)
    assert agent_identity(kind, False) == AgentIdentity(label, light)


@pytest.mark.parametrize(("kind", "label", "dark", "light"), CASES)
def test_case_insensitive(kind: str, label: str, dark: str, light: str) -> None:
    assert agent_identity(kind.upper(), True).color == dark
    assert agent_identity(f"  {kind}  ", False).color == light
    assert agent_identity(f"  {kind}  ", False).label == label


def test_empty_kind_is_the_neutral_shell_identity() -> None:
    assert agent_identity("", True) == AgentIdentity("Shell", "#A7A3C7")
    assert agent_identity("  ", False) == AgentIdentity("Shell", "#5B577A")


def test_any_kind_gets_a_palette_colour_not_a_name_table() -> None:
    palette_dark = {dark for dark, _light, _ink in PALETTE}
    assert agent_identity("mybot", True).color in palette_dark
    assert agent_identity("mybot", True).label == "Mybot"


@pytest.mark.parametrize(("kind", "label", "dark", "light"), CASES)
def test_badge_ink_mapping(kind: str, label: str, dark: str, light: str) -> None:
    ink = PALETTE[_index(kind)][2]
    assert agent_badge_ink(kind, True) == dark  # identity ink passes on dark
    assert agent_badge_ink(kind, False) == ink
    assert agent_badge_ink(kind.upper(), False) == ink


def test_badge_ink_fallback() -> None:
    assert agent_badge_ink("", True) == "#A7A3C7"
    assert agent_badge_ink("", False) == "#524E6E"


@pytest.mark.parametrize(("kind", "label", "dark", "light"), CASES)
def test_contrast_dark_composited_wash(kind: str, label: str, dark: str, light: str) -> None:
    bg = composite(dark, DARK_CARD)
    assert contrast_ratio(agent_badge_ink(kind, True), bg) >= 4.5


@pytest.mark.parametrize(("kind", "label", "dark", "light"), CASES)
def test_contrast_light_all_tile_states(kind: str, label: str, dark: str, light: str) -> None:
    for state, tile in LIGHT_TILES.items():
        bg = composite(light, tile)
        assert contrast_ratio(agent_badge_ink(kind, False), bg) >= 4.5, state


@pytest.mark.parametrize(("kind", "label", "dark", "light"), CASES)
def test_contrast_blocked_override(kind: str, label: str, dark: str, light: str) -> None:
    bg = composite(dark, BLOCKED_CARD)
    assert contrast_ratio(console.BLOCKED_TEXT, bg) >= 4.5


def test_contrast_fallback_composited() -> None:
    assert contrast_ratio(agent_badge_ink("", True), composite("#A7A3C7", DARK_CARD)) >= 4.5
    for state, tile in LIGHT_TILES.items():
        bg = composite("#5B577A", tile)
        assert contrast_ratio(agent_badge_ink("", False), bg) >= 4.5, state
    assert contrast_ratio(console.BLOCKED_TEXT, composite("#A7A3C7", BLOCKED_CARD)) >= 4.5


@pytest.mark.parametrize(
    ("subtitle", "agent", "rest"),
    [
        (f"{CUM_CLI.id} · Space Alpha · 48m ago", CUM_CLI.id, "Space Alpha · 48m ago"),
        (MSG_CLI.id, MSG_CLI.id, ""),
        (f"{CUM_CLI.id}2 · X", CUM_CLI.id, f"{CUM_CLI.id}2 · X"),  # no false prefix split
        ("Space Alpha · 48m ago", "", "Space Alpha · 48m ago"),
        ("", MSG_CLI.id, ""),
    ],
)
def test_subtitle_rest(subtitle: str, agent: str, rest: str) -> None:
    assert agent_subtitle_rest(subtitle, agent) == rest
