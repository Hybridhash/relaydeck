"""When the browser blocks clipboard reads, agent paste opens a paste panel that feeds review."""

from __future__ import annotations

from typing import Any

from relaydeck.client.features.terminal.paste_review import CONFIRM_KEY
from tests.support.fakes import FakeClipboard
from tests.support.host_harness import run
from tests.support.terminal.direct_typing import _direct
from tests.support.terminal.latch_text import _inputs


class _Blocked(FakeClipboard):
    async def get_text(self) -> str:
        raise RuntimeError("PlatformException(paste_fail, Clipboard.getData failed., null, null)")


def _shown(app: Any, key: str) -> bool:
    return app.find(key).visible is not False


def test_blocked_clipboard_opens_the_panel_and_its_text_goes_through_review() -> None:
    async def scenario() -> None:
        app, session, socket, _focuses = await _direct(False)
        try:
            session._clipboard = _Blocked()
            assert not _shown(app, "paste-entry-layer")
            await app.set_props_and_fire("agent-paste", {}, "click")
            await app.settle()
            assert _shown(app, "paste-entry-layer")
            assert app.find("paste-entry-review").disabled
            assert not _shown(app, "agent-direct-paste-hint")
            text = "echo one\necho two"
            await app.set_props_and_fire("paste-entry-field", {"value": text}, "change", text)
            assert not app.find("paste-entry-review").disabled
            await app.set_props_and_fire("paste-entry-review", {}, "click")
            await app.settle()
            assert not _shown(app, "paste-entry-layer")
            # Nothing reaches the terminal before the reviewed paste is confirmed.
            assert _inputs(socket) == [] and session.pending_review.preview == text
            await app.set_props_and_fire(CONFIRM_KEY, {}, "click")
            assert b"".join(_inputs(socket)).endswith(b"echo two")
        finally:
            await session.dispose()

    run(scenario())


def test_cancel_closes_the_panel_and_clears_its_text() -> None:
    async def scenario() -> None:
        app, session, socket, _focuses = await _direct(False)
        try:
            session._clipboard = _Blocked()
            await app.set_props_and_fire("agent-paste", {}, "click")
            await app.settle()
            await app.set_props_and_fire("paste-entry-field", {"value": "x"}, "change", "x")
            await app.set_props_and_fire("paste-entry-cancel", {}, "click")
            await app.settle()
            assert not _shown(app, "paste-entry-layer")
            assert app.find("paste-entry-field").value == ""
            assert _inputs(socket) == [] and session.pending_review is None
        finally:
            await session.dispose()

    run(scenario())
