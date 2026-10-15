"""Value labels for every chart bar. Pure: no Flet.

A bar with a value above 0 always shows it. The plan picks the first form that fits one bar slot:

1. `flat`: the compact text, horizontal, at the base size;
2. `rotated`: the same text turned 90 degrees (bottom to top) at the base size, shrinking to 9;
3. `short`: the shortest compact form (`0.31B` becomes `0.3B`, `4.2K` becomes `4K`), horizontal at
   9 to the base size;
4. `tap`: no labels on the bars; the value shows when the bar is tapped (or hovered).

Text widths are estimated from the character count (Flet has no text-measuring API), so the
estimates are deliberately a little generous. The mode is part of the plan, so the fallback is
recorded and testable."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

MIN_SIZE = 9
FLAT = "flat"
ROTATED = "rotated"
SHORT = "short"
TAP = "tap"
_GLYPH = 0.64  # em per character: bold digits plus the unit
_LINE = 1.2  # em: one line's height
_SLACK = 2.0  # px of air so neighbours never touch


@dataclass(frozen=True)
class ValuePlan:
    mode: str
    size: int
    texts: dict[int, str]  # bar index -> label, only bars with a value
    height: float  # the room the value row needs above the plot (0 for `tap`)


def text_width(text: str, size: float) -> float:
    return len(text) * size * _GLYPH + _SLACK


def line_height(size: float) -> float:
    return size * _LINE


def shortest(text: str) -> str:
    """The shortest compact form of a chart value. A value under 1 keeps its figures (0.5K would
    otherwise double)."""
    if not text or text[-1] not in "BMK":
        return text
    unit = text[-1]
    try:
        value = Decimal(text[:-1])
    except InvalidOperation:
        return text
    if unit == "B":
        places = Decimal("0.1")
    elif value >= 1:
        places = Decimal("1")
    else:
        return text
    rounded = value.quantize(places, ROUND_HALF_UP)
    if rounded == 0:
        return text
    return f"{rounded:f}{unit}"


def plan_values(
    values: list[str], *, slot_px: float, base_size: int, min_height: float
) -> ValuePlan:
    """Choose the form for the bars' value labels. `values[i]` is "" for a bar with no value."""
    texts = {index: value for index, value in enumerate(values) if value}
    if not texts:
        return ValuePlan(FLAT, base_size, {}, min_height)
    widest = max(texts.values(), key=len)
    if text_width(widest, base_size) <= slot_px:
        return ValuePlan(FLAT, base_size, texts, min_height)
    for size in range(base_size, MIN_SIZE - 1, -1):
        if line_height(size) <= slot_px:
            length = len(widest) * size * _GLYPH + _SLACK
            return ValuePlan(ROTATED, size, texts, max(min_height, length))
    compact = {index: shortest(text) for index, text in texts.items()}
    widest_short = max(compact.values(), key=len)
    for size in range(base_size, MIN_SIZE - 1, -1):
        if text_width(widest_short, size) <= slot_px:
            return ValuePlan(SHORT, size, compact, min_height)
    return ValuePlan(TAP, base_size, {}, 0)
