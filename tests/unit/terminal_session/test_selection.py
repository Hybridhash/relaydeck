"""Q5d: SelectionController alone, over the pure selection reducers."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from relaydeck.client.adapters.ports import ClipboardPort
from relaydeck.client.features.terminal.models import TerminalViewState
from relaydeck.client.features.terminal.session.selection import (
    SelectionController,
    SelectionHooks,
)


class Clipboard(ClipboardPort):
    def __init__(self) -> None:
        self.written: list[str] = []

    async def get_text(self) -> str | None:
        raise AssertionError("copy must never read the clipboard")

    async def set_text(self, value: str) -> None:
        self.written.append(value)


class Term:
    def __init__(self, snapshot: str | Exception | None = "engine view") -> None:
        self.snapshot = snapshot
        self.cleared = 0

    async def get_snapshot_text(self) -> str:
        if isinstance(self.snapshot, Exception):
            raise self.snapshot
        assert self.snapshot is not None
        return self.snapshot

    def clear_selection(self) -> None:
        self.cleared += 1


class Rig:
    def __init__(self, term: Any = None, clipboard: ClipboardPort | None = None) -> None:
        self.state = TerminalViewState()
        self.term = term
        self.clipboard = clipboard
        self.log = "raw output"
        self.notified = 0
        self.ctl = SelectionController(
            SelectionHooks(
                owner="TestSession",
                state=lambda: self.state,
                set_state=self._set,
                term=lambda: self.term,
                output_text=lambda: self.log,
                notify=self._notify,
                clipboard=lambda: self.clipboard,
            )
        )

    def _set(self, state: TerminalViewState) -> None:
        self.state = state

    def _notify(self) -> None:
        self.notified += 1


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def test_open_move_close_drive_the_selection_slice() -> None:
    rig = Rig()
    rig.ctl.open(anchor=1, focus=4)
    assert rig.state.selection.open and (rig.state.selection.anchor, rig.state.selection.focus) == (
        1,
        4,
    )
    rig.ctl.move(2, 6)
    assert (rig.state.selection.anchor, rig.state.selection.focus) == (2, 6)
    rig.ctl.close()
    assert not rig.state.selection.open
    assert rig.notified == 3


def test_sheet_text_prefers_the_frozen_snapshot_then_the_output_log() -> None:
    rig = Rig()
    assert rig.ctl.sheet_text == "raw output"
    rig.ctl.snapshot = "frozen"
    assert rig.ctl.sheet_text == "frozen"


def test_open_snapshot_freezes_the_engine_buffer() -> None:
    async def go() -> None:
        rig = Rig(Term("engine view"))
        await rig.ctl.open_snapshot(anchor=0, focus=3)
        assert rig.ctl.snapshot == "engine view" and rig.ctl.sheet_text == "engine view"
        assert rig.state.selection.open and rig.state.selection.error is None

    run(go())


def test_open_snapshot_without_a_renderer_falls_back_to_raw_output() -> None:
    async def go() -> None:
        rig = Rig(None)
        await rig.ctl.open_snapshot()
        assert rig.ctl.snapshot is None and rig.ctl.sheet_text == "raw output"
        assert rig.state.selection.open and rig.state.selection.error is None

    run(go())


@pytest.mark.parametrize(
    ("failure", "expected"),
    [(RuntimeError("engine gone"), "engine gone"), (TimeoutError(), "TimeoutError")],
)
def test_failed_snapshot_is_visible_and_never_silently_raw(
    failure: Exception, expected: str
) -> None:
    async def go() -> None:
        rig = Rig(Term(failure))
        await rig.ctl.open_snapshot()
        assert rig.ctl.snapshot is None and rig.ctl.sheet_text == "raw output"
        assert rig.state.selection.open
        assert (
            rig.state.selection.error
            == f"Live snapshot unavailable; showing raw output: {expected}"
        )

    run(go())


def test_close_clears_the_snapshot_and_the_renderer_selection_best_effort() -> None:
    term = Term()
    rig = Rig(term)
    rig.ctl.snapshot = "frozen"
    rig.ctl.close()
    assert rig.ctl.snapshot is None and term.cleared == 1

    class Broken(Term):
        def clear_selection(self) -> None:
            raise RuntimeError("engine gone")

    rig.term = Broken()
    rig.ctl.close()  # must not raise


def test_jump_to_bottom_notifies() -> None:
    rig = Rig()
    rig.ctl.jump_to_bottom()
    assert rig.notified == 1


def test_copy_writes_only_the_resolved_range_to_the_clipboard() -> None:
    async def go() -> None:
        clip = Clipboard()
        rig = Rig(clipboard=clip)
        rig.ctl.open(anchor=2, focus=5)
        written = await rig.ctl.copy("0123456789", 2, 5)
        assert written == clip.written[0] and clip.written == ["234"]
        assert rig.state.selection.open  # copy leaves the sheet as it was

    run(go())


def test_copy_without_a_clipboard_names_the_owner() -> None:
    async def go() -> None:
        rig = Rig(clipboard=None)
        with pytest.raises(RuntimeError, match="TestSession has no clipboard port"):
            await rig.ctl.copy("abc", 0, 1)

    run(go())
