"""Phone selection uses native editable selection, not terminal canvas dragging."""

from __future__ import annotations

import asyncio
from typing import Any

import flet as ft
import pytest

from relaydeck.client.features.terminal import surfaces
from tests.support.host_harness import run
from tests.support.terminal.select_sheet import _sheet
from tests.support.terminal.selection import _open_with_snapshot
from tests.support.ui.phone_terminal import _ancestry, _shape

TEXT = (
    "\r\n".join(f"line {i:03d}" for i in range(125))
    + "\r\n😀 <script>literal</script> https://untrusted.invalid\u2028same terminal line"
)


@pytest.mark.parametrize("shell", [False, True])
@pytest.mark.parametrize("tier", ["compact", "medium"])
@pytest.mark.parametrize("reading", [False, True])
def test_phone_sheet_real_select_snapshot_native_range_copy_wrap_and_done(
    shell: bool, tier: str, reading: bool
) -> None:
    async def scenario() -> None:
        app, session, _socket, channel = await _sheet(shell, tier=tier, reading=reading)
        try:
            term, binding = session.current_term, session._view_token
            ancestry = _shape(_ancestry(app, term))
            await _open_with_snapshot(app, term, channel.sent, TEXT)
            sheet = app.find("phone-select-sheet")
            assert sheet.visible and sheet.padding == 12
            frame = app.find("phone-select-stack")
            assert frame.fit == ft.StackFit.EXPAND and frame.expand
            field = app.find("phone-select-field")
            # Plain text in a selection area, never an input: no hidden browser field for iOS.
            assert isinstance(field, ft.Text) and field.value == TEXT and not field.selectable
            assert isinstance(app.find("phone-select-area"), ft.SelectionArea)
            assert field.expand is None and field.max_lines is None
            assert app.find("phone-select-text-scroll").scroll == ft.ScrollMode.AUTO
            assert field.style.font_family == "JetBrains Mono"
            assert field.style.size == term.font_size
            assert app.find("phone-select-copy-visible").disabled
            assert "viewport" in app.find("phone-select-copy-visible").tooltip.lower()
            assert "unavailable" in app.find("phone-select-visible-unavailable").value
            # Pointer focus loss cannot dismiss a sheet or enable the PTY.
            await field._trigger_event("focus", None)
            await field._trigger_event("blur", None)
            await app.settle()
            assert session.term_state.selection.open and not term.input_enabled
            for key, expected in (
                ("phone-select-copy-all", TEXT),
                ("phone-select-copy-last-100", "\r\n".join(TEXT.split("\r\n")[-100:])),
            ):
                await app.set_props_and_fire(key, {}, "click")
                assert session._clipboard.writes[-1] == expected
                assert session.term_state.selection.open
            start = TEXT.index("😀")
            end = start + len("😀 <script>literal</script>")
            await app.set_props_and_fire("phone-select-area", {}, "change", TEXT[start:end])
            await app.set_props_and_fire("phone-select-copy-selection", {}, "click")
            assert session._clipboard.writes[-1] == TEXT[start:end]
            # A cleared selection must not erase the last valid range before the copy.
            await app.set_props_and_fire("phone-select-area", {}, "change", "")
            await app.set_props_and_fire("phone-select-copy-selection", {}, "click")
            assert session._clipboard.writes[-1] == TEXT[start:end]
            field_id = field._i
            await app.set_props_and_fire("phone-select-wrap", {}, "click")
            assert app.find("phone-select-field")._i == field_id
            assert app.find("phone-select-text-row").scroll == ft.ScrollMode.AUTO
            assert app.find("phone-select-field").width > 390
            assert not app.find("phone-select-text-row").wrap
            await app.set_props_and_fire("phone-select-wrap", {}, "click")
            assert app.find("phone-select-text-row").scroll is None
            assert app.find("phone-select-field").value == TEXT
            assert session.current_term is term and session._view_token == binding
            assert _shape(_ancestry(app, term)) == ancestry
            await app.set_props_and_fire("phone-select-done", {}, "click")
            assert not session.term_state.selection.open
            assert app.find("phone-select-sheet").visible is False
            assert app.find("phone-select-field").value == ""
            assert session.current_term is term and _shape(_ancestry(app, term)) == ancestry
            if not shell:
                assert session.store.draft == "retained compose draft"
        finally:
            await session.dispose()

    run(scenario())


@pytest.mark.parametrize("shell", [False, True])
@pytest.mark.parametrize("stale", ["native-reset", "writer", "park", "new-sheet"])
def test_stale_phone_copy_and_selection_leave_replacement_unchanged(
    shell: bool, stale: str
) -> None:
    async def scenario() -> None:
        app, session, _socket, channel = await _sheet(shell)
        try:
            term = session.current_term
            await _open_with_snapshot(app, term, channel.sent, TEXT)
            old_copy = app.find("phone-select-copy-all")
            old_area = app.find("phone-select-area")
            copy_handler = old_copy.on_click
            selection_handler = old_area.on_change
            if stale == "native-reset":
                term.reset(reason="epoch_changed")
            elif stale == "writer":
                await session.reconnect()
                if not shell:
                    await session.confirm_takeover()
            elif stale == "park":
                session.set_foreground(False)
            else:
                surfaces.selection_actions(session)[2]()
                session._selection_snapshot = "new sheet"
                session.open_selection()
            state, projected = session.term_state, session.controller.state
            before = list(session._clipboard.writes)
            # Exercise queued callbacks even if Flet has already detached their controls.
            await copy_handler(ft.ControlEvent(control=old_copy, name="click", data=None))
            await selection_handler(ft.ControlEvent(control=old_area, name="change", data="l"))
            assert session._clipboard.writes == before
            assert session.term_state is state and session.controller.state is projected
        finally:
            await session.dispose()

    run(scenario())


@pytest.mark.parametrize("shell", [False, True])
def test_clipboard_completion_cannot_restore_old_session_or_update_new_sheet(shell: bool) -> None:
    async def scenario() -> None:
        app, session, _socket, channel = await _sheet(shell)
        started, release = asyncio.Event(), asyncio.Event()
        pending: asyncio.Task[Any] | None = None
        try:
            await _open_with_snapshot(app, session.current_term, channel.sent, TEXT)
            writes: list[str] = []

            class DelayedClipboard:
                async def set_text(self, text: str) -> None:
                    started.set()
                    await release.wait()
                    writes.append(text)

            session._clipboard = DelayedClipboard()
            old_copy = app.find("phone-select-copy-all")
            pending = asyncio.create_task(old_copy._trigger_event("click", None))
            await asyncio.wait_for(started.wait(), 1)
            await session.reconnect()
            if not shell:
                await session.confirm_takeover()
            await app.settle()
            state, projected = session.term_state, session.controller.state
            release.set()
            await pending
            await app.settle()
            # The already-authorized clipboard request may complete. It may
            # never write captured state into the replacement or claim success
            # on its sheet; no replay or terminal input is involved.
            assert writes == [TEXT]
            assert session.term_state is state and session.controller.state is projected
            assert not app.find("phone-select-result").visible
        finally:
            release.set()
            if pending is not None:
                await asyncio.gather(pending, return_exceptions=True)
            await session.dispose()

    run(scenario())
