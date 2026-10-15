"""Composer auth-loss fencing regressions.

Losing PTY ownership (conflict/takeover/disconnect) revokes the
terminal writer but the Composer stays valid; losing identity/host
authorization (`unauthenticated`/`forbidden` renewal failure) must
fence ALL composer writes — saved and newly obtained handlers alike —
while preserving the draft. Before the fix, a saved `on_send` still
delivered via prompt after auth loss while the notice said signed out.
"""

from __future__ import annotations

from typing import Any

import pytest

from relaydeck.client.features.composer.components import DRAFT_KEY, SEND_KEY
from relaydeck.client.features.composer.domain import (
    SEND_DELIVERED_VIA_PROMPT,
    SEND_IGNORED,
)
from relaydeck.client.features.sync.transport import TransportError
from relaydeck.client.features.terminal.agent_session import AGENT_LIVE
from relaydeck.client.features.terminal.surfaces import mount_agent_screen
from tests.support.host_harness import render_component, run
from tests.support.terminal.binding import _wait_for
from tests.support.terminal.sessions import _agent_session


@pytest.mark.parametrize("code", ["unauthenticated", "forbidden"])
def test_saved_send_fenced_after_auth_loss(code: str) -> None:
    async def scenario() -> None:
        session, _, _, submitted = _agent_session()
        session.open()
        assert await session.confirm_takeover() == AGENT_LIVE
        session.on_draft_change("unsent work")
        saved = session.composer_callbacks()["on_send"]
        session._on_renewal_terminal(code)
        assert await saved() == SEND_IGNORED
        assert submitted == []
        assert session.store.draft == "unsent work"
        assert "sign in" in session.notice

    run(scenario())


def test_dispatched_send_fenced_after_auth_loss() -> None:
    async def scenario() -> None:
        session, _, _, submitted = _agent_session()
        session.open()
        assert await session.confirm_takeover() == AGENT_LIVE

        async def renew(lease_id: str) -> dict[str, Any]:
            raise TransportError("unauthenticated", "signed out", retryable=False)

        session.start_lease_renewal(renew_lease=renew, interval_s=0.01)
        await _wait_for(lambda: "sign in" in session.notice)
        app = await render_component(mount_agent_screen, session)
        await app.set_props_and_fire(DRAFT_KEY, {"value": "unsent work"}, "change", "unsent work")
        assert session.store.draft == "unsent work"
        await app.set_props_and_fire(SEND_KEY, {}, "click")
        assert submitted == []
        assert session.store.draft == "unsent work"
        # Fresh closures after the notice rerender are fenced too.
        await app.set_props_and_fire(SEND_KEY, {}, "click")
        assert submitted == []
        assert session.store.draft == "unsent work"
        await session.stop_lease_renewal()

    run(scenario())


def test_ownership_loss_keeps_composer_valid() -> None:
    async def scenario() -> None:
        session, _, _, submitted = _agent_session()
        session.open()
        assert await session.confirm_takeover() == AGENT_LIVE
        app = await render_component(mount_agent_screen, session)
        await app.set_props_and_fire(DRAFT_KEY, {"value": "still mine"}, "change", "still mine")
        session._on_renewal_terminal("lease_conflict")
        assert "sign in" not in session.notice
        await app.set_props_and_fire(SEND_KEY, {}, "click")
        assert len(submitted) == 1
        assert session.store.draft == ""

    run(scenario())


def test_composer_rearmed_on_retakeover() -> None:
    async def scenario() -> None:
        session, _, _, submitted = _agent_session()
        session.open()
        assert await session.confirm_takeover() == AGENT_LIVE
        session.on_draft_change("unsent work")
        session._on_renewal_terminal("unauthenticated")
        assert await session.composer_callbacks()["on_send"]() == SEND_IGNORED
        assert submitted == []
        # A fresh authorized takeover re-arms composer writes.
        session.open()
        assert await session.confirm_takeover() == AGENT_LIVE
        assert await session.composer_callbacks()["on_send"]() == SEND_DELIVERED_VIA_PROMPT
        assert len(submitted) == 1

    run(scenario())
