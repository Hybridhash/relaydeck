"""Scroll authorization only accepts the pinned xterm vertical-wheel encodings."""

import pytest
from relaydeck_terminal.protocol import ORIGIN_KEY, ORIGIN_MOUSE, ViewState

from relaydeck.client.features.terminal.scrolling import is_scroll_input, last_lines


@pytest.mark.parametrize(
    "data",
    [
        b"\x1b[<64;12;3M",
        b"\x1b[<65;1;24M",
        b"\x1b[<92;1;1M",
        b"\x1b[M`!!",
        b"\x1b[Ma!!",
        b"\x1b[96;1;2M",
        b"\x1b[97;1;2M",
        "\x1b[M`\u0180\u0180".encode(),
    ],
)
def test_only_alt_mouse_wheels_pass(data: bytes) -> None:
    assert is_scroll_input(ORIGIN_MOUSE, data, ViewState(True, True, True))
    assert not is_scroll_input(ORIGIN_MOUSE, data, ViewState(False, True, True))
    assert not is_scroll_input(ORIGIN_MOUSE, data, ViewState(True, True, False))


@pytest.mark.parametrize(
    "data",
    [
        b"\x1b[<0;1;1M",
        b"\x1b[<64;1;1m",
        b"\x1b[<96;1;1M",
        b"\x1b[<66;1;1M",
        b"\x1b[<64;0;1M",
        b"\x1b[<64;1;65536M",
        b"\x1b[<64;1;1M\x1b[<65;1;1M",
        b"\xff",
        b"\x1b[M`!",
        b"hello",
    ],
)
def test_clicks_motion_release_horizontal_and_malformed_mouse_are_rejected(data: bytes) -> None:
    assert not is_scroll_input(ORIGIN_MOUSE, data, ViewState(True, True, True))


@pytest.mark.parametrize("data", [b"\x1b[A", b"\x1b[B", b"\x1bOA", b"\x1bOB"])
def test_alternate_arrow_fallback_is_exact_and_requires_alt(data: bytes) -> None:
    assert is_scroll_input(ORIGIN_KEY, data, ViewState(True, True, False))
    assert not is_scroll_input(ORIGIN_KEY, data, ViewState(False, True, False))


def test_history_cap_preserves_exact_line_delimiters() -> None:
    text = "\r\n".join(f"line {i} 😀\u2028same line" for i in range(5002)) + "\r\n"
    assert last_lines(text, 5000) == text[text.index("line 2 ") :]
    assert last_lines("\n\n", 100) == "\n\n"
    assert last_lines("one\ntwo", 1) == "two"
