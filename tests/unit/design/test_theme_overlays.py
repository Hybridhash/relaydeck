"""Seasonal theme overlays: what they may replace, when they apply, and that they stay readable."""

import copy
import os
import subprocess
import sys
from datetime import date

import pytest

from relaydeck.client import theme
from relaydeck.client.features.a11y.contrast import contrast_ratio


def _doc_with(overlay: dict) -> dict:
    doc = copy.deepcopy(theme._BASE_DOC)
    doc["themes"] = {"active": "auto", "test": overlay}
    return doc


@pytest.mark.parametrize(
    "overlay",
    [
        {"console": {"text": "#FFFFFF"}},
        {"console": {"blocked": "#FF0000"}},
        {"palette": {"dark": {"terminal_bg": "#000000"}}},
        {"palette": {"dark": {"success": "#00FF00"}}},
        {"palette": {"dark": {"accent": "red"}}},
        {"palette": {"sepia": {"bg": "#000000"}}},
        {"window": ["12-15"]},
        {"window": ["13-01", "01-02"]},
        {"fonts": {}},
    ],
)
def test_an_overlay_may_not_touch_meaning_text_or_terminal_colours(overlay: dict) -> None:
    with pytest.raises(ValueError):
        theme._overlays(_doc_with(overlay))


def test_active_must_name_auto_default_or_an_overlay() -> None:
    doc = copy.deepcopy(theme._BASE_DOC)
    doc["themes"]["active"] = "missing"
    with pytest.raises(ValueError):
        theme._overlays(doc)


@pytest.mark.parametrize(
    ("today", "expected"),
    [
        (date(2026, 12, 14), "default"),
        (date(2026, 12, 15), "christmas"),
        (date(2026, 12, 31), "christmas"),
        (date(2027, 1, 2), "christmas"),
        (date(2027, 1, 3), "default"),
        (date(2027, 7, 1), "default"),
    ],
)
def test_auto_follows_the_window_across_the_new_year(today: date, expected: str) -> None:
    assert theme.resolve_theme_name("auto", dict(theme.OVERLAYS), today) == expected


def test_an_explicit_choice_wins_and_an_unknown_one_falls_back() -> None:
    overlays = dict(theme.OVERLAYS)
    assert theme.resolve_theme_name("default", overlays, date(2026, 12, 25)) == "default"
    assert theme.resolve_theme_name("christmas", overlays, date(2026, 7, 1)) == "christmas"
    assert theme.resolve_theme_name("nope", overlays, date(2026, 12, 25)) == "default"


@pytest.mark.parametrize("name", sorted(theme.OVERLAYS))
def test_every_overlay_keeps_the_contrast_minimums(name: str) -> None:
    merged = theme.apply_overlay(theme._BASE_DOC, theme.OVERLAYS[name])
    for mode in ("dark", "light"):
        tokens = merged["palette"][mode]
        for pair, fg, bg, minimum in theme.CONTRAST_PAIRS:
            ratio = contrast_ratio(tokens[fg], tokens[bg])
            assert ratio >= minimum, f"{name} {mode} {pair}: {ratio:.2f} < {minimum}"
        assert contrast_ratio(tokens["muted"], tokens["rail_bg"]) >= 4.5
        assert contrast_ratio(tokens["on_accent"], tokens["accent"]) >= 4.5
    console = merged["console"]
    assert contrast_ratio(console["on_primary"], console["primary_fill"]) >= 4.5
    for fg in ("text", "text_secondary", "text_muted", "primary_text"):
        for bg in ("ground", "card", "raised", "panel", "nav"):
            assert contrast_ratio(console[fg], console[bg]) >= 4.5, f"{name} {fg}/{bg}"


def test_the_default_document_is_left_untouched_by_an_overlay() -> None:
    before = copy.deepcopy(theme._BASE_DOC)
    theme.apply_overlay(theme._BASE_DOC, theme.OVERLAYS["christmas"])
    assert theme._BASE_DOC == before


def test_the_environment_choice_reaches_the_module_tokens() -> None:
    probe = (
        "import relaydeck.client.theme as t;"
        "import relaydeck.client.components.design_system.tokens as T;"
        "print(t.ACTIVE_THEME, T.GROUND, t.DARK['accent'], t.STATUS_COLORS['blocked'])"
    )
    out = {}
    for name in ("default", "christmas"):
        env = {**os.environ, "RELAYDECK_THEME": name}
        result = subprocess.run(
            [sys.executable, "-B", "-c", probe], env=env, capture_output=True, text=True, check=True
        )
        out[name] = result.stdout.split()
    assert out["default"][0] == "default" and out["christmas"][0] == "christmas"
    assert out["default"][1] != out["christmas"][1]
    assert out["default"][2] != out["christmas"][2]
    assert out["default"][3] == out["christmas"][3]  # status colours never change
