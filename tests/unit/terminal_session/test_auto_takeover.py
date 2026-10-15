"""Opening an agent takes control automatically only when nobody else holds it."""

from __future__ import annotations

import asyncio
import copy
from typing import Any

import pytest

from relaydeck.client.features.settings.prefs import TerminalPrefs, clean
from relaydeck.client.features.shell.ops import ScriptedShellGatewayOps, ShellOpError
from relaydeck.client.features.terminal.agent_session import AGENT_CONFLICT
from relaydeck.gateway.leases import LeaseStore
from tests.support.terminal.sessions import _agent_session


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def test_free_agent_is_taken_automatically() -> None:
    session, ops, _factory, _ = _agent_session()

    async def go() -> None:
        session.open()
        await session.auto_take_control()

    _run(go())
    assert [a["takeover"] for a in ops.attaches] == [True]
    assert session.lease is not None and session.lease.mode == "control"


def test_other_device_in_control_gets_the_prompt_not_a_takeover() -> None:
    ops = ScriptedShellGatewayOps()
    info = ops.seed_owner("home-mac", "sess:1", "dev-other", "Other iPad")
    ops._control_holder[info.attachment_id] = (ops.account, "dev-other")
    session, ops, _factory, _ = _agent_session(ops=ops)

    async def go() -> None:
        session.open()
        await session.auto_take_control()

    _run(go())
    assert ops.attaches == []  # nothing attached, nobody displaced
    assert session.phase == AGENT_CONFLICT


def test_unknown_control_state_leaves_the_manual_take_over() -> None:
    class Unknown(ScriptedShellGatewayOps):
        async def control_holder(self, host_id: str, terminal_id: str) -> dict[str, Any]:
            raise ShellOpError("unsupported_capability", "unknown")

    session, ops, _factory, _ = _agent_session(ops=Unknown())

    async def go() -> None:
        session.open()
        await session.auto_take_control()

    _run(go())
    assert ops.attaches == []


def test_already_in_control_does_not_attach_again() -> None:
    session, ops, _factory, _ = _agent_session()

    async def go() -> None:
        session.open()
        await session.auto_take_control()
        await session.auto_take_control()

    _run(go())
    assert len(ops.attaches) == 1


@pytest.mark.parametrize(
    "state",
    [
        {"held": False, "mine": False},
        {"held": False, "mine": False, "control_revision": None},
        {"held": "false", "mine": False, "control_revision": "a" * 64},
        {"held": False, "mine": True, "control_revision": "a" * 64},
    ],
)
def test_legacy_or_malformed_holder_requires_manual_takeover(state: dict[str, Any]) -> None:
    class Legacy(ScriptedShellGatewayOps):
        async def control_holder(self, host_id: str, terminal_id: str) -> dict[str, Any]:
            return state

    session, ops, _factory, _ = _agent_session(ops=Legacy())

    async def go() -> None:
        session.open()
        await session.auto_take_control()

    _run(go())
    assert not ops.attaches and session.lease is None


def test_lease_store_reports_holder_read_only() -> None:
    store = LeaseStore()
    before = copy.deepcopy(vars(store))
    state = store.control_holder("h", "t", "acct", "dev-a", 0)
    assert state["held"] is False and state["mine"] is False
    assert len(state["control_revision"]) == 64
    assert state == store.control_holder("h", "t", "acct", "dev-a", 0)
    assert vars(store) == before


def test_auto_takeover_pref_defaults_on_and_round_trips() -> None:
    assert TerminalPrefs().auto_takeover is True
    assert clean({"auto_takeover": False}).auto_takeover is False
    assert clean({"auto_takeover": "yes"}).auto_takeover is True
