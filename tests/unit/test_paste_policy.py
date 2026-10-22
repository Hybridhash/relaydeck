"""Source-derived paste validation policy.

Checks the allowed/rejected character table, single-line vs multiline routing, exact line/character
counts, verbatim preview, bracketed framing bytes, and 16 KiB input
chunking. No Enter is synthesized on any path.
"""

from __future__ import annotations

import pytest

from relaydeck.client.features.terminal.paste_policy import (
    BRACKET_END,
    BRACKET_START,
    OUTCOME_INSERT,
    OUTCOME_REJECTED,
    OUTCOME_REVIEW,
    character_count,
    chunk_payload,
    confirm_chunks,
    contains_only_safe_scalars,
    first_unsafe,
    frame_payload,
    insert_chunks,
    is_multiline,
    is_safe_scalar,
    line_count,
    preview,
    unsafe_reason,
    validate,
)
from relaydeck.contracts.wire.terminal import MAX_INPUT_CHUNK


def test_permitted_text_matrix() -> None:
    safe = [
        "",
        "echo hello",
        "line with tab\there",
        "unix\nnewline",
        "mac\rreturn",
        "win\r\npair",
        "CJK \u4e2d\u6587\u6f22\u5b57 wide \uff21\uff22",
        "emoji \U0001f600 \U0001f469\u200d\U0001f4bb flag \U0001f1fa\U0001f1f8",
        "combining e\u0301clair",
        "rtl \u05e9\u05dc\u05d5\u05dd bidi \u202amark\u202c",
        "zero-width space \u200b keeps",
        "bom \ufeff private \ue000 unassigned \U0010fffe",
        "controls-adjacent ~\u007e\u00a0\u00ff",
    ]
    for text in safe:
        assert contains_only_safe_scalars(text) is True, repr(text)
        assert unsafe_reason(text) is None, repr(text)


@pytest.mark.parametrize(
    "value",
    [
        0x00,
        0x01,
        0x02,
        0x03,
        0x04,
        0x05,
        0x06,
        0x07,
        0x08,
        0x0B,
        0x0C,
        0x0E,
        0x0F,
        0x10,
        0x11,
        0x12,
        0x13,
        0x14,
        0x15,
        0x16,
        0x17,
        0x18,
        0x19,
        0x1A,
        0x1B,
        0x1C,
        0x1D,
        0x1E,
        0x1F,
        0x7F,
        *range(0x80, 0xA0),
    ],
)
def test_rejected_control_matrix(value: int) -> None:
    text = f"ab{chr(value)}cd"
    assert contains_only_safe_scalars(text) is False
    assert first_unsafe(text) == (2, value)
    reason = unsafe_reason(text)
    assert reason is not None
    assert f"U+{value:04X}" in reason and "offset 2" in reason
    assert "TAB" in reason and "LF" in reason and "CR" in reason


def test_first_unsafe_reports_first_scalar() -> None:
    assert first_unsafe("clean") is None
    assert first_unsafe("a\x07b\x1bc") == (1, 0x07)
    assert unsafe_reason("") is None


def test_lone_surrogates_rejected_with_reason() -> None:
    for value in (0xD800, 0xDBFF, 0xDC00, 0xDFFF):
        assert is_safe_scalar(value) is False
        text = f"ab{chr(value)}cd"
        assert contains_only_safe_scalars(text) is False
        assert first_unsafe(text) == (2, value)
        reason = unsafe_reason(text)
        assert reason is not None and f"U+{value:04X}" in reason
        assert "offset 2" in reason
    bad = validate("a\ud800\nb", engine_bracketed=True)
    assert bad.outcome == OUTCOME_REJECTED
    assert bad.reason is not None and "U+D800" in bad.reason


def test_astral_characters_still_accepted() -> None:
    for text in ("\U0001f600", "\U0001f1fa\U0001f1f8", "a\U0001f600\nb"):
        assert contains_only_safe_scalars(text) is True, repr(text)
        assert text.encode("utf-8").decode("utf-8") == text
    multi = validate("\U0001f600\n\U0001f601", engine_bracketed=True)
    assert multi.outcome == OUTCOME_REVIEW and multi.review is not None


def test_multiline_detection() -> None:
    assert is_multiline("single") is False
    assert is_multiline("") is False
    assert is_multiline("a\nb") is True
    assert is_multiline("a\rb") is True
    assert is_multiline("a\r\nb") is True
    assert is_multiline("\n") is True


@pytest.mark.parametrize(
    ("text", "want"),
    [
        ("a", 1),
        ("a\nb", 2),
        ("a\rb", 2),
        ("a\r\nb", 2),
        ("a\n", 2),
        ("a\r", 2),
        ("a\n\nb", 3),
        ("a\r\rb", 3),
        ("a\r\n\r\nb", 3),
        ("\n\n\n", 4),
    ],
)
def test_line_count_exact(text: str, want: int) -> None:
    assert line_count(text) == want


