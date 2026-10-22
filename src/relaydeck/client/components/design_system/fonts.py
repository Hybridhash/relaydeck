"""Bundled-font registration for the design system.

Flet 1.0.3 `page.fonts` maps one file per family name (the pinned Dart
client builds one `FontLoader` asset per family with no weight
descriptors), so every static weight registers under its own family
alias. `face()` maps a (family, weight) pair to the registered alias;
text styles must use the alias as `font_family` or the wrong static
face renders. All files live under
`src/relaydeck/client/assets/design_system/fonts/` (see PROVENANCE.md);
The application wires them through the actual web/Android packaging. Preview and
tests reference the same table, so an alias rename fails loudly.
"""

from __future__ import annotations

from pathlib import Path

from relaydeck.client.components.design_system.font_versions import versioned_url

ASSETS_DIR = Path(__file__).resolve().parent.parent.parent / "assets" / "design_system"
FONTS_DIR = ASSETS_DIR / "fonts"

FAMILY_INTERFACE = "Bricolage Grotesque"
FAMILY_MONO = "JetBrains Mono"
FAMILY_SYMBOLS = "Noto Sans Symbols 2"
FAMILY_SYMBOLS_REGULAR = "Noto Sans Symbols"

# (family alias, file name, weight). Aliases are the `page.fonts` keys.
# The two symbols faces are weight-400 files used only through
# `font_family_fallback` (UI text and terminal); `face()` never maps to
# it because no style uses it as a primary family.
WEIGHT_FILES: tuple[tuple[str, str, int], ...] = (
    ("Bricolage Grotesque", "BricolageGrotesque-Regular.ttf", 400),
    ("Bricolage Grotesque Medium", "BricolageGrotesque-Medium.ttf", 500),
    ("Bricolage Grotesque SemiBold", "BricolageGrotesque-SemiBold.ttf", 600),
    ("Bricolage Grotesque Bold", "BricolageGrotesque-Bold.ttf", 700),
    ("Bricolage Grotesque ExtraBold", "BricolageGrotesque-ExtraBold.ttf", 800),
    ("JetBrains Mono", "JetBrainsMono-Regular.ttf", 400),
    ("JetBrains Mono SemiBold", "JetBrainsMono-SemiBold.ttf", 600),
    ("JetBrains Mono Bold", "JetBrainsMono-Bold.ttf", 700),
    (FAMILY_SYMBOLS, "NotoSansSymbols2-Regular.ttf", 400),
    (FAMILY_SYMBOLS_REGULAR, "NotoSansSymbols-Regular.ttf", 400),
)

DOT_TILE = ASSETS_DIR / "dots" / "dot_tile.png"


def _alias_for(family: str, weight: int) -> str:
    if family == FAMILY_INTERFACE:
        if weight >= 800:
            return "Bricolage Grotesque ExtraBold"
        if weight >= 700:
            return "Bricolage Grotesque Bold"
        if weight >= 600:
            return "Bricolage Grotesque SemiBold"
        if weight >= 500:
            return "Bricolage Grotesque Medium"
        return "Bricolage Grotesque"
    if weight >= 700:
        return "JetBrains Mono Bold"
    if weight >= 600:
        return "JetBrains Mono SemiBold"
    return "JetBrains Mono"


def face(family: str, weight: int) -> str:
    """Registered `page.fonts` alias for a family/weight pair.

    Rounds down to the nearest bundled weight (mock uses 400/500/600/
    700/800 interface and 400/600/700 mono, so no component rounds).
    """
    if family not in (FAMILY_INTERFACE, FAMILY_MONO):
        raise ValueError(f"unknown mock font family: {family!r}")
    return _alias_for(family, weight)


def page_fonts(asset_base: str = "design_system") -> dict[str, str]:
    """`page.fonts` table mapping every alias to its bundled asset path.

    `asset_base` is the assets mount point (preview serves the
    design_system folder itself; the application passes the packaged prefix).
    """
    return {alias: versioned_url(asset_base, name) for alias, name, _ in WEIGHT_FILES}


def dot_tile_src(asset_base: str = "design_system") -> str:
    """Bundled dot-pattern tile path for the page-ground decoration image."""
    return f"{asset_base}/dots/dot_tile.png"
