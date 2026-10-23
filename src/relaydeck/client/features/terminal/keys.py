"""Terminal key encodings and generation-bound key/text emission.

The encoder uses pinned xterm2 bytes over text frames because upstream key-combo
names cannot express the full navigation set. Unsupported combinations emit
nothing. Ctrl uses ASCII control pairing, Alt prefixes ESC, and Shift+Enter is
LF. A lone committed LF becomes CR; other committed text stays verbatim.

One KeyInputSession owns each detail's writer. It combines physical and pending
modifiers, consumes one-shot modifiers only after success, and rejects stale
target/generation tokens. Retarget and invalidate discard queued input and
modifiers; flush clears the queue before sending so uncertain writes cannot
replay. Mode routing, deduplication and IME state live in input_modes.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Any

import flet as ft

from relaydeck.client.components.appearance import chrome_style
from relaydeck.client.components.input_chrome import mode_button
from relaydeck.client.features.terminal.events import click
from relaydeck.client.theme import MIN_TOUCH, palette

if TYPE_CHECKING:
    from relaydeck.client.features.terminal.controller import TerminalController

MOD_CTRL = "ctrl"
MOD_ALT = "alt"
MOD_SHIFT = "shift"
MODIFIERS: frozenset[str] = frozenset({MOD_CTRL, MOD_ALT, MOD_SHIFT})

# Canonical special-key names (lowercase). Single characters address the
# the character case and are matched case-sensitively.
SHIFT_TAB = "shift-tab"
SHIFT_ENTER = "shift-enter"

_SPECIAL_BYTES: dict[str, bytes] = {
    "escape": b"\x1b",
    "tab": b"\t",
    "enter": b"\r",
    "backspace": b"\x7f",
    "left": b"\x1b[D",
    "up": b"\x1b[A",
    "down": b"\x1b[B",
    "right": b"\x1b[C",
    "home": b"\x1b[H",
    "end": b"\x1b[F",
    "insert": b"\x1b[2~",
    "delete": b"\x1b[3~",
    "f1": b"\x1bOP",
    "f2": b"\x1bOQ",
    "f3": b"\x1bOR",
    "f4": b"\x1bOS",
    "f5": b"\x1b[15~",
    "f6": b"\x1b[17~",
    "f7": b"\x1b[18~",
    "f8": b"\x1b[19~",
    "f9": b"\x1b[20~",
    "f10": b"\x1b[21~",
    "f11": b"\x1b[23~",
    "f12": b"\x1b[24~",
    "page-up": b"\x1b[5~",
    "page-down": b"\x1b[6~",
}

_SHIFTED_SPECIAL: dict[str, bytes] = {
    "tab": b"\x1b[Z",  # CSI Z (keytab shift-tab)
    "enter": b"\n",  # Ctrl-J (Shift+Enter override)
}

_KEY_ALIASES: dict[str, str] = {
    # Quick-key case names.
    "esc": "escape",
    "shiftTab": SHIFT_TAB,
    "shiftEnter": SHIFT_ENTER,
    "arrowLeft": "left",
    "arrowUp": "up",
    "arrowDown": "down",
    "arrowRight": "right",
    "forwardDelete": "delete",
    "forward-delete": "delete",
    "pageUp": "page-up",
    "pageDown": "page-down",
    # Common spellings.
    "return": "enter",
    "bs": "backspace",
    "del": "delete",
    "pgup": "page-up",
    "pgdn": "page-down",
    "pagedown": "page-down",
    "pageup": "page-up",
    "space": " ",
}

# Lowercased alias index so case-name spellings resolve
# case-insensitively ("ShiftTab", "ARROWLEFT"); values agree on collision.
_KEY_ALIASES_LOWER: dict[str, str] = {key.lower(): value for key, value in _KEY_ALIASES.items()}

_UNSHIFTED = "`1234567890-=[]\\;',./"
_SHIFTED = '~!@#$%^&*()_+{}|:"<>?'

_CTRL_PUNCTUATION: dict[str, int] = {
    "@": 0x00,
    "[": 0x1B,
    "\\": 0x1C,
    "]": 0x1D,
    "^": 0x1E,
    "_": 0x1F,
    "?": 0x7F,
}

QUICK_KEY_NAMES: tuple[str, ...] = (
    "escape",
    "tab",
    SHIFT_TAB,
    SHIFT_ENTER,
    "left",
    "up",
    "down",
    "right",
    "enter",
    "backspace",
    "home",
    "end",
    "insert",
    "delete",
    "page-up",
    "page-down",
    "f1",
    "f2",
    "f3",
    "f4",
    "f5",
    "f6",
    "f7",
    "f8",
    "f9",
    "f10",
    "f11",
    "f12",
)


def normalize_key_name(token: str) -> str | None:
    """Resolve a key token to its canonical name.

    Single characters are character keys (case preserved: typing "C" sends
    "C"). Anything else is matched case-insensitively against the special
    set, case-name aliases, and `F1`-`F12` (the function range;
    `F0`/`F13+` are rejected unlike the wider Herdr key grammar). Fixture
    `key<X>` letter spellings resolve to the letter. Unknown tokens and
    empty strings return None and therefore emit nothing.
    """
    if len(token) == 1:
        # Single characters are character keys verbatim (this check runs
        # before strip so the space key survives).
        return token
    text = token.strip()
    if len(text) == 1:
        return text
    lowered = text.lower()
    if lowered in _SPECIAL_BYTES or lowered in (SHIFT_TAB, SHIFT_ENTER):
        return lowered
    alias = _KEY_ALIASES_LOWER.get(lowered)
    if alias is not None:
        return alias
    if lowered.startswith("f") and lowered[1:].isdigit() and len(lowered) <= 3:
        number = int(lowered[1:])
        if 1 <= number <= 12:
            return f"f{number}"
        return None
    if len(text) == 4 and text.startswith("key") and text[3:].isalpha():
        return text[3:]
    return None


def shifted_char(char: str) -> str:
    """US-layout shift
    pairing, otherwise uppercase. Single characters only."""
    if char in _UNSHIFTED:
        return _SHIFTED[_UNSHIFTED.index(char)]
    return char.upper()


def _control_code(char: str) -> int | None:
    """C0 control pairing for Ctrl+character: ASCII letters to 0x01-0x1A,
    space to NUL, and the standard `@[\\]^_?` punctuation pairing. Digits,
    other punctuation, and non-ASCII return None (unencodable)."""
    if char == " ":
        return 0x00
    upper = char.upper()
    if len(upper) == 1 and "A" <= upper <= "Z":
        return ord(upper) - 64
    return _CTRL_PUNCTUATION.get(char)


def encode_key(name: str, modifiers: frozenset[str] = frozenset()) -> bytes | None:
    """Encode one key plus modifiers to exact terminal bytes.

    Returns None for unknown keys, unknown modifier tokens, and combos no
    fixture pins (Ctrl/Shift with arrows, Home/End, Insert/Delete,
    PgUp/PgDn, function keys; Ctrl with digits or non-ASCII). Callers must
    preserve one-shot modifiers on None, as on any failed send.
    """
    key = normalize_key_name(name)
    if key is None:
        return None
    mods = set(modifiers)
    if not mods <= MODIFIERS:
        return None
    if key in (SHIFT_TAB, SHIFT_ENTER):
        # Shift-tab/shift-enter buttons resolve through the shifted form;
        # only an armed Shift may redundantly accompany them.
        if mods - {MOD_SHIFT}:
            return None
        key = "tab" if key == SHIFT_TAB else "enter"
        mods = {MOD_SHIFT}
    if key in _SPECIAL_BYTES:
        return _encode_special(key, mods)
    return _encode_character(key, mods)


def _encode_special(key: str, mods: set[str]) -> bytes | None:
    base = _SPECIAL_BYTES[key]
    if not mods:
        return base
    rest = set(mods)
    if MOD_SHIFT in rest:
        shifted = _SHIFTED_SPECIAL.get(key)
        if shifted is None:
            return None
        base = shifted
        rest.discard(MOD_SHIFT)
    if rest == {MOD_ALT}:
        return b"\x1b" + base
    if not rest:
        return base
    return None


def _encode_character(char: str, mods: set[str]) -> bytes | None:
    rest = set(mods)
    if MOD_SHIFT in rest:
        char = shifted_char(char)
        rest.discard(MOD_SHIFT)
    if MOD_CTRL in rest:
        code = _control_code(char)
        if code is None:
            return None
        data = bytes((code,))
        rest.discard(MOD_CTRL)
    else:
        data = char.encode("utf-8")
    if rest == {MOD_ALT}:
        return b"\x1b" + data
    if not rest:
        return data
    return None


class KeyInputError(Exception):
    """Keys/direct-text input failure. `no_writer` means no live control
    writer: an explicit failure with no hidden fallback."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class KeyEmission:
    """One bound emission: exact bytes for a target terminal at a writer
    generation. `flush` sends only emissions still bound to the live
    target+generation; anything else is dropped, never delayed."""

    target_id: str
    generation: int
    data: bytes
    label: str


