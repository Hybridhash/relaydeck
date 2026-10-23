"""Inactive native focus notifications must not create new Direct field epochs."""

from __future__ import annotations

import asyncio

import pytest

from relaydeck.client.features.composer import components as composer
from relaydeck.client.features.terminal.diagnostics import diagnostics_logger
from relaydeck.client.features.terminal.surfaces import direct_typing
from tests.support.host_harness import run
from tests.support.terminal.direct_typing import _change, _direct, _typing_key, open_compose
from tests.support.terminal.latch_text import _inputs


@pytest.mark.parametrize("shell", [False, True])
@pytest.mark.parametrize("tier", ["compact", "medium"])
@pytest.mark.parametrize("typed", [False, True])
def test_direct_hidden_blur_and_input_free_renders_do_not_reset_or_remount(
    shell: bool,
    tier: str,
    typed: bool,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    now = [1000.0]
    monkeypatch.setattr(direct_typing, "_typing_now", lambda: now[0])
    resets: list[int] = []
    reset = direct_typing._DirectEditor.reset

    def counted(editor: direct_typing._DirectEditor, capture: str) -> None:
        resets.append(editor.reset_epoch)
        reset(editor, capture)

    monkeypatch.setattr(direct_typing._DirectEditor, "reset", counted)
    logger = diagnostics_logger()
    logger.addHandler(caplog.handler)

    async def scenario() -> None:
        app, session, socket, focuses = await _direct(shell, tier)
        try:
            if typed:
                await _change(app, "  seed")
            if shell:
                now[0] += 1
                await app.set_props_and_fire("shell-keyboard-toggle", {}, "click")
            else:
                await open_compose(app)
            await asyncio.sleep(0.06)
            await app.settle()
            field = app.find(_typing_key(app))
            assert not field.visible and field.disabled
            if not shell:
                await app.set_props_and_fire(composer.DRAFT_KEY, {}, "focus")
                await app.set_props_and_fire(composer.DRAFT_KEY, {}, "tap_outside")
            resets.clear()
            caplog.clear()
            focus_count = len(focuses)
            native_ids = []
            term = session.current_term
            for i in range(12):
                native = app.find(_typing_key(app))
                native_ids.append(native._i)
                # Pinned Flet emits blur on FocusNode notifications even if the
                # node is already unfocused. Follow it with a benign parent render.
                await app.session.dispatch_event(native._i, "blur", "")
                session.set_bracketed_paste(bool(i % 2))
                await app.settle()
            assert resets == []
            assert not any(
                record.getMessage().startswith("direct_typing_reset ") for record in caplog.records
            )
            assert set(native_ids) == {field._i}
            assert app.find(_typing_key(app))._i == field._i
            assert session.current_term is term
            assert len(focuses) == focus_count
            assert _inputs(socket) == ([b"seed"] if typed else [])
            if not shell:
                assert session.store.draft == "retained compose draft"
                assert app.find(composer.DRAFT_KEY).value == session.store.draft
                send = next(c for c in app.session.index.values() if c.key == composer.SEND_KEY)
                assert not send.disabled
        finally:
            app.session.close()
            await session.dispose()

    try:
        run(scenario())
    finally:
        logger.removeHandler(caplog.handler)
