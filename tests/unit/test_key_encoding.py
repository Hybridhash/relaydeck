"""Exact key encodings, one-shot modifiers, and the
generation/target-bound key emission path.

Every encoding in `tests/fixtures/keys/encodings.json` (31 goldens, xterm2
keytab + Shift-Enter override) must match `encode_key` byte for byte.
Modifiers are one-shot: armed, consumed on success, preserved on failure,
and an interrupt discards pre-armed ones.
Emissions bind (target terminal, writer generation) at press time; stale
bindings fail closed at flush so no previous terminal receives a delayed key.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from relaydeck.client.features.sync.transport import TerminalSocket, TransportError
from relaydeck.client.features.terminal.controller import TerminalController
from relaydeck.client.features.terminal.keys import (
    MOD_ALT,
    MOD_CTRL,
    MOD_SHIFT,
    KeyInputError,
    KeyInputSession,
    bind_keys_session,
    encode_key,
    normalize_key_name,
    shifted_char,
)
from relaydeck.gateway import terminal_wire
from tests.support.host_harness import run

KEYS = Path(__file__).resolve().parent.parent / "fixtures" / "keys"

_FIXTURE_MODS = {"ctrl": MOD_CTRL, "shift": MOD_SHIFT, "alt": MOD_ALT}


def _fixture_key(entry: dict[str, Any]) -> tuple[str, frozenset[str]]:
    key = str(entry["key"])
    if key.startswith("key") and len(key) == 4:
        key = key[3:].lower()
    mods = frozenset(_FIXTURE_MODS[m] for m in entry["mods"])
    return key, mods


class _Writer:
    def __init__(self, generation: int | None = 7) -> None:
        self.generation = generation
        self.sent: list[bytes] = []

    def probe(self) -> int | None:
        return self.generation

    async def send(self, data: bytes) -> None:
        self.sent.append(data)

    def session(self, target_id: str = "term-a") -> KeyInputSession:
        return KeyInputSession(target_id=target_id, generation_probe=self.probe, sender=self.send)


# -- exact bytes vs goldens -----------------------------------------------------


def test_all_fixture_goldens_match_exactly() -> None:
    spec = json.loads((KEYS / "encodings.json").read_text())
    entries = spec["encodings"]
    assert len(entries) == 31
    for entry in entries:
        key, mods = _fixture_key(entry)
        assert encode_key(key, mods) == bytes.fromhex(entry["bytes_hex"]), entry["name"]


def test_shift_tab_and_shift_enter_spellings() -> None:
    assert encode_key("shift-tab") == b"\x1b[Z"
    assert encode_key("shiftTab") == b"\x1b[Z"
    assert encode_key("tab", frozenset({MOD_SHIFT})) == b"\x1b[Z"
    assert encode_key("shift-enter") == b"\n"
    assert encode_key("shiftEnter") == b"\n"
    assert encode_key("enter", frozenset({MOD_SHIFT})) == b"\n"
    # Redundant armed Shift on the shifted buttons resolves identically.
    assert encode_key("shift-tab", frozenset({MOD_SHIFT})) == b"\x1b[Z"
    assert encode_key("shift-enter", frozenset({MOD_SHIFT})) == b"\n"
    # But Ctrl may not join them.
    assert encode_key("shift-tab", frozenset({MOD_CTRL})) is None
    assert encode_key("shift-enter", frozenset({MOD_CTRL, MOD_SHIFT})) is None


def test_shift_character_mapping() -> None:
    pairs = {
        "`": "~",
        "1": "!",
        "2": "@",
        "9": "(",
        "0": ")",
        "-": "_",
        "=": "+",
        "[": "{",
        "]": "}",
        "\\": "|",
        ";": ":",
        "'": '"',
        ",": "<",
        ".": ">",
        "/": "?",
        "a": "A",
        "z": "Z",
        "C": "C",
        " ": " ",
    }
    for plain, shifted in pairs.items():
        assert shifted_char(plain) == shifted, plain
        assert encode_key(plain, frozenset({MOD_SHIFT})) == shifted.encode(), plain


def test_ctrl_c0_pairing() -> None:
    assert encode_key("c", frozenset({MOD_CTRL})) == b"\x03"
    assert encode_key("d", frozenset({MOD_CTRL})) == b"\x04"
    assert encode_key("z", frozenset({MOD_CTRL})) == b"\x1a"
    assert encode_key("a", frozenset({MOD_CTRL})) == b"\x01"
    assert encode_key("C", frozenset({MOD_CTRL})) == b"\x03"
    assert encode_key(" ", frozenset({MOD_CTRL})) == b"\x00"
    assert encode_key("[", frozenset({MOD_CTRL})) == b"\x1b"
    assert encode_key("?", frozenset({MOD_CTRL})) == b"\x7f"
    # Shift folds first: Ctrl+Shift+c is still ETX.
    assert encode_key("c", frozenset({MOD_CTRL, MOD_SHIFT})) == b"\x03"


def test_ctrl_rejects_digits_and_nonscii() -> None:
    assert encode_key("2", frozenset({MOD_CTRL})) is None
    assert encode_key("\u00e9", frozenset({MOD_CTRL})) is None
    assert encode_key(".", frozenset({MOD_CTRL})) is None


def test_alt_sequences_prefix_escape() -> None:
    assert encode_key("x", frozenset({MOD_ALT})) == b"\x1bx"
    assert encode_key("enter", frozenset({MOD_ALT})) == b"\x1b\r"
    assert encode_key("left", frozenset({MOD_ALT})) == b"\x1b\x1b[D"
    assert encode_key("f5", frozenset({MOD_ALT})) == b"\x1b\x1b[15~"
    assert encode_key("c", frozenset({MOD_CTRL, MOD_ALT})) == b"\x1b\x03"
    assert encode_key("x", frozenset({MOD_ALT, MOD_SHIFT})) == b"\x1bX"
    assert encode_key("tab", frozenset({MOD_ALT, MOD_SHIFT})) == b"\x1b\x1b[Z"


def test_unpinned_combos_return_none() -> None:
    assert encode_key("left", frozenset({MOD_CTRL})) is None
    assert encode_key("up", frozenset({MOD_SHIFT})) is None
    assert encode_key("home", frozenset({MOD_CTRL})) is None
    assert encode_key("f1", frozenset({MOD_SHIFT})) is None
    assert encode_key("page-up", frozenset({MOD_CTRL})) is None
    assert encode_key("delete", frozenset({MOD_ALT, MOD_SHIFT})) is None


def test_unknown_keys_and_modifiers_return_none() -> None:
    assert encode_key("") is None
    assert encode_key("nope-not-a-key") is None
    assert encode_key("f0") is None
    assert encode_key("f13") is None
    assert encode_key("F13") is None
    assert encode_key("ctrl") is None
    assert normalize_key_name("") is None
    assert normalize_key_name("f0") is None
    assert normalize_key_name("F12") == "f12"
    assert normalize_key_name("arrowLeft") == "left"
    assert normalize_key_name("forwardDelete") == "delete"
    assert normalize_key_name("C") == "C"
    assert encode_key("a", frozenset({"hyper"})) is None


# -- one-shot modifiers ----------------------------------------------------------


def test_key_aliases_are_case_insensitive() -> None:
    assert normalize_key_name("ShiftTab") == "shift-tab"
    assert normalize_key_name("SHIFTTAB") == "shift-tab"
    assert normalize_key_name("ArrowLeft") == "left"
    assert normalize_key_name("ARROWDOWN") == "down"
    assert normalize_key_name("ForwardDelete") == "delete"
    assert normalize_key_name("FORWARDDELETE") == "delete"
    assert normalize_key_name("PAGEUP") == "page-up"
    assert normalize_key_name("ESC") == "escape"
    assert encode_key("ShiftTab") == b"\x1b[Z"
    assert encode_key("ArrowLeft") == b"\x1b[D"
    # Single characters still resolve verbatim (case preserved).
    assert normalize_key_name("C") == "C"


def test_modifier_toggle_and_set() -> None:
    session = _Writer().session()
    assert session.pending_modifiers == frozenset()
    session.toggle_modifier(MOD_CTRL)
    assert session.is_modifier_armed(MOD_CTRL)
    session.toggle_modifier(MOD_CTRL)
    assert not session.is_modifier_armed(MOD_CTRL)
    session.set_modifier_armed(MOD_ALT, True)
    session.set_modifier_armed(MOD_SHIFT, True)
    assert session.pending_modifiers == frozenset({MOD_ALT, MOD_SHIFT})
    with pytest.raises(KeyInputError):
        session.toggle_modifier("hyper")


def test_press_consumes_armed_modifiers() -> None:
    async def scenario() -> None:
        writer = _Writer()
        session = writer.session()
        session.toggle_modifier(MOD_CTRL)
        emission = session.press_key("c")
        assert emission is not None
        assert emission.data == b"\x03"
        assert emission.label == "ctrl+c"
        assert session.pending_modifiers == frozenset()
        assert await session.flush() == 1
        assert writer.sent == [b"\x03"]

    run(scenario())


def test_unencodable_press_preserves_armed_set() -> None:
    writer = _Writer()
    session = writer.session()
    session.toggle_modifier(MOD_CTRL)
    assert session.press_key("left") is None
    assert session.pending_modifiers == frozenset({MOD_CTRL})
    assert session.outbox == ()
    # The armed Ctrl still applies to a later key the encoder can represent.
    assert session.press_key("c") is not None
    assert session.pending_modifiers == frozenset()


def test_press_key_lf_queues_raw_lf() -> None:
    async def scenario() -> None:
        writer = _Writer()
        session = writer.session()
        emission = session.press_key("\n")
        assert emission is not None
        assert emission.data == b"\n"  # raw LF, not the CR committed text yields
        assert await session.flush() == 1
        assert writer.sent == [b"\n"]

    run(scenario())


def test_dead_writer_press_preserves_armed_set() -> None:
    writer = _Writer(generation=None)
    session = writer.session()
    session.toggle_modifier(MOD_ALT)
    assert session.press_key("x") is None
    assert session.pending_modifiers == frozenset({MOD_ALT})
    assert session.outbox == ()


def test_physical_modifiers_union_with_armed() -> None:
    writer = _Writer()
    session = writer.session()
    session.toggle_modifier(MOD_SHIFT)
    emission = session.press_key("x", physical_modifiers=frozenset({MOD_ALT}))
    assert emission is not None
    assert emission.data == b"\x1bX"
    assert session.pending_modifiers == frozenset()


def test_interrupt_discards_pre_armed_mods() -> None:
    async def scenario() -> None:
        writer = _Writer()
        session = writer.session()
        session.set_modifier_armed(MOD_ALT, True)
        session.set_modifier_armed(MOD_SHIFT, True)
        emission = session.interrupt()
        assert emission is not None
        assert emission.data == b"\x03"
        assert session.pending_modifiers == frozenset()
        assert await session.flush() == 1
        assert writer.sent == [b"\x03"]

    run(scenario())


def test_interrupt_without_writer_keeps_mods_cleared_and_queues_nothing() -> None:
    writer = _Writer(generation=None)
    session = writer.session()
    session.set_modifier_armed(MOD_ALT, True)
    assert session.interrupt() is None
    assert session.pending_modifiers == frozenset()
    assert session.outbox == ()


# -- generation + target binding --------------------------------------------------


def test_press_binds_live_generation_and_flush_sends() -> None:
    async def scenario() -> None:
        writer = _Writer()
        session = writer.session()
        first = session.press_key("escape")
        assert first is not None
        assert (first.target_id, first.generation) == ("term-a", 7)
        assert await session.flush() == 1
        assert writer.sent == [b"\x1b"]

    run(scenario())


def test_generation_bump_drops_stale_queue() -> None:
    async def scenario() -> None:
        writer = _Writer()
        session = writer.session()
        assert session.press_key("up") is not None
        writer.generation = 8  # takeover / reconnect rebinding
        assert await session.flush() == 0
        assert writer.sent == []
        assert session.outbox == ()

    run(scenario())


def test_dead_writer_at_flush_drops_queue() -> None:
    async def scenario() -> None:
        writer = _Writer()
        session = writer.session()
        assert session.press_key("down") is not None
        writer.generation = None
        assert await session.flush() == 0
        assert writer.sent == []

    run(scenario())


def test_mid_flush_sender_failure_clears_before_send() -> None:
    async def scenario() -> None:
        sent: list[bytes] = []

        async def flaky(data: bytes) -> None:
            if sent:
                raise RuntimeError("boom")
            sent.append(data)

        session = KeyInputSession(target_id="term-a", generation_probe=lambda: 3, sender=flaky)
        assert session.press_key("a") is not None
        assert session.press_key("b") is not None
        with pytest.raises(RuntimeError, match="boom"):
            await session.flush()
        assert sent == [b"a"]  # first byte went out before the failure
        assert session.outbox == ()  # cleared before the first send: no retry
        assert await session.flush() == 0  # a second flush sends nothing
        assert sent == [b"a"]

    run(scenario())


def test_retarget_drops_queue_clears_mods_kills_token() -> None:
    async def scenario() -> None:
        first = _Writer()
        session = first.session()
        token = session.token
        session.toggle_modifier(MOD_CTRL)
        assert session.press_key("a") is not None
        second = _Writer(generation=11)
        session.retarget("term-b", second.probe, second.send)
        assert session.target_id == "term-b"
        assert session.pending_modifiers == frozenset()
        assert session.outbox == ()
        assert session.token != token
        # Stale-token callbacks and the old queue emit nothing anywhere.
        assert session.press_key("b", token=token) is None
        assert await session.flush() == 0
        assert first.sent == []
        assert second.sent == []
        # The new target works with a clean slate.
        assert session.press_key("b") is not None
        assert await session.flush() == 1
        assert second.sent == [b"b"]

    run(scenario())


def test_invalidate_kills_callbacks_and_queue() -> None:
    async def scenario() -> None:
        writer = _Writer()
        session = writer.session()
        token = session.token
        session.toggle_modifier(MOD_SHIFT)
        assert session.press_key("a") is not None
        session.invalidate()
        assert session.pending_modifiers == frozenset()
        assert session.press_key("b", token=token) is None
        assert await session.flush() == 0
        assert writer.sent == []

    run(scenario())


def test_cancel_outbox_sends_nothing() -> None:
    async def scenario() -> None:
        writer = _Writer()
        session = writer.session()
        assert session.press_key("a") is not None
        assert session.commit_text("bc") is not None
        assert session.cancel_outbox() == 2
        assert await session.flush() == 0
        assert writer.sent == []

    run(scenario())


# -- committed text -----------------------------------------------------------------


def test_commit_text_plain_and_multibyte() -> None:
    async def scenario() -> None:
        writer = _Writer()
        session = writer.session()
        assert session.commit_text("h\u00e9llo") is not None
        assert await session.flush() == 1
        assert writer.sent == ["h\u00e9llo".encode()]

    run(scenario())


def test_commit_lone_lf_sends_cr() -> None:
    async def scenario() -> None:
        writer = _Writer()
        session = writer.session()
        assert session.commit_text("\n") is not None
        assert session.commit_text("\r") is not None
        assert session.commit_text("a\nb") is not None
        assert await session.flush() == 3
        assert writer.sent == [b"\r", b"\r", b"a\nb"]

    run(scenario())


def test_commit_single_char_applies_armed_mods() -> None:
    async def scenario() -> None:
        writer = _Writer()
        session = writer.session()
        session.toggle_modifier(MOD_CTRL)
        assert session.commit_text("c") is not None
        assert session.pending_modifiers == frozenset()
        session.toggle_modifier(MOD_SHIFT)
        assert session.commit_text("\n") is not None  # shift+enter -> LF
        assert await session.flush() == 2
        assert writer.sent == [b"\x03", b"\n"]

    run(scenario())


def test_commit_unencodable_char_flows_and_keeps_mods() -> None:
    async def scenario() -> None:
        writer = _Writer()
        session = writer.session()
        session.toggle_modifier(MOD_CTRL)
        assert session.commit_text("\u00e9") is not None  # falls through
        assert session.pending_modifiers == frozenset({MOD_CTRL})
        assert session.commit_text("ab") is not None  # multi-char untouched
        assert session.pending_modifiers == frozenset({MOD_CTRL})
        assert await session.flush() == 2
        assert writer.sent == ["\u00e9".encode(), b"ab"]

    run(scenario())


def test_commit_empty_dead_writer_or_stale_token_queues_nothing() -> None:
    writer = _Writer(generation=None)
    session = writer.session()
    assert session.commit_text("") is None
    assert session.commit_text("x") is None
    writer.generation = 7
    token = session.token
    session.invalidate()
    assert session.commit_text("x", token=token) is None
    assert session.outbox == ()


# -- controller integration: sole current writer -------------------------------------


class _ScriptedSocket(TerminalSocket):
    def __init__(self) -> None:
        self.sent_bytes: list[bytes] = []
        self.sent_text: list[str] = []

    async def connect(self) -> None:
        pass

    async def send_bytes(self, data: bytes) -> None:
        self.sent_bytes.append(data)

    async def send_text(self, data: str) -> None:
        self.sent_text.append(data)

    async def receive(self) -> tuple[str, Any]:
        raise TransportError("host_offline", "eof")

    async def close(self) -> None:
        pass


def _controller(sock: _ScriptedSocket, mode: str, generation: int = 1) -> TerminalController:
    return TerminalController(
        socket_factory=lambda ticket: sock,
        lease_id="lease-1",
        generation=generation,
        mode=mode,
        on_bytes=lambda data: None,
    )


def test_controller_key_path_sends_text_frames() -> None:
    async def scenario() -> None:
        sock = _ScriptedSocket()
        controller = _controller(sock, "control")
        await controller.start("rdt_test")
        assert controller.key_generation() == 1
        session = bind_keys_session(controller)
        assert session.target_id == "lease-1"
        session.toggle_modifier(MOD_CTRL)
        assert session.press_key("c") is not None
        assert session.commit_text("ls") is not None
        assert await session.flush() == 2
        assert len(sock.sent_bytes) == 2
        first = terminal_wire.decode_input(sock.sent_bytes[0])
        assert (first.lease_id, first.generation, first.kind) == ("lease-1", 1, "text")
        assert first.input_seq == 1
        assert first.data == b"\x03"
        second = terminal_wire.decode_input(sock.sent_bytes[1])
        assert second.input_seq == 2
        assert second.data == b"ls"

    run(scenario())


def test_controller_key_path_view_lease_fails_closed() -> None:
    async def scenario() -> None:
        sock = _ScriptedSocket()
        controller = _controller(sock, "view")
        await controller.start("rdt_test")
        assert controller.key_generation() is None
        session = bind_keys_session(controller)
        assert session.press_key("escape") is None
        with pytest.raises(KeyInputError) as exc:
            await controller.send_key_bytes(b"\x1b")
        assert exc.value.code == "no_writer"
        assert sock.sent_bytes == []

    run(scenario())


def test_controller_key_path_closed_socket_fails_closed() -> None:
    async def scenario() -> None:
        sock = _ScriptedSocket()
        controller = _controller(sock, "control")
        assert controller.key_generation() is None
        with pytest.raises(KeyInputError) as exc:
            await controller.send_key_bytes(b"\x1b")
        assert exc.value.code == "no_writer"

    run(scenario())