def _label(key: str, mods: set[str]) -> str:
    parts = sorted(mods)
    parts.append(key)
    return "+".join(parts)


class KeyInputSession:
    """One-shot modifiers plus generation/target-bound key/text emissions.

    `generation_probe` returns the current writer generation, or None when
    no writer is live. `sender` transmits one chunk of exact bytes through
    the sole current writer. Every emission binds (`target_id`, generation)
    at press time; `flush` revalidates both and drops stale emissions with
    nothing sent. `retarget` (selection/takeover) and `invalidate`
    (unmount/teardown) drop queued input, clear modifiers, and kill the
    callback token so earlier callbacks emit nothing.
    """

    def __init__(
        self,
        *,
        target_id: str,
        generation_probe: Callable[[], int | None],
        sender: Callable[[bytes], Awaitable[None]],
    ) -> None:
        self._target_id = target_id
        self._probe = generation_probe
        self._sender = sender
        self._pending: set[str] = set()
        self._outbox: list[KeyEmission] = []
        self._token = 0

    @property
    def target_id(self) -> str:
        return self._target_id

    @property
    def token(self) -> int:
        """Callback token: callbacks capture it at bind time and pass it
        back; a mismatch means unmount/retarget happened since."""
        return self._token

    @property
    def pending_modifiers(self) -> frozenset[str]:
        return frozenset(self._pending)

    @property
    def outbox(self) -> tuple[KeyEmission, ...]:
        return tuple(self._outbox)

    # -- one-shot modifiers ----------------------------------------------
    def is_modifier_armed(self, modifier: str) -> bool:
        return modifier in self._pending

    def set_modifier_armed(self, modifier: str, armed: bool) -> None:
        if modifier not in MODIFIERS:
            raise KeyInputError("bad_modifier", f"unknown modifier {modifier!r}")
        if armed:
            self._pending.add(modifier)
        else:
            self._pending.discard(modifier)

    def toggle_modifier(self, modifier: str) -> None:
        self.set_modifier_armed(modifier, not self.is_modifier_armed(modifier))

    # -- emissions ------------------------------------------------------------
    def _live(self, token: int | None) -> int | None:
        if token is not None and token != self._token:
            return None
        return self._probe()

    def press_key(
        self,
        name: str,
        *,
        physical_modifiers: frozenset[str] = frozenset(),
        token: int | None = None,
    ) -> KeyEmission | None:
        """Encode a tools/dock key (union of armed + physical modifiers) and
        queue it bound to the live target+generation. Stale token, dead
        writer, and unencodable combos return None with the armed set
        preserved (blocked or failed keys never consume).
        `press_key("\\n")` queues raw LF, not CR, and no dock button emits it."""
        generation = self._live(token)
        if generation is None:
            return None
        mods = self._pending | set(physical_modifiers)
        if not mods <= MODIFIERS:
            return None
        data = encode_key(name, frozenset(mods))
        if data is None:
            return None
        key = normalize_key_name(name)
        assert key is not None  # encode_key succeeded, so the name resolved  # noqa: S101
        emission = KeyEmission(self._target_id, generation, data, _label(key, mods))
        self._outbox.append(emission)
        self._pending.clear()
        return emission

    def commit_text(self, text: str, *, token: int | None = None) -> KeyEmission | None:
        """Queue committed (post-IME) text bound to the live target.

        A single character with armed modifiers encodes as a character key
        (LF/CR as Enter); when that encoding
        fails the original text still flows and the armed set stays. Lone LF
        without modifiers is sent as CR.
        Empty text, stale tokens, and dead writers queue nothing.
        """
        if not text:
            return None
        generation = self._live(token)
        if generation is None:
            return None
        if self._pending and len(text) == 1:
            key_name = "enter" if text in ("\n", "\r") else text
            data = encode_key(key_name, frozenset(self._pending))
            if data is not None:
                emission = KeyEmission(
                    self._target_id, generation, data, _label(key_name, set(self._pending))
                )
                self._outbox.append(emission)
                self._pending.clear()
                return emission
        payload = b"\r" if text == "\n" else text.encode("utf-8")
        emission = KeyEmission(self._target_id, generation, payload, f"text:{len(text)}")
        self._outbox.append(emission)
        return emission

    def commit_bytes(self, data: bytes, *, token: int | None = None) -> KeyEmission | None:
        """Queue exact engine-encoded bytes bound to the live target.

        The Dart/xterm2 physical-key path (protocol ORIGIN_KEY) and the
        blocked-composer-adjacent raw paths arrive pre-encoded: modifiers
        were already applied engine-side, so the armed set is neither
        consulted nor consumed. Empty data, stale tokens, and dead
        writers queue nothing.
        """
        if not data:
            return None
        generation = self._live(token)
        if generation is None:
            return None
        emission = KeyEmission(self._target_id, generation, data, f"raw:{len(data)}")
        self._outbox.append(emission)
        return emission

    def interrupt(self, *, token: int | None = None) -> KeyEmission | None:
        """Control-only interrupt: pre-armed
        Alt/Shift are discarded so the chord cannot become Ctrl+Alt+C."""
        if token is not None and token != self._token:
            return None
        self._pending.clear()
        generation = self._probe()
        if generation is None:
            return None
        data = encode_key("c", frozenset({MOD_CTRL}))
        assert data == b"\x03"  # C0 pairing fixture  # noqa: S101
        emission = KeyEmission(self._target_id, generation, data, "ctrl+c")
        self._outbox.append(emission)
        return emission

    async def flush(self) -> int:
        """Send queued emissions still bound to the live target+generation.

        Stale emissions and a dead writer fail closed: the queue is dropped
        with nothing sent, so no previous terminal ever receives a delayed
        key. The queue is cleared before the first byte so a mid-send
        failure cannot be retried implicitly (no blind replay).
        Returns the number of emissions sent before any failure.
        """
        pending = self._outbox
        self._outbox = []
        current = self._probe()
        if current is None:
            return 0
        sent = 0
        for emission in pending:
            if emission.target_id != self._target_id or emission.generation != current:
                continue
            await self._sender(emission.data)
            sent += 1
        return sent

    def cancel_outbox(self) -> int:
        """Drop queued emissions. Provably sends nothing."""
        count = len(self._outbox)
        self._outbox.clear()
        return count

    # -- target lifecycle -------------------------------------------------------
    def retarget(
        self,
        target_id: str,
        generation_probe: Callable[[], int | None],
        sender: Callable[[bytes], Awaitable[None]],
    ) -> None:
        """Bind a new target terminal: queued input is dropped, modifiers
        cleared, and the callback token killed."""
        self._target_id = target_id
        self._probe = generation_probe
        self._sender = sender
        self._pending.clear()
        self._outbox.clear()
        self._token += 1

    def invalidate(self) -> None:
        """Unmount/teardown: drop queued input, clear modifiers, and kill
        the callback token. Later callbacks with the old token emit nothing."""
        self._pending.clear()
        self._outbox.clear()
        self._token += 1


