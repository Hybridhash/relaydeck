"""The older-output notice has an X; other notices do not, and the X never types."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any

from relaydeck.client.features.terminal.models import (
    OLDER_OUTPUT_NOT_KEPT,
    NoticeModel,
    TerminalViewState,
    history_notice,
)
from relaydeck.client.features.terminal.stream import INCOMPLETE_HISTORY
from tests.support.host_harness import run
from tests.support.ui.split_harness import split_mounted

DISMISS = "terminal-notice-dismiss"


def _has(app: Any, key: str) -> bool:
    try:
        app.find(key)
    except KeyError:
        return False
    return True


def _show(session: Any, notice: NoticeModel | None) -> None:
    controller = session._controller
    assert controller is not None
    controller._set(replace(controller.state, notice=notice))


def test_only_the_older_output_notice_is_dismissible() -> None:
    assert INCOMPLETE_HISTORY == OLDER_OUTPUT_NOT_KEPT == "Older output not kept while away."
    assert history_notice(OLDER_OUTPUT_NOT_KEPT).dismissible
    assert not history_notice("Output lost (viewer too slow): reattached live.").dismissible
    assert not NoticeModel("warning", "x").dismissible
    assert TerminalViewState().notice is None


def test_the_x_clears_the_notice_without_typing_and_a_later_notice_still_shows() -> None:
    async def scenario() -> None:
        app, session, _ = await split_mounted(split=False)
        try:
            sent_before = _sent(session)
            _show(session, history_notice(OLDER_OUTPUT_NOT_KEPT))
            await app.wait_for(DISMISS)
            assert app.find("terminal-notice-text").value == OLDER_OUTPUT_NOT_KEPT
            draft = session.composer_props()["draft"]
            await app.set_props_and_fire(DISMISS, {}, "click")
            await app.settle()
            assert not _has(app, "terminal-notice")
            assert session.term_state.notice is None
            assert _sent(session) == sent_before
            assert session.composer_props()["draft"] == draft

            _show(session, history_notice("Output lost (viewer too slow): reattached live."))
            await app.wait_for("terminal-notice")
            assert not _has(app, DISMISS)
            _show(session, history_notice(OLDER_OUTPUT_NOT_KEPT))
            await app.wait_for(DISMISS)
        finally:
            await session.dispose()

    run(scenario())


def test_dismissing_a_replaced_notice_keeps_the_new_one() -> None:
    async def scenario() -> None:
        _, session, _ = await split_mounted(split=False)
        try:
            first = history_notice(OLDER_OUTPUT_NOT_KEPT)
            _show(session, first)
            newer = history_notice("Output lost (viewer too slow): reattached live.")
            _show(session, newer)
            session.dismiss_notice(first)
            assert session.term_state.notice is newer
            session.dismiss_notice(newer)
            assert session.term_state.notice is None
            await asyncio.sleep(0)
        finally:
            await session.dispose()

    run(scenario())


def _sent(session: Any) -> list[bytes]:
    return [data for sock in session._socket_factory.sockets for data in sock.sent_bytes]
