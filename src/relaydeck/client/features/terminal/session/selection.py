"""Select-text/copy surface shared by AgentSession and ShellSession.

Owns the frozen engine snapshot of an open selection sheet and drives the
terminal view state's selection slice through the pure reducers in
`models`/`selection_model`. It holds no session reference: state access,
the bound renderer, the output log, notification and the clipboard port all
arrive through `SelectionHooks`. Copy writes only to the clipboard port; it
never emits PTY input and never touches a composer draft.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from relaydeck.client.features.terminal.models import TerminalViewState, jump_to_bottom
from relaydeck.client.features.terminal.selection_model import (
    SelectionRange,
    close_selection,
    copy_range,
    move_anchor,
    move_focus,
    open_selection,
    resolved_range,
    set_selection_error,
)


@dataclass(frozen=True)
class SelectionHooks:
    owner: str  # class name used in the "no clipboard port" error
    state: Callable[[], TerminalViewState]
    set_state: Callable[[TerminalViewState], None]
    term: Callable[[], Any | None]
    output_text: Callable[[], str]
    notify: Callable[[], None]
    clipboard: Callable[[], Any | None]


class SelectionController:
    def __init__(self, hooks: SelectionHooks) -> None:
        self._hooks = hooks
        self.snapshot: str | None = None

    def open(self, anchor: int | None = None, focus: int | None = None) -> None:
        hooks = self._hooks
        hooks.set_state(open_selection(hooks.state(), anchor=anchor, focus=focus))
        hooks.notify()

    @property
    def sheet_text(self) -> str:
        """Frozen engine snapshot when one was fetched for this sheet,
        else the raw output log (pre-engine fallback for unbound views
        and direct sync opens)."""
        if self.snapshot is not None:
            return self.snapshot
        return self._hooks.output_text()

    async def open_snapshot(self, anchor: int | None = None, focus: int | None = None) -> None:
        """Open Select Text on a frozen ENGINE snapshot.

        Fetches the live emulator buffer from the bound renderer and
        freezes it for the sheet lifetime, so cursor overwrites and the
        alternate screen render as the engine shows them, never raw
        ANSI bytes. When no renderer is bound (or it cannot snapshot),
        falls back to the raw output log; a failed fetch additionally
        records a visible error instead of silently showing raw bytes.
        """
        hooks = self._hooks
        term = hooks.term()
        fetch = getattr(term, "get_snapshot_text", None) if term is not None else None
        failure: Exception | None = None
        if callable(fetch):
            try:
                self.snapshot = await fetch()
            except Exception as exc:  # noqa: BLE001 - snapshot best effort
                self.snapshot = None
                failure = exc
        else:
            self.snapshot = None
        # Open first: the reducer replaces the selection slice, so the
        # failure note must land after it (never silently raw bytes).
        self.open(anchor=anchor, focus=focus)
        if failure is not None:
            # TimeoutError stringifies to "": name the failure type so the
            # sheet never shows an empty error (browser regression).
            detail = str(failure) or type(failure).__name__
            hooks.set_state(
                set_selection_error(
                    hooks.state(), f"Live snapshot unavailable; showing raw output: {detail}"
                )
            )
            hooks.notify()

    def move(self, anchor: int, focus: int) -> None:
        """Track the user's live sheet selection (TextField
        on_selection_change): Copy resolves its range from this state,
        so dragging before Copy copies the CURRENT range."""
        hooks = self._hooks
        hooks.set_state(move_anchor(hooks.state(), anchor))
        hooks.set_state(move_focus(hooks.state(), focus))
        hooks.notify()

    def close(self) -> None:
        hooks = self._hooks
        self.snapshot = None
        hooks.set_state(close_selection(hooks.state()))
        term = hooks.term()
        if term is not None:
            clear = getattr(term, "clear_selection", None)
            if callable(clear):
                try:
                    clear()
                except Exception:  # noqa: BLE001 - renderer best effort
                    pass
        hooks.notify()

    def jump_to_bottom(self) -> None:
        hooks = self._hooks
        hooks.set_state(jump_to_bottom(hooks.state()))
        hooks.notify()

    async def copy(self, text: str, anchor: int, focus: int) -> str:
        """Copy the anchored range through the platform clipboard port.
        Never emits PTY input and never touches a composer draft."""
        hooks = self._hooks
        clipboard = hooks.clipboard()
        if clipboard is None:
            raise RuntimeError(f"{hooks.owner} has no clipboard port")
        state = hooks.state()
        selection: SelectionRange = resolved_range(state, text)
        _ = (anchor, focus)  # anchored by the sheet; range resolves from state
        written = await copy_range(text, selection, clipboard)
        hooks.set_state(state)
        return written
