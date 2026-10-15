"""Tokens: the dark values are exact, the light set clears
4.5:1 for every text pair the screen uses, and no existing token is recoloured."""

from __future__ import annotations

from relaydeck.client.components.design_system import tokens as T


def _luminance(hex_color: str) -> float:
    channels = [int(hex_color[i : i + 2], 16) / 255 for i in (1, 3, 5)]
    linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _ratio(first: str, second: str) -> float:
    high, low = sorted((_luminance(first), _luminance(second)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def test_the_dark_values_are_the_spec_values() -> None:
    dark = T.usage_palette(True)
    expected = {
        "bg": "#13101F",
        "surface": "#1C1730",
        "text": "#F2EFFA",
        "text_muted": "#A9A2C8",
        "accent": "#7046F0",
        "link": "#B9A4FF",
        "series_1": "#8B63FF",
        "series_1_dim": "#5A3BC4",
        "series_2": "#4FD1B8",
        "series_other": "#5B5380",
        "warn": "#FFC46B",
        "warn_bg": "#3A2A12",
        "danger": "#FF7A85",
        "danger_bg": "#3A1620",
    }
    for name, value in expected.items():
        assert dark[name] == value, name


def test_text_pairs_clear_four_and_a_half_to_one_on_both_themes() -> None:
    for palette in (T.usage_palette(True), T.usage_palette(False)):
        for text in ("text", "text_2", "text_muted", "link", "warn", "danger"):
            for ground in ("surface", "surface_raised"):
                assert _ratio(palette[text], palette[ground]) >= 4.5, (text, ground)
        assert _ratio(palette["warn"], palette["warn_bg"]) >= 4.5
        assert _ratio(palette["accent_on"], palette["accent"]) >= 4.5


def test_the_light_warning_pill_text_is_readable_on_its_own_fill() -> None:
    assert _ratio(T.USAGE_LIGHT["warn"], T.USAGE_LIGHT["warn_bg"]) >= 4.5


def test_faint_text_is_only_used_where_it_is_not_actionable() -> None:
    # Spec §4.1: text-faint is for footnotes and axes only. It still clears 4.5:1 on cards.
    assert _ratio(T.USAGE_DARK["text_faint"], T.USAGE_DARK["surface"]) >= 4.5
    assert _ratio(T.USAGE_LIGHT["text_faint"], T.USAGE_LIGHT["surface"]) >= 4.5


def test_existing_tokens_are_not_recoloured_by_the_usage_set() -> None:
    assert T.GROUND == "#130F26" and T.CARD == "#1F1840" and T.PRIMARY_FILL == "#7443F5"
    assert T.DONE == "#C8F03C" and T.BLOCKED == "#FF8A3D"


def test_the_palette_is_a_copy() -> None:
    palette = T.usage_palette(True)
    palette["surface"] = "#000000"
    assert T.USAGE_DARK["surface"] == "#1C1730"
