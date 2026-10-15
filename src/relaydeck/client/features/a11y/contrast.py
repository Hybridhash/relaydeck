"""WCAG contrast math over the real theme tokens.

Reference surfaces approximate the framework-owned Material surfaces
(light ~white, dark ~near-black); exact rendered pixels need a browser
or device (BLOCKED). Text content
uses framework on-surface colors (no custom text colors in the UI);
theme tokens are graphics/status accents that always pairs
with a text label, so they are audited at the 3:1 non-text bar while
any custom text color must meet 4.5:1.
"""

from __future__ import annotations

from relaydeck.client.theme import (
    ACCENT,
    BANNER_TONES,
    CONTRAST_SURFACE_DARK_REF,
    CONTRAST_SURFACE_LIGHT_REF,
    STATUS_COLORS,
)

# Reference surfaces now live in theme.toml [a11y]; names kept for callers.
SURFACE_LIGHT_REF = CONTRAST_SURFACE_LIGHT_REF
SURFACE_DARK_REF = CONTRAST_SURFACE_DARK_REF

MIN_TEXT_RATIO = 4.5
MIN_GRAPHIC_RATIO = 3.0


def _channel(value: int) -> float:
    linearized: float = value / 255
    if linearized <= 0.04045:
        return linearized / 12.92
    return float(((linearized + 0.055) / 1.055) ** 2.4)


def relative_luminance(color: str) -> float:
    """Relative luminance of a `#rrggbb` color per WCAG 2.1."""
    triplet = color.lstrip("#")
    red, green, blue = (int(triplet[i : i + 2], 16) for i in (0, 2, 4))
    return 0.2126 * _channel(red) + 0.7152 * _channel(green) + 0.0722 * _channel(blue)


def contrast_ratio(foreground: str, background: str) -> float:
    """WCAG contrast ratio between two `#rrggbb` colors (>= 1.0)."""
    first = relative_luminance(foreground)
    second = relative_luminance(background)
    lighter, darker = (first, second) if first >= second else (second, first)
    return (lighter + 0.05) / (darker + 0.05)


def theme_token_colors() -> dict[str, str]:
    """Every status/banner/accent token from the real theme module."""
    tokens = {f"status.{name}": value for name, value in STATUS_COLORS.items()}
    tokens.update({f"banner.{name}": value for name, value in BANNER_TONES.items()})
    tokens["accent"] = ACCENT
    return tokens


def audit_theme_tokens() -> tuple[tuple[str, str, float, float], ...]:
    """(name, color, vs-light, vs-dark) for each theme token."""
    return tuple(
        (
            name,
            color,
            contrast_ratio(color, SURFACE_LIGHT_REF),
            contrast_ratio(color, SURFACE_DARK_REF),
        )
        for name, color in sorted(theme_token_colors().items())
    )


__all__ = [
    "MIN_GRAPHIC_RATIO",
    "MIN_TEXT_RATIO",
    "SURFACE_DARK_REF",
    "SURFACE_LIGHT_REF",
    "audit_theme_tokens",
    "contrast_ratio",
    "relative_luminance",
    "theme_token_colors",
]
