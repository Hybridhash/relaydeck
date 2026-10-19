"""Extended grapheme clusters per Unicode UAX #29 (rules GB1 to GB999, with GB9c).

The property tables are generated (grapheme_data.py). One function serves the client
counter, the input clamp and the gateway policy, so they always agree.
"""

from __future__ import annotations

from bisect import bisect_right

from relaydeck.contracts import grapheme_data


class _Table:
    def __init__(self, text: str) -> None:
        rows = [line.split() for line in text.strip().splitlines()]
        self._starts = [int(span.split("-")[0], 16) for span, _ in rows]
        self._ends = [int(span.split("-")[1], 16) for span, _ in rows]
        self._kinds = [kind for _, kind in rows]

    def get(self, code: int) -> str:
        index = bisect_right(self._starts, code) - 1
        return self._kinds[index] if index >= 0 and code <= self._ends[index] else ""


_BREAK = _Table(grapheme_data.GRAPHEME_BREAK)
_PICTOGRAPHIC = _Table(grapheme_data.EXTENDED_PICTOGRAPHIC)
_CONJUNCT = _Table(grapheme_data.INDIC_CONJUNCT_BREAK)

_HANGUL_FIRST, _HANGUL_LAST, _JAMO_T_COUNT = 0xAC00, 0xD7A3, 28
_CONTROLS = frozenset({"Control", "CR", "LF"})


def _break_class(code: int) -> str:
    if _HANGUL_FIRST <= code <= _HANGUL_LAST:
        return "LV" if (code - _HANGUL_FIRST) % _JAMO_T_COUNT == 0 else "LVT"
    return _BREAK.get(code) or "Other"


def graphemes(text: str) -> list[str]:
    """Split text into extended grapheme clusters."""
    clusters: list[str] = []
    prev = ""  # break class of the previous character
    pict = 0  # GB11: 1 after ExtPict Extend*, 2 after that plus ZWJ
    conjunct = 0  # GB9c: 1 after a consonant, 2 once a linker followed it
    regional = 0  # consecutive regional indicators ending at the previous character
    for char in text:
        code = ord(char)
        kind = _break_class(code)
        incb = _CONJUNCT.get(code)
        extended = bool(_PICTOGRAPHIC.get(code))
        joins = bool(clusters) and not _boundary(
            prev, kind, extended, incb, pict, conjunct, regional
        )
        if joins:
            clusters[-1] += char
        else:
            clusters.append(char)
        if extended:
            pict = 1
        elif kind == "Extend" and pict == 1:
            pass
        elif kind == "ZWJ" and pict == 1:
            pict = 2
        else:
            pict = 0
        if incb == "Consonant":
            conjunct = 1
        elif conjunct and incb == "Linker":
            conjunct = 2
        elif not (conjunct and incb == "Extend"):
            conjunct = 0
        regional = regional + 1 if kind == "RI" else 0
        prev = kind
    return clusters


def _boundary(
    prev: str, kind: str, extended: bool, incb: str, pict: int, conjunct: int, regional: int
) -> bool:
    if prev == "CR" and kind == "LF":  # GB3
        return False
    if prev in _CONTROLS or kind in _CONTROLS:  # GB4, GB5
        return True
    if prev == "L" and kind in ("L", "V", "LV", "LVT"):  # GB6
        return False
    if prev in ("LV", "V") and kind in ("V", "T"):  # GB7
        return False
    if prev in ("LVT", "T") and kind == "T":  # GB8
        return False
    if kind in ("Extend", "ZWJ", "SpacingMark") or prev == "Prepend":  # GB9, GB9a, GB9b
        return False
    if conjunct == 2 and incb == "Consonant":  # GB9c
        return False
    if pict == 2 and extended:  # GB11
        return False
    return not (kind == "RI" and regional % 2 == 1)  # GB12, GB13, GB999


def grapheme_count(text: str) -> int:
    return len(graphemes(text))