def bind_keys_session(
    controller: TerminalController, target_id: str | None = None
) -> KeyInputSession:
    """Wire a session to the sole current writer: the controller's live
    control-lease generation probe plus its exact-bytes sender. The target
    defaults to the controller's lease identity."""
    return KeyInputSession(
        target_id=target_id if target_id is not None else controller.lease_id,
        generation_probe=controller.key_generation,
        sender=controller.send_key_bytes,
    )


# -- declarative tools-keys dock -----------------------------------------------

KEYS_DOCK_KEY = "tools-keys-dock"
KEYS_TITLE_KEY = "tools-keys-title"
KEYS_MOD_ROW_KEY = "tools-keys-modifiers"
KEYS_DISMISS_KEY = "tools-keys-dismiss"

DOCK_TITLE = "Keys"
TEXT_LABEL = "Text"

_MOD_LABELS: dict[str, str] = {MOD_CTRL: "Ctrl", MOD_ALT: "Alt", MOD_SHIFT: "Shift"}
_MOD_ACCESSIBILITY: dict[str, str] = {
    MOD_CTRL: "Control modifier",
    MOD_ALT: "Option modifier",
    MOD_SHIFT: "Shift modifier",
}

DOCK_KEYS: tuple[tuple[str, str, str], ...] = (
    # (canonical name, button label, accessibility label). Accessibility
    # labels are the reference quick-key names.
    # Use ASCII labels where the bundled font lacks key symbols.
    ("escape", "Esc", "Escape"),
    ("tab", "Tab", "Tab"),
    (SHIFT_TAB, "Shift Tab", "Shift Tab"),
    (SHIFT_ENTER, "Shift Enter", "Shift Enter"),
    ("left", "\u2190", "Left Arrow"),
    ("up", "\u2191", "Up Arrow"),
    ("down", "\u2193", "Down Arrow"),
    ("right", "\u2192", "Right Arrow"),
    ("enter", "Enter", "Enter"),
    ("backspace", "Backspace", "Backspace"),
    ("home", "Home", "Home"),
    ("end", "End", "End"),
    ("insert", "Ins", "Insert"),
    ("delete", "Del", "Forward Delete"),
    ("page-up", "PgUp", "Page Up"),
    ("page-down", "PgDn", "Page Down"),
    ("f1", "F1", "F1"),
    ("f2", "F2", "F2"),
    ("f3", "F3", "F3"),
    ("f4", "F4", "F4"),
    ("f5", "F5", "F5"),
    ("f6", "F6", "F6"),
    ("f7", "F7", "F7"),
    ("f8", "F8", "F8"),
    ("f9", "F9", "F9"),
    ("f10", "F10", "F10"),
    ("f11", "F11", "F11"),
    ("f12", "F12", "F12"),
)

