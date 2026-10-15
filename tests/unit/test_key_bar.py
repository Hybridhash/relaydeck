"""Key-bar order/labels, encode resolution, latch, width class."""

from __future__ import annotations

import pytest

from relaydeck.client.features.terminal.key_bar import (
    KEY_BAR,
    KEY_BAR_GROUPS,
    KeyBarItem,
    ModifierLatch,
    width_class,
)
from relaydeck.client.features.terminal.keys import MOD_ALT, MOD_CTRL, encode_key

LABELS = [
    "Esc",
    "Tab",
    "\u21e7Tab",
    "\u2190",
    "\u2191",
    "\u2193",
    "\u2192",
    "\u232b",
    "\u23ce",
    "Ctrl",
    "Alt",
    "^C",
    "^D",
    "^Z",
    "^L",
    "^R",
    "/",
    "~",
    "|",
    "-",
    "Paste",
    "Select",
]

IDS = [
    "key-esc",
    "key-tab",
    "key-shift-tab",
    "key-left",
    "key-up",
    "key-down",
    "key-right",
    "key-backspace",
    "key-enter",
    "mod-ctrl",
    "mod-alt",
    "key-ctrl-c",
    "key-ctrl-d",
    "key-ctrl-z",
    "key-ctrl-l",
    "key-ctrl-r",
    "key-slash",
    "key-tilde",
    "key-pipe",
    "key-minus",
    "action-paste",
    "action-select",
]


def test_order_labels_ids_and_groups_match_spec() -> None:
    assert [item.label for item in KEY_BAR] == LABELS
    assert [item.id for item in KEY_BAR] == IDS
    assert len(set(IDS)) == len(IDS)
    assert tuple(item for group in KEY_BAR_GROUPS for item in group) == KEY_BAR
    assert [len(group) for group in KEY_BAR_GROUPS] == [3, 4, 2, 2, 5, 4, 2]
    kinds = [item.kind for item in KEY_BAR]
    assert kinds == (["key"] * 9 + ["modifier"] * 2 + ["key"] * 9 + ["action"] * 2)
    by_id = {item.id: item for item in KEY_BAR}
    assert by_id["key-shift-tab"].key == "shift-tab"
    assert by_id["key-ctrl-c"].key == "c" and by_id["key-ctrl-c"].mods == frozenset({MOD_CTRL})
    assert by_id["mod-ctrl"].key is None and by_id["mod-alt"].key is None
    assert by_id["action-paste"].key is None and by_id["action-select"].key is None
    assert by_id["key-backspace"].long_press == ("w", frozenset({MOD_CTRL}))
    assert by_id["key-up"].long_press == ("page-up", frozenset())
    assert by_id["key-down"].long_press == ("page-down", frozenset())
    assert by_id["key-left"].long_press == ("home", frozenset())
    assert by_id["key-right"].long_press == ("end", frozenset())
    assert all(
        item.long_press is None
        for item in KEY_BAR
        if item.id not in {"key-up", "key-down", "key-left", "key-right", "key-backspace"}
    )
    assert by_id["key-ctrl-c"].semantics == "Control C"
    assert isinstance(KEY_BAR[0], KeyBarItem)


def test_every_emitting_item_and_long_press_encodes() -> None:
    for item in KEY_BAR:
        if item.kind == "key":
            assert item.key is not None
            assert encode_key(item.key, item.mods) is not None, item.id
        if item.long_press is not None:
            key, mods = item.long_press
            assert encode_key(key, mods) is not None, item.id


def test_pinned_control_bytes() -> None:
    assert encode_key("c", frozenset({MOD_CTRL})) == b"\x03"
    assert encode_key("d", frozenset({MOD_CTRL})) == b"\x04"
    assert encode_key("z", frozenset({MOD_CTRL})) == b"\x1a"
    assert encode_key("l", frozenset({MOD_CTRL})) == b"\x0c"
    assert encode_key("r", frozenset({MOD_CTRL})) == b"\x12"
    assert encode_key("shift-tab", frozenset()) == b"\x1b[Z"
    assert encode_key("w", frozenset({MOD_CTRL})) == b"\x17"


def test_latch_one_shot_and_consume() -> None:
    latch = ModifierLatch()
    assert latch.state(MOD_CTRL) == "off"
    assert latch.armed() == frozenset()
    assert latch.tap(MOD_CTRL, 1000) == "once"
    assert latch.state(MOD_CTRL) == "once"
    assert latch.armed() == frozenset({MOD_CTRL})
    latch.consume()
    assert latch.state(MOD_CTRL) == "off"
    assert latch.armed() == frozenset()


def test_latch_double_tap_locks_and_consume_keeps_locked() -> None:
    latch = ModifierLatch()
    assert latch.tap(MOD_ALT, 1000) == "once"
    assert latch.tap(MOD_ALT, 1349) == "locked"
    assert latch.armed() == frozenset({MOD_ALT})
    latch.consume()
    assert latch.state(MOD_ALT) == "locked"
    assert latch.tap(MOD_ALT, 2000) == "off"
    assert latch.armed() == frozenset()


def test_latch_slow_second_tap_returns_to_off() -> None:
    latch = ModifierLatch()
    assert latch.tap(MOD_CTRL, 1000) == "once"
    assert latch.tap(MOD_CTRL, 1350) == "off"  # exactly 350 ms: not locked
    assert latch.armed() == frozenset()
    assert latch.tap(MOD_CTRL, 2000) == "once"
    assert latch.tap(MOD_CTRL, 2351) == "off"  # 351 ms: not locked


def test_latch_modifiers_are_independent_and_validated() -> None:
    latch = ModifierLatch()
    latch.tap(MOD_CTRL, 1000)
    latch.tap(MOD_ALT, 1000)
    assert latch.armed() == frozenset({MOD_CTRL, MOD_ALT})
    latch.consume()
    assert latch.armed() == frozenset()
    with pytest.raises(ValueError):
        latch.tap("super", 1000)
    with pytest.raises(ValueError):
        latch.state("super")


def test_width_class_boundaries() -> None:
    assert width_class(0) == "compact"
    assert width_class(390) == "compact"
    assert width_class(599) == "compact"
    assert width_class(599.9) == "compact"
    assert width_class(600) == "medium"
    assert width_class(768) == "medium"
    assert width_class(1023) == "medium"
    assert width_class(1023.9) == "medium"
    assert width_class(1024) == "expanded"
    assert width_class(1280) == "expanded"
