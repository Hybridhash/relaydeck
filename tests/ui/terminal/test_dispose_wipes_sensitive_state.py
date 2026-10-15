"""Reason-aware teardown: `dispose(clear_sensitive=...)`.

Ordinary leave (Back/route/offline) keeps object contents: Root
recreates sessions after Back, so the default `dispose()` preserves
the draft, output bytes, selection snapshot, and notices. Only
revoke/logout closes pass `clear_sensitive=True`, wiping cached
sensitive content instead of relying on GC. Live-session transitions
(workflow/keyboard/sheet switches, disconnect) preserve the draft
in both cases.
"""

from __future__ import annotations

from typing import Any

from relaydeck.client.features.composer.domain import SEND_FAILED
from relaydeck.client.features.sync.transport import TransportError
from relaydeck.client.features.terminal.agent_session import AGENT_LIVE
from relaydeck.client.features.terminal.input_modes import (
    KEYBOARD_KEYS,
    KEYBOARD_TEXT,
    WORKFLOW_AGENT_COMPOSER,
    WORKFLOW_AGENT_DIRECT,
)
from tests.support.host_harness import run
from tests.support.terminal.sessions import _agent_session, _session


class _SnapTerm:
    """Minimal bound renderer: snapshot source only."""

    async def get_snapshot_text(self) -> str:
        return "secret screen"


async def _loaded_agent() -> Any:
    """Live agent holding every sensitive cache: failed-send error,
    unsent draft, output bytes, selection snapshot, and notice."""

    async def failing_prompt(target: str, text: str) -> dict[str, Any]:
        raise TransportError("agent_not_found", "no such agent", retryable=False)

    session, _, _, _ = _agent_session(prompt=failing_prompt)
    session.open()
    assert await session.confirm_takeover() == AGENT_LIVE
    session.on_draft_change("secret plan")
    assert await session.on_send() == SEND_FAILED
    assert session.store.error is not None
    session._handle_bytes(b"secret output")
    session.bind_view(_SnapTerm())
    await session.open_selection_snapshot()
    session._on_renewal_terminal("lease_conflict")
    assert session.store.draft == "secret plan"
    assert len(session.output_log) > 0
    assert session.selection_sheet_text == "secret screen"
    assert "lease" in session.notice
    return session


async def _loaded_shell() -> Any:
    session, _, _ = _session()
    assert await session.open("term-1") == "live"
    session._handle_bytes(b"secret output")
    session.bind_view(_SnapTerm())
    await session.open_selection_snapshot()
    session._on_renewal_terminal("lease_conflict")
    assert len(session.output_log) > 0
    assert session.selection_sheet_text == "secret screen"
    assert "lease" in session.notice
    return session


def test_agent_ordinary_dispose_preserves_contents() -> None:
    async def scenario() -> None:
        session = await _loaded_agent()
        await session.dispose()
        assert session.disposed is True
        assert session.store.draft == "secret plan"
        assert session.store.error is not None
        assert bytes(session.output_log) == b"secret output"
        assert session.selection_sheet_text == "secret screen"
        assert session.notice != ""

    run(scenario())


def test_agent_revoke_dispose_wipes_sensitive_caches() -> None:
    async def scenario() -> None:
        session = await _loaded_agent()
        await session.dispose(clear_sensitive=True)
        assert session.disposed is True
        assert session.store.draft == ""
        assert session.store.error is None
        assert len(session.output_log) == 0
        assert bytes(session.output_log) == b""
        assert session.selection_sheet_text == ""
        assert session.notice == ""

    run(scenario())


def test_shell_ordinary_dispose_preserves_contents() -> None:
    async def scenario() -> None:
        session = await _loaded_shell()
        await session.dispose()
        assert session.disposed is True
        assert bytes(session.output_log) == b"secret output"
        assert session.selection_sheet_text == "secret screen"
        assert session.notice != ""

    run(scenario())


def test_shell_revoke_dispose_wipes_sensitive_caches() -> None:
    async def scenario() -> None:
        session = await _loaded_shell()
        await session.dispose(clear_sensitive=True)
        assert session.disposed is True
        assert len(session.output_log) == 0
        assert bytes(session.output_log) == b""
        assert session.selection_sheet_text == ""
        assert session.notice == ""

    run(scenario())


def test_live_transitions_preserve_draft() -> None:
    async def scenario() -> None:
        session, _, _, _ = _agent_session()
        session.open()
        assert await session.confirm_takeover() == AGENT_LIVE
        session.on_draft_change("unsent work")
        session.set_workflow(WORKFLOW_AGENT_DIRECT)
        session.set_workflow(WORKFLOW_AGENT_COMPOSER)
        assert session.store.draft == "unsent work"
        session.set_keyboard(KEYBOARD_KEYS)
        session.set_keyboard(KEYBOARD_TEXT)
        assert session.store.draft == "unsent work"
        session.open_selection()
        session.close_selection()
        assert session.store.draft == "unsent work"
        await session.disconnect()
        assert session.store.draft == "unsent work"

    run(scenario())