@pytest.mark.parametrize(
    ("text", "want"),
    [
        ("hello", 5),
        ("", 0),
        ("e\u0301", 1),
        ("e\u0301clair", 6),
        ("\U0001f1fa\U0001f1f8", 1),
        ("\U0001f1fa\U0001f1f8\U0001f1e8\U0001f1e6", 2),
        ("\U0001f468\u200d\U0001f469\u200d\U0001f467", 1),
        ("a\r\nb", 3),
        ("\U0001f44d\U0001f3fd", 1),
        ("\u4e2d\u6587", 2),
        ("a\tb", 3),
    ],
)
def test_character_count_exact(text: str, want: int) -> None:
    assert character_count(text) == want


def test_preview_verbatim_and_truncated() -> None:
    assert preview("a\nb\t\u00e9\U0001f600") == "a\nb\t\u00e9\U0001f600"
    assert preview("x" * 2000) == "x" * 2000
    over = "y" * 2001
    assert preview(over) == "y" * 2000 + "\u2026"
    combining = "e\u0301" * 2001
    assert preview(combining) == "e\u0301" * 2000 + "\u2026"
    assert character_count(combining) == 2001


def test_validate_outcomes() -> None:
    single = validate("echo hi", engine_bracketed=True)
    assert (single.outcome, single.review, single.reason) == (OUTCOME_INSERT, None, None)
    assert validate("", engine_bracketed=False).outcome == OUTCOME_INSERT
    multi = validate("a\nb", engine_bracketed=True)
    assert multi.outcome == OUTCOME_REVIEW
    assert multi.review is not None
    assert (multi.review.line_count, multi.review.character_count) == (2, 3)
    assert multi.review.preview == "a\nb"
    assert multi.bracketed is True
    plain = validate("a\nb", engine_bracketed=False)
    assert plain.outcome == OUTCOME_REVIEW and plain.bracketed is False
    bad = validate("a\x1bb\nc", engine_bracketed=True)
    assert bad.outcome == OUTCOME_REJECTED
    assert bad.review is None
    assert bad.reason is not None and "U+001B" in bad.reason and "offset 1" in bad.reason


def test_framing_exact_bytes() -> None:
    assert BRACKET_START == b"\x1b[200~"
    assert BRACKET_END == b"\x1b[201~"
    assert frame_payload("a\nb", False) == b"a\nb"
    assert frame_payload("a\nb", True) == b"\x1b[200~a\nb\x1b[201~"
    assert frame_payload("", True) == b"\x1b[200~\x1b[201~"
    assert frame_payload("caf\u00e9 \U0001f600", True).startswith(BRACKET_START)
    assert frame_payload("caf\u00e9 \U0001f600", True).endswith(BRACKET_END)


def test_single_line_never_framed() -> None:
    assert insert_chunks("echo hi") == (b"echo hi",)
    assert insert_chunks("") == ()
    assert insert_chunks("a\tb \u00e9") == ("a\tb \u00e9".encode(),)


def test_chunking_respects_frame_bound() -> None:
    assert MAX_INPUT_CHUNK == 16 * 1024
    assert chunk_payload(b"") == ()
    assert chunk_payload(b"a" * MAX_INPUT_CHUNK) == (b"a" * MAX_INPUT_CHUNK,)
    two = chunk_payload(b"b" * (MAX_INPUT_CHUNK + 1))
    assert two == (b"b" * MAX_INPUT_CHUNK, b"b")
    assert all(len(chunk) <= MAX_INPUT_CHUNK for chunk in two)


def test_chunking_never_splits_multibyte_sequence() -> None:
    text = "a" * (MAX_INPUT_CHUNK - 1) + "\u00e9" + "z" * 10
    chunks = chunk_payload(text.encode("utf-8"))
    assert len(chunks) == 2
    assert all(len(chunk) <= MAX_INPUT_CHUNK for chunk in chunks)
    assert b"".join(chunks).decode("utf-8") == text
    assert chunks[0] == b"a" * (MAX_INPUT_CHUNK - 1)
    emoji = "\U0001f600" * 5000
    rounds = chunk_payload(emoji.encode("utf-8"))
    assert b"".join(rounds).decode("utf-8") == emoji
    assert all(len(chunk) <= MAX_INPUT_CHUNK for chunk in rounds)


def test_confirm_chunks_roundtrip_exact() -> None:
    body = ("line \u00e9\U0001f600\n" * 2000).rstrip("\n")
    framed = confirm_chunks(body, True)
    assert b"".join(framed) == BRACKET_START + body.encode() + BRACKET_END
    assert all(len(chunk) <= MAX_INPUT_CHUNK for chunk in framed)
    plain = confirm_chunks(body, False)
    assert b"".join(plain) == body.encode()


def test_no_enter_synthesized_on_any_path() -> None:
    assert insert_chunks("echo hi") == (b"echo hi",)
    assert not any(b"\r" in chunk or b"\n" in chunk for chunk in insert_chunks("echo hi"))
    src = "printf 'a'\nprintf 'b'"
    assert b"".join(confirm_chunks(src, False)) == src.encode()
    assert b"".join(confirm_chunks(src, True)) == BRACKET_START + src.encode() + BRACKET_END
    assert not frame_payload(src, True).endswith(b"\r")
    assert frame_payload(src + "\n", False) == src.encode() + b"\n"
