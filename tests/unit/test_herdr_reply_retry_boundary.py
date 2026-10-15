"""Only an unambiguous, correlated upstream refusal can authorize retry."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import relaydeck.connector.journal as dispatch_module
from relaydeck.connector.herdr import HerdrAdapter
from relaydeck.connector.journal import ConnectorDispatch
from relaydeck.journal import CommandJournal
from tests.support.creation.command_journal import NOW
from tests.support.creation.create_readiness import _message
from tests.support.gateway.herdr_adapter import _FakeReader, _FakeWriter
from tests.support.host_harness import run

BUSY = {"code": "agent_pane_busy", "message": "synthetic shell initialization"}


async def _dispatch_reply(
    path: Path,
    reply: dict[str, Any] | bytes,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    writers: list[_FakeWriter] = []
    waits: list[float] = []

    async def opener() -> tuple[_FakeReader, _FakeWriter]:
        index = len(writers) + 1
        writer = _FakeWriter()
        writers.append(writer)
        if index == 1:
            doc = {
                "id": "rd-1",
                "result": {
                    "tab": {"tab_id": "w1:t7"},
                    "root_pane": {"pane_id": "w1:p7"},
                },
            }
        elif index == 2:
            doc = reply
        else:
            doc = {"id": f"rd-{index}", "result": {"agent": {"terminal_id": "agent-1"}}}
        raw = doc if isinstance(doc, bytes) else (json.dumps(doc) + "\n").encode()
        return _FakeReader([raw]), writer

    async def wait(delay: float) -> None:
        waits.append(delay)

    monkeypatch.setattr(dispatch_module, "sleep", wait)
    journal = CommandJournal(path, clock=lambda: NOW)
    try:
        dispatch = ConnectorDispatch(
            journal, HerdrAdapter(opener=opener), host_id="home-mac", clock=lambda: NOW
        )
        message = _message()
        result = await dispatch.dispatch(message)
        stored = dispatch.reconcile(message["command_id"])
        assert stored is not None
        writes = [json.loads(writer.written) for writer in writers]
        assert all(writer.closed for writer in writers)
        # Settled and unknown redelivery must never re-execute or resume the loop.
        before = len(writers)
        duplicate = await dispatch.dispatch(message)
        assert duplicate["status"] == result["status"]
        if result["status"] != "unknown":
            assert duplicate == result
        assert len(writers) == before
        if result["status"] == "unknown":
            assert waits == []
        else:
            assert waits == [0.1]
        return result, stored, writes
    finally:
        journal.close()


@pytest.mark.parametrize(
    "reply",
    [
        {"error": BUSY},
        {"id": "unrelated-request", "error": BUSY},
        {"id": "rd-2", "result": {"agent": {"terminal_id": "already-launched"}}, "error": BUSY},
    ],
    ids=["missing-id", "wrong-id", "result-and-error"],
)
def test_uncorrelated_or_contradictory_busy_never_authorizes_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reply: dict[str, Any],
) -> None:
    async def scenario() -> None:
        result, stored, writes = await _dispatch_reply(tmp_path / "commands.db", reply, monkeypatch)
        assert result["status"] == stored["status"] == "unknown"
        assert result["error_code"] == "bad_envelope"
        assert [w["method"] for w in writes] == ["tab.create", "agent.start"]
        assert stored["progress"] == {"workspace_id": "w1", "tab_id": "w1:t7", "pane_id": "w1:p7"}
        assert writes[-1]["params"]["pane_id"] == "w1:p7"

    run(scenario())


@pytest.mark.parametrize(
    "error",
    [
        None,
        "agent_pane_busy",
        [],
        {},
        {"message": "missing code"},
        {"code": None, "message": "null code"},
        {"code": 17, "message": "non-string code"},
        {"code": "", "message": "empty code"},
        {"code": "agent_pane_busy"},
        {"code": "agent_pane_busy", "message": None},
        {"code": "agent_pane_busy", "message": ["not a string"]},
    ],
)
def test_malformed_error_body_is_unknown_and_keeps_partial_identities(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: Any,
) -> None:
    async def scenario() -> None:
        result, stored, writes = await _dispatch_reply(
            tmp_path / "commands.db",
            {"id": "rd-2", "error": error},
            monkeypatch,
        )
        assert result["status"] == stored["status"] == "unknown"
        assert result["error_code"] == "bad_envelope"
        assert [w["method"] for w in writes] == ["tab.create", "agent.start"]
        assert stored["progress"]["tab_id"] == "w1:t7"
        assert stored["progress"]["pane_id"] == "w1:p7"

    run(scenario())


def test_correlated_busy_refusal_still_retries_the_same_pane_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        result, stored, writes = await _dispatch_reply(
            tmp_path / "commands.db",
            {"id": "rd-2", "error": BUSY},
            monkeypatch,
        )
        assert result["status"] == stored["status"] == "succeeded"
        assert result["result"]["agent_id"] == "agent-1"
        assert [w["method"] for w in writes] == ["tab.create", "agent.start", "agent.start"]
        assert writes[1]["params"] == writes[2]["params"]
        assert writes[1]["id"] == "rd-2" and writes[2]["id"] == "rd-3"

    run(scenario())


@pytest.mark.parametrize(
    "reply",
    [
        b'{"id":"unrelated","id":"rd-2","error":{"code":"agent_pane_busy","message":"m"}}\n',
        b'{"id":"rd-2","error":{"code":"agent_not_ready","code":"agent_pane_busy","message":"m"}}\n',
    ],
    ids=["duplicate-id", "duplicate-code"],
)
def test_duplicate_properties_cannot_authorize_a_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reply: bytes,
) -> None:
    async def scenario() -> None:
        result, stored, writes = await _dispatch_reply(tmp_path / "commands.db", reply, monkeypatch)
        assert result["status"] == stored["status"] == "unknown"
        assert [w["method"] for w in writes] == ["tab.create", "agent.start"]
        assert stored["progress"]["pane_id"] == "w1:p7"

    run(scenario())
