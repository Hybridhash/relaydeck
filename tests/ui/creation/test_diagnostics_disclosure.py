"""Creation diagnostics disclose categories; paste discloses source."""

from __future__ import annotations

import logging
from typing import Any

import flet as ft
import pytest

from relaydeck.client.app import Root
from relaydeck.client.features.composer.observation import CommandOutcomeUnknown
from relaydeck.client.features.inventory.routing import RouteBridge
from relaydeck.client.features.sync.transport import TransportError
from tests.support.host_harness import render_component, run
from tests.support.pictures.clipboard_picker import _Blocked
from tests.support.scripted_sessions import make_scripted_sessions
from tests.support.terminal.direct_typing import _direct
from tests.support.terminal.latch_text import _inputs
from tests.support.ui.navigation import _state, _StubController, _StubTransport

MARKERS = (
    "SYNTHETIC_BEARER_DISCLOSURE",
    "SYNTHETIC_GRANT_DISCLOSURE",
    "SYNTHETIC_COOKIE_DISCLOSURE",
    "/private/SYNTHETIC_PATH_DISCLOSURE",
    "SYNTHETIC_IMAGE_CONTENT_DISCLOSURE",
)
SECRET_TEXT = " https://synthetic.invalid/private?grant=" + "|".join(MARKERS)
SOURCE_LABEL = "Last copied in RelayDeck; clipboard unavailable"


@pytest.mark.parametrize("shell", [False, True])
@pytest.mark.parametrize(
    "failure", ["transport", "untrusted_code", "value", "observation", "class"]
)
def test_creation_warning_omits_messages_causes_and_untrusted_codes(
    shell: bool, failure: str, caplog: pytest.LogCaptureFixture
) -> None:
    async def scenario() -> None:
        class FailedTransport(_StubTransport):
            async def submit_command(self, command: dict[str, Any]) -> dict[str, Any]:
                self.submitted.append(command)
                if failure in {"transport", "untrusted_code"}:
                    error: Exception = TransportError(
                        " HOST_OFFLINE " if failure == "transport" else SECRET_TEXT, SECRET_TEXT
                    )
                elif failure == "value":
                    error = ValueError(SECRET_TEXT)
                elif failure == "observation":
                    error = CommandOutcomeUnknown(SECRET_TEXT)
                else:
                    error = type("SYNTHETIC_BEARER_DISCLOSURE", (RuntimeError,), {})(SECRET_TEXT)
                raise error from RuntimeError("cause: " + SECRET_TEXT)

        controller = _StubController()
        transport = FailedTransport()
        app = await render_component(
            Root,
            controller=controller,
            transport=transport,
            host_id="home-mac",
            bridge=RouteBridge(route="/", width=1280),
            go=lambda route: None,
            session_factory=make_scripted_sessions().factory,
            account="synthetic",
        )
        try:
            controller.push(_state())
            await app.settle()
            if shell:
                await app.set_props_and_fire("console-rail-terminals", {}, "click")
                await app.set_props_and_fire("new-terminal-open", {}, "click")
            else:
                await app.set_props_and_fire("console-new-agent", {}, "click")
            await app.set_props_and_fire(
                "shell-workspace" if shell else "agent-workspace", {"value": "w1"}, "select", "w1"
            )
            caplog.clear()
            await app.set_props_and_fire("sheet-submit", {}, "click")
            assert len(transport.submitted) == 1
            records = [r for r in caplog.records if r.name == "relaydeck.client.app"]
            assert len(records) == 1
            category = {
                "transport": "TransportError",
                "untrusted_code": "TransportError",
                "value": "ValueError",
                "observation": "CommandOutcomeUnknown",
                "class": "Exception",
            }[failure]
            code = "host_offline" if failure == "transport" else "unknown"
            assert (
                records[0].getMessage() == f"Creation outcome unknown: class={category} code={code}"
            )
            assert records[0].exc_info is None and records[0].exc_text is None
            assert not any(marker in caplog.text for marker in MARKERS)
            assert "https://synthetic.invalid" not in caplog.text
            assert "do not retry" in app.find("sheet-error").value
        finally:
            app.page.render(lambda: ft.Text("Closed"))
            await app.settle()

    caplog.set_level(logging.WARNING, logger="relaydeck.client.app")
    run(scenario())


@pytest.mark.parametrize("shell", [False, True])
@pytest.mark.parametrize("text", ["echo copied", "echo copied\necho reviewed"])
def test_cached_copy_review_discloses_source_and_requires_confirmation(
    shell: bool, text: str
) -> None:
    async def scenario() -> None:
        app, session, socket, _ = await _direct(shell)
        try:
            session._clipboard = _Blocked(text)
            draft = None if shell else session.store.draft
            term = session.current_term
            await app.set_props_and_fire("shell-paste" if shell else "agent-paste", {}, "click")
            assert _inputs(socket) == []
            assert session.pending_review is not None
            assert session.pending_review.preview == text
            assert session.pending_review.from_app is True
            source = app.find("paste-review-source")
            assert source.visible and source.value == SOURCE_LABEL
            await app.set_props_and_fire("paste-review-confirm", {}, "click")
            assert b"".join(_inputs(socket)) == text.encode()
            assert session.current_term is term
            if not shell:
                assert session.store.draft == draft
        finally:
            await session.dispose()

    run(scenario())


@pytest.mark.parametrize("shell", [False, True])
def test_fresh_clipboard_review_clears_cached_source_and_stale_confirm_sends_nothing(
    shell: bool,
) -> None:
    async def scenario() -> None:
        app, session, socket, _ = await _direct(shell)
        try:
            session._clipboard = _Blocked("cached copy")
            await app.set_props_and_fire("shell-paste" if shell else "agent-paste", {}, "click")
            assert app.find("paste-review-source").visible

            async def fresh() -> str:
                return "fresh\nclipboard"

            session._clipboard.get_text = fresh
            await app.set_props_and_fire("shell-paste" if shell else "agent-paste", {}, "click")
            assert session.pending_review is not None and not session.pending_review.from_app
            assert not app.find("paste-review-source").visible
            assert session.pending_review.preview == "fresh\nclipboard"
            session.controller.invalidate_writer("generation_replaced")
            await app.set_props_and_fire("paste-review-confirm", {}, "click")
            assert _inputs(socket) == [] and session.pending_review is None
        finally:
            await session.dispose()

    run(scenario())
