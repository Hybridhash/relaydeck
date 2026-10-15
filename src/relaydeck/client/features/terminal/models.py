"""Pure terminal view state and reducers.

Scroll, selection, notices and stream continuity remain independent of input."""

from __future__ import annotations

from dataclasses import dataclass, replace

STATUS_LIVE = "live"
STATUS_CONNECTING = "connecting"
STATUS_RECONNECTING = "reconnecting"
STATUS_RESET = "reset"

TONE_INFO = "info"
TONE_WARN = "warning"


@dataclass(frozen=True)
class ScrollModel:
    """Reader scroll position. New output never flips `stick_to_bottom`:
    a reader examining older output stays put and a deliberate
    jump-to-latest restores the tail."""

    stick_to_bottom: bool = True
    unseen_bytes: int = 0


@dataclass(frozen=True)
class NoticeModel:
    tone: str
    text: str


@dataclass(frozen=True)
class SelectionState:
    """Select Text sheet state. Selection mode is
    separate from terminal input focus: opening, moving, copying, or
    closing a selection never emits PTY input or touches the composer
    draft. `anchor`/`focus` are flat str offsets into the sheet text;
    None selects all."""

    open: bool = False
    anchor: int | None = None
    focus: int | None = None
    last_copied: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class TerminalViewState:
    # Pre-stream default: nothing attached yet (agent disclosure, shell
    # dialing). Only apply_live() on the first established chunk may
    # claim "live", so the footer never presents an unattached surface
    # as live (browser regression).
    status: str = STATUS_CONNECTING
    notice: NoticeModel | None = None
    scroll: ScrollModel = ScrollModel()
    epoch: str = ""
    applied_offset: int = 0
    lease_mode: str = "view"
    selection: SelectionState = SelectionState()

    @property
    def show_jump_to_latest(self) -> bool:
        return not self.scroll.stick_to_bottom


def apply_bytes(state: TerminalViewState, count: int) -> TerminalViewState:
    """Account delivered output. Scroll position is preserved: stuck
    readers follow the tail implicitly, detached readers accrue unseen
    bytes for the jump affordance."""
    if count <= 0:
        return state
    if state.scroll.stick_to_bottom:
        return replace(state, applied_offset=state.applied_offset + count)
    return replace(
        state,
        applied_offset=state.applied_offset + count,
        scroll=ScrollModel(stick_to_bottom=False, unseen_bytes=state.scroll.unseen_bytes + count),
    )


def user_scroll_away(state: TerminalViewState) -> TerminalViewState:
    """The reader left the tail: pin position and show the affordance."""
    return replace(state, scroll=ScrollModel(stick_to_bottom=False, unseen_bytes=0))


def jump_to_bottom(state: TerminalViewState) -> TerminalViewState:
    """Deliberate scroll-to-bottom: re-stick and clear the unseen count."""
    return replace(state, scroll=ScrollModel(stick_to_bottom=True, unseen_bytes=0))


def apply_gap(state: TerminalViewState, detail: str) -> TerminalViewState:
    """Transient offset gap: reconnecting, resume pending, scroll kept."""
    return replace(
        state,
        status=STATUS_RECONNECTING,
        notice=NoticeModel(TONE_WARN, f"Reconnecting ({detail}); resuming output..."),
    )


def apply_reset(state: TerminalViewState, notice_text: str) -> TerminalViewState:
    """Epoch rotation or retention loss: the old screen cannot continue.
    Reattach starts a new stream; the notice says history is incomplete."""
    return replace(
        state,
        status=STATUS_RESET,
        notice=NoticeModel(TONE_WARN, notice_text),
        scroll=ScrollModel(stick_to_bottom=True, unseen_bytes=0),
    )


def apply_live(
    state: TerminalViewState, epoch: str, offset: int, notice: NoticeModel | None = None
) -> TerminalViewState:
    """Stream (re)established. A notice marks incomplete history when the
    new emulator did not replay from byte 0 of the epoch."""
    return replace(state, status=STATUS_LIVE, notice=notice, epoch=epoch, applied_offset=offset)
