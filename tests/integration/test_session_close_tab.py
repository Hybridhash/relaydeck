"""session.close closes the Herdr TAB for a shell's tab id, the pane for a pane id,
and fails clearly for an unknown id. Synthetic fixture adapter and a temporary journal.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from relaydeck.connector.fixture import FixtureAdapter
from relaydeck.connector.journal import ConnectorDispatch
from relaydeck.journal import CommandJournal
from tests.support.creation.command_journal import NOW, _dispatch_msg


async def _shell(dispatch: ConnectorDispatch, command_id: str) -> dict[str, Any]:
    result = await dispatch.dispatch(
        _dispatch_msg(command_id, "shell.create", {"workspace_id": "w1"}, {"label": "ops"})
    )
    assert result["status"] == "succeeded", result
    return result


def _close(dispatch: ConnectorDispatch, command_id: str, terminal_id: str) -> Any:
    return dispatch.dispatch(
        _dispatch_msg(command_id, "session.close", {"terminal_id": terminal_id}, {})
    )


def test_close_with_a_tab_id_closes_the_tab(tmp_path: Path) -> None:
    asyncio.run(_test_close_with_a_tab_id_closes_the_tab(tmp_path))


async def _test_close_with_a_tab_id_closes_the_tab(tmp_path: Path) -> None:
    journal = CommandJournal(tmp_path / "c.db", clock=lambda: NOW)
    adapter = FixtureAdapter()
    dispatch = ConnectorDispatch(journal, adapter, host_id="home-mac", clock=lambda: NOW)
    try:
        await _shell(dispatch, "close-tab-1")
        tab_id = next(iter(adapter._tabs))
        pane_id = next(iter(adapter._panes))
        closed = await _close(dispatch, "close-tab-2", tab_id)
        assert closed["status"] == "succeeded", closed
        assert ("tab.close", {"tab_id": tab_id}) in adapter._calls
        assert tab_id not in adapter._tabs and pane_id not in adapter._panes
    finally:
        journal.close()


def test_close_with_a_pane_id_closes_the_pane(tmp_path: Path) -> None:
    asyncio.run(_test_close_with_a_pane_id_closes_the_pane(tmp_path))


async def _test_close_with_a_pane_id_closes_the_pane(tmp_path: Path) -> None:
    journal = CommandJournal(tmp_path / "c.db", clock=lambda: NOW)
    adapter = FixtureAdapter()
    dispatch = ConnectorDispatch(journal, adapter, host_id="home-mac", clock=lambda: NOW)
    try:
        await _shell(dispatch, "close-pane-1")
        pane_id = next(iter(adapter._panes))
        closed = await _close(dispatch, "close-pane-2", pane_id)
        assert closed["status"] == "succeeded", closed
        assert ("pane.close", {"pane_id": pane_id}) in adapter._calls
        assert not any(call[0] == "tab.close" for call in adapter._calls)
    finally:
        journal.close()


def test_close_with_an_unknown_id_fails_with_a_clear_code(tmp_path: Path) -> None:
    asyncio.run(_test_close_with_an_unknown_id_fails_with_a_clear_code(tmp_path))


async def _test_close_with_an_unknown_id_fails_with_a_clear_code(tmp_path: Path) -> None:
    journal = CommandJournal(tmp_path / "c.db", clock=lambda: NOW)
    adapter = FixtureAdapter()
    dispatch = ConnectorDispatch(journal, adapter, host_id="home-mac", clock=lambda: NOW)
    try:
        failed = await _close(dispatch, "close-unknown-1", "w9:tZ")
        assert failed["status"] == "failed", failed
        assert failed["error_code"] == "no_such_tab", failed
    finally:
        journal.close()
