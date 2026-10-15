"""Real compact paste actions, native field review, and sanitized key-bar failures."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from relaydeck.client.adapters.clipboard import ClipboardReadStatus, read_clipboard
from relaydeck.client.adapters.fake import FakeClipboard
from relaydeck.client.components.design_system.terminal_window import (
    KEYBAR_FAILURE_MESSAGE,
    MobileKeyBar,
)
from relaydeck.client.features.terminal.paste_policy import BRACKET_END, BRACKET_START
from relaydeck.client.features.terminal.paste_review import CONFIRM_KEY
from relaydeck.client.features.terminal.surfaces import direct_typing
from tests.support.host_harness import render_component, run
from tests.support.terminal.direct_typing import _change, _direct, _typing_key
from tests.support.terminal.latch_text import _inputs

MENU = json.loads((Path(__file__).parents[2] / "fixtures/typing/paste_menu.json").read_text())


class _Unavailable(FakeClipboard):
    def __init__(self, error: Exception | None = None) -> None:
        super().__init__()
        self.error = error or RuntimeError(
            "PlatformException(paste_fail, Clipboard.getData failed., null, null)"
        )
        self.reads = 0

    async def get_text(self) -> str:
        self.reads += 1
        raise self.error


def _paste_key(shell: bool) -> str:
    return "shell-paste" if shell else "agent-paste"


@pytest.mark.parametrize("error", [RuntimeError("PlatformException(paste_fail)"), TimeoutError()])
def test_clipboard_read_has_a_typed_unavailable_result(error: Exception) -> None:
    async def scenario() -> None:
        port = _Unavailable(error)
        result = await read_clipboard(port)
        assert result.status is ClipboardReadStatus.UNAVAILABLE and result.text is None
        assert port.reads == 1
        empty = await read_clipboard(FakeClipboard())
        assert empty.status is ClipboardReadStatus.AVAILABLE
        assert (await read_clipboard(None)).status is ClipboardReadStatus.UNAVAILABLE

    run(scenario())


@pytest.mark.parametrize("shell", [False, True])
@pytest.mark.parametrize("timeout", [False, True])
def test_paste_failure_opens_and_focuses_the_retained_field_without_terminal_input(
    shell: bool, timeout: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = [1.0]
    monkeypatch.setattr(direct_typing, "_typing_now", lambda: now[0])

    async def scenario() -> None:
        app, session, socket, focuses = await _direct(shell)
        try:
            term, field = session.current_term, app.find(_typing_key(app))
            port = _Unavailable(TimeoutError() if timeout else None)
            session._clipboard = port
            now[0] += 0.5
            await app.set_props_and_fire(
                "shell-keyboard-toggle" if shell else "agent-workflow-toggle", {}, "click"
            )
            assert not app.find(_typing_key(app)).visible
            count = len(focuses)
            await app.set_props_and_fire(_paste_key(shell), {}, "click")
            current = app.find(_typing_key(app))
            assert current.visible and current.can_request_focus and not current.disabled
            assert current._i == field._i and focuses[count:] == [current._i]
            hint = app.find("shell-direct-paste-hint" if shell else "agent-direct-paste-hint")
            assert hint.visible and hint.value == "Long-press here and choose Paste"
            assert current.enable_interactive_selection is True
            assert port.reads == 1 and _inputs(socket) == []
            assert session.current_term is term and session.pending_review is None
            if not shell:
                assert session.store.draft == "retained compose draft"
        finally:
            await session.dispose()

    run(scenario())


@pytest.mark.parametrize("shell", [False, True])
@pytest.mark.parametrize("selection", ["caret", "select-all"])
@pytest.mark.parametrize("prefix", ["", " ", "  "])
def test_fallback_native_menu_contract_selection_paste_review_and_exact_confirm(
    shell: bool, selection: str, prefix: str
) -> None:
    """Host half of native long-press/Paste contract; widget half is separate."""

    async def scenario() -> None:
        app, session, socket, _focuses = await _direct(shell)
        try:
            field = app.find(_typing_key(app))
            assert not field.enable_interactive_selection
            field_id, term, binding = field._i, session.current_term, session._view_token
            session._clipboard = _Unavailable()
            await app.set_props_and_fire(_paste_key(shell), {}, "click")
            current = app.find(_typing_key(app))
            assert current._i == field_id and current.enable_interactive_selection
            for prop, value in MENU["field"].items():
                if not prop.startswith("on_"):
                    assert getattr(current, prop) == value, prop
            # Also deliver a late range from the old sentinel field. Capture
            # never derives payload bytes or replacement DELs from that range.
            base, extent = (0, 2) if selection == "select-all" else (0, 0)
            await app.set_props_and_fire(
                _typing_key(app),
                {},
                "selection_change",
                {
                    "selection": {"base_offset": base, "extent_offset": extent},
                    "selected_text": "  " if selection == "select-all" else "",
                },
            )
            assert _inputs(socket) == [] and session.pending_review is None
            # Native widget test uses the actual menu and emits this change;
            # this host test only consumes the resulting selection/value events.
            text = prefix + MENU["pasted_text"]
            await _change(app, text)
            assert _inputs(socket) == []  # Includes withheld sentinel replacement DELs.
            assert session.pending_review.preview == text
            assert not app.find(_typing_key(app)).enable_interactive_selection
            await app.set_props_and_fire(CONFIRM_KEY, {}, "click")
            assert _inputs(socket) == [text.encode()]
            assert session.current_term is term and session._view_token == binding
            assert app.find(_typing_key(app))._i == field_id
            assert app.find(_typing_key(app)).value == "  "
            await _change(app, "  x")
            assert _inputs(socket) == [text.encode(), b"x"]
            if not shell:
                assert session.store.draft == "retained compose draft"
        finally:
            await session.dispose()

    run(scenario())


@pytest.mark.parametrize("shell", [False, True])
@pytest.mark.parametrize("stale", [False, True])
def test_explicit_native_paste_select_all_cannot_write_unsafe_or_stale_replacement(
    shell: bool, stale: bool
) -> None:
    async def scenario() -> None:
        app, session, socket, _focuses = await _direct(shell)
        try:
            session._clipboard = _Unavailable()
            await app.set_props_and_fire(_paste_key(shell), {}, "click")
            field = app.find(_typing_key(app))
            assert field.enable_interactive_selection
            await app.set_props_and_fire(
                _typing_key(app),
                {},
                "selection_change",
                {"selection": {"base_offset": 0, "extent_offset": 2}, "selected_text": "  "},
            )
            if stale:
                session.set_foreground(False)
                # Deliver the captured native callback before a parent rerender.
                await field._trigger_event("change", "echo safe")
            else:
                await _change(app, "echo\x1b[2J")
                assert "unsafe control" in session.paste_error
            assert _inputs(socket) == [] and session.pending_review is None
            if not shell:
                assert session.store.draft == "retained compose draft"
        finally:
            await session.dispose()

    run(scenario())


@pytest.mark.parametrize("shell", [False, True])
@pytest.mark.parametrize("bracketed", [False, True])
def test_native_paste_requires_review_and_preserves_exact_bytes(
    shell: bool, bracketed: bool
) -> None:
    async def scenario() -> None:
        app, session, socket, _focuses = await _direct(shell)
        try:
            session._clipboard = _Unavailable()
            session.set_bracketed_paste(bracketed)
            await app.set_props_and_fire(_paste_key(shell), {}, "click")
            text = "echo one\r\necho two\n"
            await _change(app, text)
            assert _inputs(socket) == [] and session.pending_review.preview == text
            assert app.find(_typing_key(app)).disabled
            # More changes cannot bypass the open review or amend its snapshot.
            await _change(app, "  forbidden")
            assert _inputs(socket) == [] and session.pending_review.preview == text
            await app.set_props_and_fire(CONFIRM_KEY, {}, "click")
            assert _inputs(socket) == [
                BRACKET_START + text.encode() + BRACKET_END if bracketed else text.encode()
            ]
            if not shell:
                assert session.store.draft == "retained compose draft"
        finally:
            await session.dispose()

    run(scenario())


@pytest.mark.parametrize("shell", [False, True])
@pytest.mark.parametrize("text", ["a\tb", "a\x1bb", "a\ud800b"])
def test_control_field_input_is_reviewed_or_rejected_without_replacement_bytes(
    shell: bool, text: str
) -> None:
    async def scenario() -> None:
        app, session, socket, _focuses = await _direct(shell)
        try:
            # No clipboard fallback is required to fence unsafe native input;
            # replacing the sentinel must not send DELs ahead of classification.
            await _change(app, text)
            assert _inputs(socket) == []
            if "\t" in text:
                assert session.pending_review.preview == text
                await app.set_props_and_fire(CONFIRM_KEY, {}, "click")
                assert _inputs(socket) == [text.encode()]
            else:
                assert session.pending_review is None and "unsafe control" in session.paste_error
        finally:
            await session.dispose()

    run(scenario())


@pytest.mark.parametrize("shell", [False, True])
def test_successful_clipboard_read_keeps_single_line_insert_behavior(shell: bool) -> None:
    async def scenario() -> None:
        app, session, socket, focuses = await _direct(shell)
        try:
            port = FakeClipboard()
            await port.set_text("echo hello")
            session._clipboard = port
            count = len(focuses)
            await app.set_props_and_fire(_paste_key(shell), {}, "click")
            assert _inputs(socket) == [b"echo hello"] and session.pending_review is None
            assert len(focuses) == count
        finally:
            await session.dispose()

    run(scenario())


@pytest.mark.parametrize("shell", [False, True])
def test_late_clipboard_result_cannot_focus_or_write_after_generation_change(shell: bool) -> None:
    async def scenario() -> None:
        app, session, socket, focuses = await _direct(shell)
        started, release = asyncio.Event(), asyncio.Event()
        task: Any = None
        try:

            class Delayed(_Unavailable):
                async def get_text(self) -> str:
                    started.set()
                    await release.wait()
                    return await super().get_text()

            session._clipboard = Delayed()
            count = len(focuses)
            task = asyncio.create_task(
                app.session.dispatch_event(app.find(_paste_key(shell))._i, "click", "")
            )
            await asyncio.wait_for(started.wait(), 1)
            session.set_foreground(False)
            session.set_foreground(True)
            release.set()
            await task
            await app.settle()
            assert _inputs(socket) == [] and len(focuses) == count
            assert not app.find(
                "shell-direct-paste-hint" if shell else "agent-direct-paste-hint"
            ).visible
        finally:
            release.set()
            if task:
                await asyncio.gather(task, return_exceptions=True)
            await session.dispose()

    run(scenario())


@pytest.mark.parametrize("kind", ["paste", "select", "key", "hold"])
@pytest.mark.parametrize("async_action", [False, True])
def test_keybar_failures_are_logged_and_show_only_a_short_message(
    kind: str, async_action: bool, caplog: pytest.LogCaptureFixture
) -> None:
    async def scenario() -> None:
        def fail(*args: Any) -> None:
            raise RuntimeError("Traceback: /Users/someone/private.py:15\nprivate detail")

        async def fail_async(*args: Any) -> None:
            await asyncio.sleep(0)
            fail(*args)

        action = fail_async if async_action else fail
        app = await render_component(
            MobileKeyBar,
            on_item=action,
            modifier_states={},
            enabled=True,
            on_paste=action,
            on_select=action,
        )
        key = (
            "terminal-paste"
            if kind == "paste"
            else (
                "terminal-select-text"
                if kind == "select"
                else ("key-left" if kind == "hold" else "key-slash")
            )
        )
        await app.find(key)._trigger_event("long_press" if kind == "hold" else "click", "")
        await app.settle()
        message = app.find("terminal-keybar-error").value
        assert app.find("terminal-keybar-error-region").visible
        assert message == KEYBAR_FAILURE_MESSAGE
        assert "try again" not in message.lower()
        assert "Traceback" not in message and "/Users/" not in message and "private" not in message
        # Q3: content-free log (category only); never a traceback or message.
        assert not any(record.exc_info for record in caplog.records)
        assert any("category=RuntimeError" in record.getMessage() for record in caplog.records)
        assert "private" not in "\n".join(record.getMessage() for record in caplog.records)

    run(scenario())
