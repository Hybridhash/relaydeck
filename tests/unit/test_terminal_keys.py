"""Herdr key-combo grammar mirrors upstream (065ef9d6)."""

from __future__ import annotations

from relaydeck.connector.keys import is_valid_key, normalize_combo


def test_modifiers_and_named_keys() -> None:
    assert normalize_combo("ctrl+c") == (frozenset({"ctrl"}), "c")
    assert normalize_combo("Enter") == (frozenset(), "enter")
    assert normalize_combo("shift+Tab") == (frozenset({"shift"}), "tab")
    assert normalize_combo("right") == (frozenset(), "right")
    assert normalize_combo("alt+option+x") == (frozenset({"alt", "option"}), "x")
    assert normalize_combo("F12") == (frozenset(), "f12")
    assert normalize_combo("cmd+shift+p") == (frozenset({"cmd", "shift"}), "p")


def test_api_aliases() -> None:
    assert normalize_combo("C-c") == (frozenset({"ctrl"}), "c")
    assert normalize_combo("c-c") == (frozenset({"ctrl"}), "c")
    assert normalize_combo("+") == (frozenset(), "plus")


def test_invalid_combos_rejected() -> None:
    assert normalize_combo("") is None
    assert normalize_combo("ctrl++c") is None  # empty part
    assert normalize_combo("a+b") is None  # two keys
    assert normalize_combo("ctrl") is None  # modifier only
    assert normalize_combo("home") is None  # not in upstream grammar
    assert normalize_combo("pageup") is None
    assert normalize_combo("delete") is None
    assert normalize_combo("f0") == (frozenset(), "f0")  # u8 range
    assert normalize_combo("f256") is None
    assert normalize_combo("x" * 65) is None
    assert is_valid_key("ctrl+c") is True
    assert is_valid_key("nope-not-a-key-name") is False
