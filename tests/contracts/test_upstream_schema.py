"""frozen upstream compatibility evidence.

Compares the pinned Herdr 0.9.1 schema export against the recorded method
list, checks golden fixture envelope shapes, and runs read-only live probes
(ping, unknown-method rejection) only with explicit RELAYDECK_LIVE=1 opt-in.
"""

from __future__ import annotations

import json
import os
import socket
from pathlib import Path
from typing import Any

import pytest

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "upstream"
SCHEMA_SHA256 = "226d4ecbd128d2e6bc84e4c8ddcec21ba9c7e51a0aafffcf087111ead3f1fa9a"

REQUIRED_METHODS = {
    "ping",
    "session.snapshot",
    "agent.list",
    "agent.get",
    "agent.read",
    "agent.prompt",
    "agent.start",
    "agent.send_keys",
    "pane.read",
    "pane.send_text",
    "pane.send_keys",
    "pane.send_input",
    "pane.close",
    "tab.create",
    "tab.close",
    "workspace.create",
    "workspace.close",
    "events.subscribe",
}

REQUIRED_RESULTS = {
    "pong",
    "session_snapshot",
    "agent_list",
    "agent_info",
    "agent_prompted",
    "agent_started",
    "pane_read",
    "tab_created",
    "workspace_created",
    "subscription_started",
    "ok",
}


def _load_schema() -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIXTURES / "herdr-0.9.1-schema.json").read_text())
    return data


def _method_names(schema: dict[str, Any]) -> set[str]:
    return {v["properties"]["method"]["const"] for v in schema["schemas"]["request"]["oneOf"]}


def _result_types(schema: dict[str, Any]) -> set[str]:
    variants = schema["schemas"]["success_response"]["$defs"]["ResponseResult"]["oneOf"]
    return {v["properties"]["type"]["const"] for v in variants}


def test_schema_export_fingerprint_and_protocol() -> None:
    import hashlib

    raw = (FIXTURES / "herdr-0.9.1-schema.json").read_bytes()
    assert hashlib.sha256(raw).hexdigest() == SCHEMA_SHA256
    schema = json.loads(raw)
    assert schema["protocol"] == 22


def test_method_set_matches_recorded_baseline() -> None:
    """Any drift reopens; update live_methods.txt deliberately."""
    schema = _load_schema()
    recorded = (FIXTURES / "live_methods.txt").read_text().split()
    assert sorted(_method_names(schema)) == sorted(recorded)


def test_required_methods_present_and_forbidden_absent() -> None:
    methods = _method_names(_load_schema())
    assert REQUIRED_METHODS <= methods
    for name in methods:
        assert "approv" not in name, name
        assert "transcript" not in name, name
        assert not name.endswith(".exec"), name


def test_required_result_types_present() -> None:
    assert REQUIRED_RESULTS <= _result_types(_load_schema())


def test_golden_success_envelopes() -> None:
    for name in (
        "ping.pong.json",
        "session_snapshot.json",
        "agent_list.json",
        "agent_prompted.json",
        "subscription_started.json",
    ):
        line = json.loads((FIXTURES / name).read_text())
        assert isinstance(line["id"], str) and line["id"]
        assert isinstance(line["result"]["type"], str)
        assert "error" not in line


def test_golden_error_envelopes() -> None:
    for name, code in (
        ("error_agent_blocked.json", "agent_blocked"),
        ("error_agent_not_ready.json", "agent_not_ready"),
        ("error_agent_not_found.json", "agent_not_found"),
        ("error_pane_not_found.json", "pane_not_found"),
        ("error_invalid_request.json", "invalid_request"),
    ):
        line = json.loads((FIXTURES / name).read_text())
        assert isinstance(line["id"], str)
        assert line["error"]["code"] == code
        assert isinstance(line["error"]["message"], str)
        assert "result" not in line


def test_golden_event_lines_have_no_id() -> None:
    for name in ("event_agent_status.json", "event_workspace_updated.json"):
        line = json.loads((FIXTURES / name).read_text())
        assert "id" not in line
        assert isinstance(line["event"], str)
        assert isinstance(line["data"], dict)


def _socket_path() -> str:
    return os.path.expanduser("~/.config/herdr/herdr.sock")


def _call_live(payload: bytes) -> str:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(5)
    try:
        sock.connect(_socket_path())
        sock.sendall(payload)
        data = b""
        while not data.endswith(b"\n"):
            chunk = sock.recv(65536)
            if not chunk:
                break
            data += chunk
        return data.decode()
    finally:
        sock.close()


live = pytest.mark.skipif(
    os.environ.get("RELAYDECK_LIVE") != "1", reason="live Herdr requires RELAYDECK_LIVE=1"
)


@live
def test_live_ping_reports_protocol_22() -> None:
    line = json.loads(_call_live(b'{"id":"rd-live-1","method":"ping","params":{}}\n'))
    assert line["id"] == "rd-live-1"
    assert line["result"]["type"] == "pong"
    assert line["result"]["protocol"] == 22


@live
def test_live_unknown_method_fails_closed() -> None:
    line = json.loads(_call_live(b'{"id":"rd-live-2","method":"agent.approve","params":{}}\n'))
    assert "result" not in line
    assert isinstance(line["error"]["code"], str)


@live
def test_live_integer_id_rejected() -> None:
    line = json.loads(_call_live(b'{"id":1,"method":"ping","params":{}}\n'))
    assert "result" not in line
    assert line["error"]["code"] == "invalid_request"
