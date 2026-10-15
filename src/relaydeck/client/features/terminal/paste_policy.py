"""Safe paste validation, counts, and framing.

TAB, LF and CR are the only permitted control characters. Lone surrogates are
rejected because they cannot be encoded as UTF-8. Paste preserves all other
text verbatim; single-line insertion is unframed and never adds Enter.
Multiline confirmation uses bracketed framing only if captured at review open.

Displayed character counts approximate grapheme clusters with a UAX-29 subset:
combining marks, variation selectors, emoji modifiers, ZWJ joins, regional pairs,
and CRLF glue. Prepend and Hangul Jamo composition are not modeled. ZWJ joins
unconditionally; this affects counts and preview cuts, never transmitted bytes.
Line counts treat CRLF as one break. Preview is limited to 2000 characters."""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass

from relaydeck.contracts.wire.terminal import MAX_INPUT_CHUNK

BRACKET_START = b"\x1b[200~"
BRACKET_END = b"\x1b[201~"

PREVIEW_LIMIT = 2000
ELLIPSIS = "\u2026"

OUTCOME_INSERT = "insert"
OUTCOME_REVIEW = "review"
OUTCOME_REJECTED = "rejected"

SAFE_CONTROLS = frozenset({0x09, 0x0A, 0x0D})

_ZWJ = 0x200D
_CR = 0x0D
_LF = 0x0A
_RI_LO = 0x1F1E6
_RI_HI = 0x1F1FF


class PasteError(Exception):
    """Explicit paste-routing failure. `code` is `no_writer` (no live
    control writer), `stale_generation` (review bound to a replaced
    writer), or `no_review` (confirm with nothing pending)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def is_safe_scalar(value: int) -> bool:
    """Allow TAB/LF/CR and non-control, non-surrogate Unicode scalars.

    Rejecting lone surrogates keeps UTF-8 encoding total.
    """
    if value in SAFE_CONTROLS:
        return True
    return unicodedata.category(chr(value)) not in ("Cc", "Cs")


def contains_only_safe_scalars(text: str) -> bool:
    return all(is_safe_scalar(ord(char)) for char in text)


def first_unsafe(text: str) -> tuple[int, int] | None:
    """Offset and code point of the first rejected scalar, if any."""
    for offset, char in enumerate(text):
        value = ord(char)
        if not is_safe_scalar(value):
            return (offset, value)
    return None


def unsafe_reason(text: str) -> str | None:
    """Exact rejection reason naming the first unsafe scalar, if any."""
    found = first_unsafe(text)
    if found is None:
        return None
    offset, value = found
    name = unicodedata.name(chr(value), "UNKNOWN")
    return (
        f"unsafe control U+{value:04X} ({name}) at offset {offset}; "
        "only TAB (U+0009), LF (U+000A), CR (U+000D) are permitted"
    )


def is_multiline(text: str) -> bool:
    """True when any CR or LF scalar is present."""
    return any(ord(char) in (_CR, _LF) for char in text)


def _is_regional_indicator(value: int) -> bool:
    return _RI_LO <= value <= _RI_HI


def _is_extend(value: int, char: str) -> bool:
    if unicodedata.category(char) in ("Mn", "Me", "Mc"):
        return True
    return 0xFE00 <= value <= 0xFE0F or 0xE0100 <= value <= 0xE01EF or 0x1F3FB <= value <= 0x1F3FF


def split_clusters(text: str) -> tuple[str, ...]:
    """Extended-grapheme-cluster approximation (documented UAX-29 subset;
    see the module docstring). One cluster per user-perceived character
    for ASCII and common emoji/combining text."""
    clusters: list[str] = []
    current: list[str] = []
    prev: str | None = None
    prev_zwj = False
    ri_run = 0
    for char in text:
        value = ord(char)
        if not current:
            current.append(char)
        elif prev is not None:
            glue = (
                prev_zwj
                or value == _ZWJ
                or _is_extend(value, char)
                or (value == _LF and ord(prev) == _CR)
                or (
                    _is_regional_indicator(value)
                    and _is_regional_indicator(ord(prev))
                    and ri_run % 2 == 1
                )
            )
            if glue:
                current.append(char)
            else:
                clusters.append("".join(current))
                current = [char]
        prev_zwj = value == _ZWJ
        ri_run = ri_run + 1 if _is_regional_indicator(value) else 0
        prev = char
    if current:
        clusters.append("".join(current))
    return tuple(clusters)


def character_count(text: str) -> int:
    """Count grapheme clusters under the documented subset."""
    return len(split_clusters(text))


def line_count(text: str) -> int:
    """Count CRLF once, lone CR and LF separately.

    A trailing newline opens an empty final line.
    """
    count = 1
    prev_cr = False
    for char in text:
        value = ord(char)
        if value == _LF:
            if not prev_cr:
                count += 1
        elif value == _CR:
            count += 1
        prev_cr = value == _CR
    return count


def preview(text: str) -> str:
    """Show at most 2000 characters, then an ellipsis.

    Counts continue to describe the full text.
    """
    clusters = split_clusters(text)
    if len(clusters) <= PREVIEW_LIMIT:
        return text
    return "".join(clusters[:PREVIEW_LIMIT]) + ELLIPSIS


@dataclass(frozen=True)
class PasteReview:
    line_count: int
    character_count: int
    preview: str
    from_app: bool = False


@dataclass(frozen=True)
class PasteValidation:
    outcome: str
    review: PasteReview | None = None
    reason: str | None = None
    bracketed: bool = False


def validate(source_text: str, engine_bracketed: bool) -> PasteValidation:
    """Classify pasted text: accepted single-line insert, multiline
    review (with exact counts/preview and the framing captured now),
    or rejection with the exact unsafe-scalar reason."""
    reason = unsafe_reason(source_text)
    if reason is not None:
        return PasteValidation(OUTCOME_REJECTED, reason=reason)
    if not is_multiline(source_text):
        return PasteValidation(OUTCOME_INSERT)
    return PasteValidation(
        OUTCOME_REVIEW,
        review=PasteReview(
            line_count=line_count(source_text),
            character_count=character_count(source_text),
            preview=preview(source_text),
        ),
        bracketed=engine_bracketed,
    )


def frame_payload(text: str, bracketed: bool) -> bytes:
    """Frame UTF-8 only when bracketed mode is negotiated; never add Enter."""
    body = text.encode("utf-8")
    if not bracketed:
        return body
    return BRACKET_START + body + BRACKET_END


def chunk_payload(payload: bytes) -> tuple[bytes, ...]:
    """Split framed bytes into <=16 KiB input frames on UTF-8 code-point
    boundaries (never inside a multibyte sequence). Empty input yields
    zero frames: nothing is sent."""
    if not payload:
        return ()
    chunks: list[bytes] = []
    start = 0
    while start < len(payload):
        end = min(start + MAX_INPUT_CHUNK, len(payload))
        while end > start and end < len(payload) and (payload[end] & 0xC0) == 0x80:
            end -= 1
        if end == start:  # unreachable for valid UTF-8 (<=4-byte sequences)
            end = start + MAX_INPUT_CHUNK
        chunks.append(payload[start:end])
        start = end
    return tuple(chunks)


def insert_chunks(text: str) -> tuple[bytes, ...]:
    """Single-line UTF-8 input is never bracketed-framed."""
    return chunk_payload(text.encode("utf-8"))


def confirm_chunks(text: str, bracketed: bool) -> tuple[bytes, ...]:
    """Reviewed multiline confirm bytes: framed iff bracketed mode was
    captured at review open."""
    return chunk_payload(frame_payload(text, bracketed))
