"""Tap-outside renders must not republish a hidden field's pending reset."""

from __future__ import annotations

import asyncio

import pytest

from relaydeck.client.features.composer import components as composer
from relaydeck.client.features.terminal.diagnostics import diagnostics_logger
from relaydeck.client.features.terminal.surfaces import direct_typing
from tests.support.host_harness import run
from tests.support.terminal.direct_typing import _direct, _typing_key, open_compose
from tests.support.terminal.latch_text import _inputs


@pytest.mark.parametrize("tier", ["compact", "medium"])
def test_direct_compose_tap_outside_preserves_reset_boundary(
    tier: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("RELAYDECK_TERMINAL_DIAGNOSTICS", "1")
    now = [1000.0]
    monkeypatch.setattr(direct_typing, "_typing_now", lambda: now[0])
    resets: list[direct_typing._DirectEditor] = []
    reset = direct_typing._DirectEditor.reset

    def counted(editor: direct_typing._DirectEditor, capture: str) -> None:
        resets.append(editor)
        reset(editor, capture)

    monkeypatch.setattr(direct_typing._DirectEditor, "reset", counted)
    logger = diagnostics_logger()
    logger.addHandler(caplog.handler)

    async def scenario() -> None:
        app, session, socket, focuses = await _direct(False, tier)
        try:
            # In Reading/Direct the field remains usable with the keyboard down.
            # Model the real native caret patch as well as the committed text.
            now[0] += 1
            await app.set_props_and_fire("agent-workflow-toggle", {}, "click")
            await asyncio.sleep(0.06)
            await app.settle()
            await app.set_props_and_fire(
                _typing_key(app),
                {"value": "  seed", "selection": {"base_offset": 6, "extent_offset": 6}},
                "change",
                "  seed",
            )
            resets.clear()
            caplog.clear()
            await open_compose(app)
            await app.set_props_and_fire(composer.DRAFT_KEY, {}, "focus")
            assert resets
            editor = resets[-1]
            boundary_count = len(resets)
            pending, waiting = editor.reset_revision, editor.waiting
            native = app.find(_typing_key(app))
            assert not native.visible and native.disabled
            epoch, focus_count, term = editor.reset_epoch, len(focuses), session.current_term
            caplog.clear()

            for _tap in range(3):
                await app.set_props_and_fire(composer.DRAFT_KEY, {}, "tap_outside")
                for i in range(6):
                    # Caret/focus notifications and parent renders after the tap
                    # cannot acknowledge an inactive Direct field's reset.
                    await app.session.dispatch_event(native._i, "blur", "")
                    session.set_bracketed_paste(bool(i % 2))
                    await app.settle()
            assert len(resets) == boundary_count
            assert editor.reset_epoch == epoch
            assert editor.waiting == waiting and editor.reset_revision == pending
            assert app.find(_typing_key(app))._i == native._i
            assert session.current_term is term
            assert len(focuses) == focus_count
            assert _inputs(socket) == [b"seed"]
            assert session.store.draft == "retained compose draft"
            assert app.find(composer.DRAFT_KEY).value == session.store.draft
            reset_lines = [
                record.getMessage()
                for record in caplog.records
                if record.name == logger.name
                and record.getMessage().startswith("direct_typing_reset ")
            ]
            assert reset_lines == []
        finally:
            app.session.close()
            await session.dispose()

    try:
        run(scenario())
    finally:
        logger.removeHandler(caplog.handler)


@pytest.mark.parametrize("shell", [False, True])
@pytest.mark.parametrize("tier", ["compact", "medium"])
def test_direct_terminal_taps_report_pending_reset_once(
    shell: bool, tier: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("RELAYDECK_TERMINAL_DIAGNOSTICS", "1")
    resets: list[direct_typing._DirectEditor] = []
    reset = direct_typing._DirectEditor.reset

    def counted(editor: direct_typing._DirectEditor, capture: str) -> None:
        resets.append(editor)
        reset(editor, capture)

    monkeypatch.setattr(direct_typing._DirectEditor, "reset", counted)
    logger = diagnostics_logger()
    logger.addHandler(caplog.handler)

    async def scenario() -> None:
        app, session, socket, focuses = await _direct(shell, tier)
        try:
            await app.set_props_and_fire(
                _typing_key(app),
                {"value": "  seed", "selection": {"base_offset": 6, "extent_offset": 6}},
                "change",
                "  seed",
            )
            retired = app.find(_typing_key(app))._i
            focus_count = len(focuses)
            resets.clear()
            caplog.clear()
            await app.set_props_and_fire(_typing_key(app), {}, "blur")
            assert len(resets) == 1
            editor = resets[0]
            epoch, pending = editor.reset_epoch, editor.reset_revision
            assert editor.waiting and pending is not None
            native = app.find(_typing_key(app))
            assert native._i != retired and retired not in app.session.index
            assert len(focuses) == focus_count  # Blur never requests focus.
            term = session.current_term
            for _tap in range(3):
                await app.set_props_and_fire("terminal-direct-tap", {}, "tap")
                for i in range(6):
                    session.set_bracketed_paste(bool(i % 2))
                    await app.settle()
            assert len(resets) == 1 and editor.reset_epoch == epoch
            assert editor.waiting and editor.reset_revision == pending
            assert app.find(_typing_key(app))._i == native._i
            assert session.current_term is term
            # Shell taps explicitly request focus; agent terminal taps do not.
            assert focuses[focus_count:] == ([native._i] * 3 if shell else [])
            assert _inputs(socket) == [b"seed"]
            if not shell:
                assert session.store.draft == "retained compose draft"
            reset_lines = [
                r.getMessage()
                for r in caplog.records
                if r.name == logger.name and r.getMessage().startswith("direct_typing_reset ")
            ]
            assert reset_lines == ["direct_typing_reset sent=True"]
            # The acknowledgement remains necessary, and a later real boundary
            # still reports even when its field value/caret already match.
            await app.set_props_and_fire(
                _typing_key(app),
                {},
                "selection_change",
                {"selected_text": "", "selection": {"base_offset": 2, "extent_offset": 2}},
            )
            assert not editor.waiting and editor.reset_revision is None
            await app.set_props_and_fire(_typing_key(app), {}, "blur")
            assert len(resets) == 2
            assert caplog.records[-1].getMessage() == "direct_typing_reset sent=False"
            assert _inputs(socket) == [b"seed"]
        finally:
            app.session.close()
            await session.dispose()

    try:
        run(scenario())
    finally:
        logger.removeHandler(caplog.handler)