_DOCK_ROWS: tuple[tuple[str, ...], ...] = (
    ("escape", "tab", SHIFT_TAB, SHIFT_ENTER),
    ("home", "end", "insert", "delete", "page-up", "page-down"),
    ("f1", "f2", "f3", "f4", "f5", "f6"),
    ("f7", "f8", "f9", "f10", "f11", "f12"),
    ("left", "up", "down", "right", "enter", "backspace"),
)

_DOCK_LABELS: dict[str, tuple[str, str]] = {
    name: (label, access) for name, label, access in DOCK_KEYS
}


def key_button_key(name: str) -> str:
    return f"tools-key-{name}"


def modifier_button_key(modifier: str) -> str:
    return f"tools-mod-{modifier}"


def modifier_button_label(modifier: str, armed: bool) -> str:
    """Armed modifiers show a filled marker."""
    label = _MOD_LABELS[modifier]
    return f"\u25cf {label}" if armed else label


@ft.component
def ToolsKeysDock(
    armed: frozenset[str],
    enabled: bool,
    on_key: Callable[[str], Any],
    on_toggle_modifier: Callable[[str], Any],
    on_dismiss: Callable[[], Any],
    presentation: str = "full",
    dark: bool = True,
    max_height: float = 192,
    dedupe_inline: bool = False,
    compact: bool = False,
) -> Any:
    """Fully controlled tools keyboard: immutable props plus callbacks, no
    sockets, no timers, no direct session access. Keys target the live
    terminal even when a composer draft exists; the session (not this
    component) binds generations and clears one-shot modifiers.

    `dedupe_inline` (tablet/desktop agent dock): show only keys missing
    from the inline key row, as wrapping inline-style pills with a
    segmented Agent/Terminal switch. The default keeps the legacy
    stretched grid (phone and existing callers unchanged).

    `compact` (inline key row hidden, small overlay): same wrapping pills,
    but every key of the page is shown because nothing is inline."""
    page, set_page = ft.use_state("agent" if presentation == "agent" else "terminal")
    tokens = palette(dark)
    if presentation not in ("full", "agent", "terminal"):
        raise ValueError("unknown tools presentation")
    if presentation != "full":
        # The primary row, in the same order as the phone/expanded key row.
        agent_keys = (
            "escape",
            "tab",
            "shift-tab",
            "left",
            "up",
            "down",
            "right",
            "backspace",
            "enter",
        )
        names = (
            agent_keys
            if presentation == "agent" and page == "agent"
            else tuple(name for name, _, _ in DOCK_KEYS)
        )
        if dedupe_inline or compact:
            # Deferred import: terminal_window imports key_bar/scrolling,
            # which import this module (cycle at module import time).
            from relaydeck.client.components.design_system.terminal_window import (
                KEY_ROW,
                KeyButton,
                PillButton,
                SegmentedSwitch,
            )

            inline = frozenset() if compact else frozenset(name for name, _, _ in KEY_ROW)
            extra = tuple(name for name, _, _ in DOCK_KEYS if name not in inline)
            if presentation == "agent" and page == "agent":
                # The agent quick set duplicates the inline row key for
                # key; fall back to the same deduped set so the page is
                # never empty (modifiers stay terminal-only below).
                names = tuple(name for name in agent_keys if name not in inline) or extra
            else:
                names = extra
            # Flet 1.0.3: design-system buttons directly inside a Wrap break
            # layout (grey error box). Like KeyRow, the Wrap holds plain
            # tight Row groups; the buttons live inside those groups.
            buttons = [
                KeyButton(name, _DOCK_LABELS[name][0], None, on_key, enabled) for name in names
            ]
            groups: list[Any] = [
                ft.Row(tight=True, spacing=6, controls=buttons[i : i + 4])
                for i in range(0, len(buttons), 4)
            ]
            grid: list[Any] = [
                ft.Row(
                    key="tools-keys-deduped", wrap=True, spacing=10, run_spacing=8, controls=groups
                )
            ]
            if page == "terminal" or presentation == "terminal":
                grid.append(
                    ft.Row(
                        key=KEYS_MOD_ROW_KEY,
                        tight=True,
                        spacing=6,
                        controls=[
                            PillButton(
                                modifier_button_label(mod, mod in armed),
                                partial(on_toggle_modifier, mod),
                                key=modifier_button_key(mod),
                                tooltip=_MOD_ACCESSIBILITY[mod],
                                disabled=not enabled,
                                selected=mod in armed,
                            )
                            for mod in (MOD_CTRL, MOD_ALT, MOD_SHIFT)
                        ],
                    )
                )
            grid.append(PillButton(TEXT_LABEL, on_dismiss, key=KEYS_DISMISS_KEY))
            tabs = (
                [
                    SegmentedSwitch(
                        (
                            ("agent", "Agent", "tools-page-agent"),
                            ("terminal", "Terminal", "tools-page-terminal"),
                        ),
                        page,
                        set_page,
                        key="tools-page-switch",
                    )
                ]
                if presentation == "agent"
                else []
            )
        else:
            grid = []
            for offset in range(0, len(names), 3):
                grid.append(
                    ft.Row(
                        spacing=6,
                        controls=[
                            ft.Button(
                                _DOCK_LABELS[name][0],
                                key=key_button_key(name),
                                tooltip=_DOCK_LABELS[name][1],
                                disabled=not enabled,
                                height=MIN_TOUCH,
                                expand=True,
                                style=chrome_style(dark),
                                on_click=click(on_key, name),
                            )
                            for name in names[offset : offset + 3]
                        ],
                    )
                )
            if page == "terminal" or presentation == "terminal":
                grid.append(
                    ft.Row(
                        spacing=6,
                        controls=[
                            ft.Button(
                                modifier_button_label(mod, mod in armed),
                                key=modifier_button_key(mod),
                                tooltip=_MOD_ACCESSIBILITY[mod],
                                disabled=not enabled,
                                height=MIN_TOUCH,
                                expand=True,
                                style=chrome_style(dark),
                                on_click=click(on_toggle_modifier, mod),
                            )
                            for mod in (MOD_CTRL, MOD_ALT, MOD_SHIFT)
                        ],
                    )
                )
            grid.append(
                ft.Button(
                    TEXT_LABEL,
                    key=KEYS_DISMISS_KEY,
                    height=MIN_TOUCH,
                    style=chrome_style(dark),
                    on_click=click(on_dismiss),
                )
            )
            tabs = (
                [
                    ft.Row(
                        spacing=6,
                        controls=[
                            mode_button(
                                "Agent",
                                "tools-page-agent",
                                page == "agent",
                                lambda e: set_page("agent"),
                                dark,
                            ),
                            mode_button(
                                "Terminal",
                                "tools-page-terminal",
                                page == "terminal",
                                lambda e: set_page("terminal"),
                                dark,
                            ),
                        ],
                    )
                ]
                if presentation == "agent"
                else []
            )
        if compact and not dedupe_inline:
            # Overlay presentation: natural height; the overlay scrolls.
            return ft.Container(
                key=KEYS_DOCK_KEY,
                bgcolor=tokens["surface"],
                padding=6,
                content=ft.Column(
                    key="tools-grid-scroll", tight=True, spacing=6, controls=[*grid, *tabs]
                ),
            )
        return ft.Container(
            key=KEYS_DOCK_KEY,
            bgcolor=tokens["surface"],
            height=max_height if dedupe_inline or compact else max(48, max_height),
            padding=6,
            content=ft.Column(
                spacing=6,
                controls=[
                    ft.Column(
                        key="tools-grid-scroll",
                        expand=True,
                        scroll=ft.ScrollMode.AUTO,
                        spacing=6,
                        controls=grid,
                    ),
                    *tabs,
                ],
            ),
        )
    rows: list[Any] = [ft.Text(DOCK_TITLE, key=KEYS_TITLE_KEY, color=tokens["text"])]
    for row in _DOCK_ROWS:
        controls: list[Any] = []
        for name in row:
            label, access = _DOCK_LABELS[name]
            controls.append(
                ft.Button(
                    label,
                    key=key_button_key(name),
                    tooltip=access,
                    disabled=not enabled,
                    height=MIN_TOUCH,
                    style=chrome_style(dark),
                    on_click=click(on_key, name),
                )
            )
        rows.append(ft.Row(key=f"tools-keys-row-{row[0]}", wrap=True, controls=controls))
    mod_controls: list[Any] = []
    for modifier in (MOD_CTRL, MOD_ALT, MOD_SHIFT):
        mod_controls.append(
            ft.Button(
                modifier_button_label(modifier, modifier in armed),
                key=modifier_button_key(modifier),
                tooltip=_MOD_ACCESSIBILITY[modifier],
                disabled=not enabled,
                height=MIN_TOUCH,
                style=chrome_style(dark),
                on_click=click(on_toggle_modifier, modifier),
            )
        )
    mod_controls.append(
        ft.Button(
            TEXT_LABEL,
            key=KEYS_DISMISS_KEY,
            height=MIN_TOUCH,
            style=chrome_style(dark),
            on_click=click(on_dismiss),
        )
    )
    rows.append(ft.Row(key=KEYS_MOD_ROW_KEY, wrap=True, controls=mod_controls))
    return ft.Column(key=KEYS_DOCK_KEY, controls=rows)


__all__ = [
    "DOCK_KEYS",
    "DOCK_TITLE",
    "KEYS_DISMISS_KEY",
    "KEYS_DOCK_KEY",
    "KEYS_MOD_ROW_KEY",
    "KEYS_TITLE_KEY",
    "MODIFIERS",
    "MOD_ALT",
    "MOD_CTRL",
    "MOD_SHIFT",
    "QUICK_KEY_NAMES",
    "SHIFT_ENTER",
    "SHIFT_TAB",
    "TEXT_LABEL",
    "KeyEmission",
    "KeyInputError",
    "KeyInputSession",
    "ToolsKeysDock",
    "bind_keys_session",
    "encode_key",
    "key_button_key",
    "modifier_button_key",
    "modifier_button_label",
    "normalize_key_name",
    "shifted_char",
]
