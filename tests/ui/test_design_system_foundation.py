"""Tokens, ordering, fonts, assets.

Locks the mock rules the application integrates against (status order, initials,
breakpoints, font-table shape) and proves the bundled assets exist and
load offline. Contrast checks verify the mock's own dark-on-fill
claims with real math; they never restate a constant alone.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageFont

from relaydeck.client.components.design_system import tokens as T
from relaydeck.client.components.design_system.fonts import (
    ASSETS_DIR,
    DOT_TILE,
    FAMILY_SYMBOLS,
    FONTS_DIR,
    WEIGHT_FILES,
    dot_tile_src,
    face,
    page_fonts,
)
from relaydeck.client.features.a11y import contrast_ratio

MIN_TEXT_RATIO = 4.5


def test_status_ordering_unknown_last_and_stable() -> None:
    items = [
        ("u1", T.STATUS_UNKNOWN),
        ("d1", T.STATUS_DONE),
        ("b2", T.STATUS_BLOCKED),
        ("w1", T.STATUS_WORKING),
        ("b1", T.STATUS_BLOCKED),
        ("i1", T.STATUS_IDLE),
        ("bogus", "mystery"),
    ]
    ordered = T.sort_keys(items, lambda item: item[1])
    assert [name for name, _ in ordered] == ["b2", "b1", "w1", "d1", "i1", "u1", "bogus"]
    # Unknown and unrecognized values rank last, never implied live.
    assert T.status_rank("mystery") == T.status_rank(T.STATUS_UNKNOWN)


def test_initials_vectors() -> None:
    assert T.initials("fx-rounding-fix") == "FX"
    assert T.initials("onboarding-copy") == "ON"
    assert T.initials("migrate-to-pg16") == "MI"
    assert T.initials("flaky-test-hunt") == "FL"
    assert T.initials("changelog-draft") == "CH"
    assert T.initials("solo") == "SO"
    assert T.initials("x") == "X"
    assert T.initials("") == "??"


def test_layout_breakpoints() -> None:
    assert T.layout_class(360) == T.LAYOUT_STACKED
    assert T.layout_class(599) == T.LAYOUT_STACKED
    assert T.layout_class(1099) == T.LAYOUT_STACKED
    assert T.layout_class(1100) == T.LAYOUT_SPLIT
    assert T.layout_class(1440) == T.LAYOUT_SPLIT
    assert T.has_rail(390) is False
    assert T.has_rail(599) is False
    assert T.has_rail(600) is True
    assert T.has_rail(768) is True


def test_font_assets_exist_and_load_offline() -> None:
    assert len(WEIGHT_FILES) == 9
    for alias, name, _weight in WEIGHT_FILES:
        path = FONTS_DIR / name
        assert path.is_file(), alias
        font = ImageFont.truetype(str(path), 16)
        family, _style = font.getname()
        assert (
            "Bricolage Grotesque" in family
            or "JetBrains Mono" in family
            or "Noto Sans Symbols 2" in family
        ), alias
    # Licenses ship beside the binaries they cover.
    assert (ASSETS_DIR / "OFL-BricolageGrotesque.txt").is_file()
    assert (ASSETS_DIR / "OFL-JetBrainsMono.txt").is_file()
    assert (ASSETS_DIR / "OFL-NotoSansSymbols2.txt").is_file()
    assert "SIL Open Font License" in (ASSETS_DIR / "OFL-BricolageGrotesque.txt").read_text()
    assert "SIL Open Font License" in (ASSETS_DIR / "OFL-JetBrainsMono.txt").read_text()
    assert "SIL Open Font License" in (ASSETS_DIR / "OFL-NotoSansSymbols2.txt").read_text()
    assert "Noto Project Authors" in (ASSETS_DIR / "OFL-NotoSansSymbols2.txt").read_text()


def test_symbols_font_registered_and_wired_as_fallback() -> None:
    from relaydeck.client.components.appearance import build_theme
    from relaydeck.client.components.design_system.primitives import text_style
    from relaydeck.client.theme import TEXT_FONT_FALLBACK

    table = page_fonts("design_system")
    assert table[FAMILY_SYMBOLS] == "design_system/fonts/NotoSansSymbols2-Regular.ttf"
    assert TEXT_FONT_FALLBACK == (FAMILY_SYMBOLS,)
    # Theme text and mock card text list the loaded family as fallback
    # so agent symbols render instead of tofu.
    for dark in (False, True):
        theme = build_theme(dark)
        styles = [
            theme.text_theme.body_large,
            theme.text_theme.body_medium,
            theme.text_theme.body_small,
            theme.text_theme.title_large,
            theme.text_theme.title_medium,
            theme.text_theme.title_small,
            theme.text_theme.label_large,
            theme.text_theme.label_medium,
            theme.text_theme.label_small,
        ]
        for style in styles:
            assert style is not None
            assert FAMILY_SYMBOLS in (style.font_family_fallback or []), style
    card = text_style("Bricolage Grotesque", T.TYPE_CARD_TITLE, T.TEXT)
    assert FAMILY_SYMBOLS in (card.font_family_fallback or [])


def test_font_table_shape_is_offline_and_complete() -> None:
    table = page_fonts("design_system")
    assert set(table) == {alias for alias, _, _ in WEIGHT_FILES}
    for alias, src in table.items():
        assert not src.startswith("http"), alias
        assert (
            ASSETS_DIR / Path(src).name if "/" not in src else FONTS_DIR / src.split("/")[-1]
        ).is_file()
    assert face("Bricolage Grotesque", 800) == "Bricolage Grotesque ExtraBold"
    assert face("Bricolage Grotesque", 700) == "Bricolage Grotesque Bold"
    assert face("Bricolage Grotesque", 500) == "Bricolage Grotesque Medium"
    assert face("JetBrains Mono", 400) == "JetBrains Mono"


def test_dot_tile_is_valid_18px_png() -> None:
    assert DOT_TILE.is_file()
    assert DOT_TILE.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    with Image.open(DOT_TILE) as tile:
        assert tile.size == (18, 18)
    assert dot_tile_src("design_system") == "design_system/dots/dot_tile.png"


def test_mock_dark_on_fill_pairs_pass_contrast() -> None:
    pairs = {
        "blocked text/orange": (T.BLOCKED_TEXT, T.BLOCKED),
        "blocked small/orange": (T.BLOCKED_SMALL_TEXT, T.BLOCKED),
        "blocked pill": (T.BLOCKED_PILL_TEXT, T.BLOCKED_PILL_BG),
        "working/sky": (T.WORKING_TEXT, T.WORKING),
        "done/lime": (T.DONE_TEXT, T.DONE),
        "idle": (T.IDLE_TEXT, T.IDLE_FILL),
        "unknown outline": (T.UNKNOWN_TEXT, T.PANEL),
        "text/card": (T.TEXT, T.CARD),
        "secondary/card": (T.TEXT_SECONDARY, T.CARD),
        "muted/card": (T.TEXT_MUTED, T.CARD),
        "white/primary": ("#FFFFFF", T.PRIMARY_FILL),
    }
    for name, (fg, bg) in pairs.items():
        assert contrast_ratio(fg, bg) >= MIN_TEXT_RATIO, name
