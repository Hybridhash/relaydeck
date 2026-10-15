"""Independent workflow and keyboard modes with committed-text routing.

Composer edits stay in the draft; Direct and Shell use the generation-bound
writer. Mode switches preserve drafts and IME composition. Keydown/text dedupe
suppresses only the matching text echo. Send requires completed composition.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from relaydeck.client.features.composer.domain import INPUT_MODE_TOOLS_KEYS
from relaydeck.client.features.terminal.keys import KeyInputSession

if TYPE_CHECKING:
    from relaydeck.client.features.composer.domain import ComposerStore

WORKFLOW_AGENT_COMPOSER = "agent-composer"
WORKFLOW_AGENT_DIRECT = "agent-direct"
WORKFLOW_SHELL = "shell"
WORKFLOWS: tuple[str, ...] = (
    WORKFLOW_AGENT_COMPOSER,
    WORKFLOW_AGENT_DIRECT,
    WORKFLOW_SHELL,
)

KEYBOARD_TEXT = "text"
KEYBOARD_KEYS = "keys"
KEYBOARDS: tuple[str, ...] = (KEYBOARD_TEXT, KEYBOARD_KEYS)

AXIS_LABELS: dict[tuple[str, str], str] = {
    (WORKFLOW_AGENT_COMPOSER, KEYBOARD_TEXT): "Agent Composer with text keyboard",
    (WORKFLOW_AGENT_COMPOSER, KEYBOARD_KEYS): "Agent Composer with terminal tools",
    (WORKFLOW_AGENT_DIRECT, KEYBOARD_TEXT): "Agent Direct Input with text keyboard",
    (WORKFLOW_AGENT_DIRECT, KEYBOARD_KEYS): "Agent Direct Input with terminal tools",
    (WORKFLOW_SHELL, KEYBOARD_TEXT): "Shell Text",
    (WORKFLOW_SHELL, KEYBOARD_KEYS): "Shell Keys",
}

ROUTE_DRAFT = "draft"
ROUTE_PTY = "pty"
ROUTE_DROPPED = "dropped"
ROUTE_NONE = "none"

KEY_SEND_KEY = "send-key"
KEY_HOLD = "hold"
KEY_DROP = "drop"

TEXT_SEND = "send-text"
TEXT_SUPPRESSED = "suppressed"
TEXT_DROP = "drop"

IME_IDLE = "idle"
IME_COMPOSING = "composing"


class InputModeState:
    """Workflow x keyboard mode.

    Agent workflows bind the screen's `ComposerStore`; shells bind none
    (no composer, no draft). Switching workflows or keyboards never calls
    into the draft, so unsent (multiline) drafts survive byte-identically;
    the only store write happens when leaving the tools-keys
    surface calls `store.leave_tools_keys()`. Entering tools never touches
    the store: `input_mode == "tools-keys"` remains the blocked-delivery
    handoff, honored here via `sync_from_store`.
    """

    def __init__(
        self,
        *,
        workflow: str = WORKFLOW_AGENT_COMPOSER,
        keyboard: str = KEYBOARD_TEXT,
        store: ComposerStore | None = None,
    ) -> None:
        if workflow not in WORKFLOWS:
            raise ValueError(f"unknown workflow {workflow!r}")
        if keyboard not in KEYBOARDS:
            raise ValueError(f"unknown keyboard {keyboard!r}")
        self._workflow = workflow
        self._keyboard = keyboard
        self._store = store

    @property
    def workflow(self) -> str:
        return self._workflow

    @property
    def keyboard(self) -> str:
        return self._keyboard

    @property
    def store(self) -> ComposerStore | None:
        return self._store

    @property
    def axis(self) -> str:
        """Axis label for the current combination."""
        return AXIS_LABELS[(self._workflow, self._keyboard)]

    @property
    def text_edits_draft(self) -> bool:
        """Normal typing edits only the draft in Composer mode and goes to
        the PTY in Direct Input and Shell modes."""
        return self._workflow == WORKFLOW_AGENT_COMPOSER

    def set_workflow(self, workflow: str) -> None:
        """Switch workflows, preserving the hidden draft and any active IME
        composition by construction: no store, draft, or IME calls happen
        here, and nothing is autosent or cleared."""
        if workflow not in WORKFLOWS:
            raise ValueError(f"unknown workflow {workflow!r}")
        self._workflow = workflow

    def set_keyboard(self, keyboard: str) -> bool:
        """Switch Text/Keys, preserving the hidden draft by construction.

        Leaving the tools-keys surface with a bound agent store calls
        `store.leave_tools_keys()`; returns True when the
        reset happened. Entering tools never touches the store.
        """
        if keyboard not in KEYBOARDS:
            raise ValueError(f"unknown keyboard {keyboard!r}")
        leaving = self._keyboard == KEYBOARD_KEYS and keyboard == KEYBOARD_TEXT
        self._keyboard = keyboard
        if leaving and self._store is not None:
            return self._store.leave_tools_keys()
        return False

    def sync_from_store(self) -> bool:
        """Honor a blocked-delivery handoff: when the bound store reports
        tools-keys, latch the keyboard there. Never latches away from keys
        (Direct-keys keeps a composer-mode store), and shells (no store)
        never sync. Returns True when the keyboard latched."""
        if self._store is None:
            return False
        if self._store.input_mode != INPUT_MODE_TOOLS_KEYS:
            return False
        if self._keyboard == KEYBOARD_KEYS:
            return False
        self._keyboard = KEYBOARD_KEYS
        return True


def route_committed_text(
    state: InputModeState,
    *,
    store: ComposerStore | None,
    session: KeyInputSession,
    text: str,
    token: int | None = None,
) -> str:
    """Route committed text by workflow: Composer appends to
    the draft (the PTY sender is untouched); Direct/Shell bind to the live
    writer generation. Returns `draft`, `pty`, or `dropped` (dead writer,
    stale token, or empty text: nothing queued)."""
    if not text:
        # Empty text queues nothing on either path (a composer insert would
        # be a no-op append), so both report `dropped`.
        return ROUTE_DROPPED
    if state.text_edits_draft:
        if store is None:
            return ROUTE_DROPPED
        store.insert_into_draft(text)
        return ROUTE_DRAFT
    emission = session.commit_text(text, token=token)
    return ROUTE_PTY if emission is not None else ROUTE_DROPPED


def submit_allowed(ime: ImeState) -> bool:
    """Composer Send is gated on IME completion: while a
    composition is active the draft must not submit."""
    return not ime.is_composing


def route_ime_commit(
    state: InputModeState,
    *,
    store: ComposerStore | None,
    session: KeyInputSession,
    ime: ImeState,
    token: int | None = None,
) -> str:
    """Take exactly one pending IME commit and route it. Returns `none`
    when no commit is pending (a cancel leaves nothing to take)."""
    text = ime.take_committed()
    if text is None:
        return ROUTE_NONE
    return route_committed_text(state, store=store, session=session, text=text, token=token)


@dataclass
class _Hold:
    key_id: str
    token: int


@dataclass
class _Echo:
    expected: tuple[bytes, ...]
    token: int


class KeyEventDeduper:
    """Desktop keydown/text dedupe (echo suppression).

    The screen feeds Flet-layer physical keydowns here plus extension text
    packets; at most one path sends each press:
    - Printable keydowns are held, never sent; the following text packet
      sends once and resolves the hold.
    - Keydowns consumed as key emissions (non-printables, armed-modifier
      chords, Enter) suppress their one matching text echo
      (`consumed_as_key`); a non-matching text clears the suppression and
      still flows.
    - Cancel/keyup clears the hold and suppression with nothing emitted.
    - Stale tokens and `reset` (target change/unmount) drop everything.

    Single-slot by design:
    desktop key events and text packets are assumed to arrive in order, so
    the next text resolves the open hold. Limitation: a second
    `consumed_as_key` overwrites the armed echo, so the first echo (if it
    still arrives) flows as fresh text and the second echo then flows too
    (echo-overwrite can double-flow). An overwritten *hold* still flows
    exactly once as fresh text.
    """

    def __init__(self) -> None:
        self._hold: _Hold | None = None
        self._echo: _Echo | None = None
        self._token = 0

    @property
    def token(self) -> int:
        return self._token

    @property
    def has_hold(self) -> bool:
        return self._hold is not None

    @property
    def has_echo_suppression(self) -> bool:
        return self._echo is not None

    def key_down(self, key_id: str, *, printable: bool, token: int | None = None) -> str:
        """Classify a physical keydown: non-printables (and Enter) send as
        keys; printables hold for their text packet. Stale tokens drop."""
        if token is not None and token != self._token:
            return KEY_DROP
        if not printable:
            # Enter's platform echo is text LF; other
            # non-printables produce no text. The echo is armed only for
            # Enter so unrelated text still flows.
            if key_id == "enter":
                self._echo = _Echo((b"\n", b"\r"), self._token)
            return KEY_SEND_KEY
        self._hold = _Hold(key_id, self._token)
        return KEY_HOLD

    def consumed_as_key(self, *, echo: bytes | None, token: int | None = None) -> None:
        """The held keydown was emitted as key bytes: resolve the hold and
        suppress its one matching text echo (`echo=None` arms nothing)."""
        if token is not None and token != self._token:
            return
        self._hold = None
        if echo is not None:
            self._echo = _Echo((echo,), self._token)

    def text(self, data: bytes, *, token: int | None = None) -> str:
        """Classify an extension text packet: a matching armed echo is
        suppressed; anything else sends exactly once (resolving any open
        hold). Stale tokens drop."""
        if token is not None and token != self._token:
            return TEXT_DROP
        if self._echo is not None:
            if data in self._echo.expected:
                self._echo = None
                self._hold = None
                return TEXT_SUPPRESSED
            self._echo = None
        self._hold = None
        return TEXT_SEND

    def cancel(self, *, token: int | None = None) -> None:
        """Keyup/cancelled gesture: clear the hold and suppression. Provably
        sends nothing."""
        if token is not None and token != self._token:
            return
        self._hold = None
        self._echo = None

    def reset(self) -> None:
        """Target change/unmount: clear all state and kill the token so
        earlier key callbacks emit nothing."""
        self._hold = None
        self._echo = None
        self._token += 1


class ImeState:
    """IME composition state machine. Updates never emit; `commit` banks exactly one text
    for `take_committed`; `cancel` drops everything silently. Mode
    switches never touch this: composition survives Text/Keys switches
    because the input view is zero-height."""

    def __init__(self) -> None:
        self._state = IME_IDLE
        self._marked = ""
        self._committed: str | None = None

    @property
    def state(self) -> str:
        return self._state

    @property
    def is_composing(self) -> bool:
        return self._state == IME_COMPOSING

    @property
    def marked_text(self) -> str:
        return self._marked

    @property
    def has_committed(self) -> bool:
        return self._committed is not None

    def begin(self) -> None:
        self._state = IME_COMPOSING
        self._marked = ""

    def update(self, marked_text: str) -> None:
        """Composition update: state only, provably sends nothing."""
        self._state = IME_COMPOSING
        self._marked = marked_text

    def commit(self, text: str) -> str:
        """Finish composition: bank the committed text for exactly one
        `take_committed`. Returns the text for convenience."""
        self._state = IME_IDLE
        self._marked = ""
        self._committed = text
        return text

    def cancel(self) -> None:
        """Abandon composition. Provably banks and sends nothing."""
        self._state = IME_IDLE
        self._marked = ""
        self._committed = None

    def take_committed(self) -> str | None:
        """Take the banked commit exactly once; later takes return None."""
        text, self._committed = self._committed, None
        return text

    def reset(self) -> None:
        """Target change/unmount wiring: abandon composition silently so no
        text leaks into a new terminal."""
        self.cancel()


__all__ = [
    "AXIS_LABELS",
    "IME_COMPOSING",
    "IME_IDLE",
    "KEYBOARDS",
    "KEYBOARD_KEYS",
    "KEYBOARD_TEXT",
    "KEY_DROP",
    "KEY_HOLD",
    "KEY_SEND_KEY",
    "ROUTE_DRAFT",
    "ROUTE_DROPPED",
    "ROUTE_NONE",
    "ROUTE_PTY",
    "TEXT_DROP",
    "TEXT_SEND",
    "TEXT_SUPPRESSED",
    "WORKFLOWS",
    "WORKFLOW_AGENT_COMPOSER",
    "WORKFLOW_AGENT_DIRECT",
    "WORKFLOW_SHELL",
    "ImeState",
    "InputModeState",
    "KeyEventDeduper",
    "route_committed_text",
    "route_ime_commit",
    "submit_allowed",
]
