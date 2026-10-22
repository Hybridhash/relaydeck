"""gateway schema contracts."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from relaydeck.contracts.schemas import SCHEMAS, SchemaError, validate

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "gateway"

EXAMPLES = {
    "relaydeck/gateway/v1/terminal-attach-request": "terminal-attach-request.json",
    "relaydeck/gateway/v1/terminal-control-holder": "terminal-control-holder.json",
    "relaydeck/gateway/v1/terminal-lease-request": "terminal-lease-request.json",
    "relaydeck/gateway/v1/pairing-request": "pairing-request.json",
    "relaydeck/gateway/v1/pairing-proof": "pairing-proof.json",
    "relaydeck/gateway/v1/pairing-status": "pairing-status.json",
    "relaydeck/gateway/v1/picture-request": "picture-request.json",
    "relaydeck/gateway/v1/picture-grant": "picture-grant.json",
    "relaydeck/gateway/v1/picture-result": "picture-result.json",
    "relaydeck/gateway/v1/server-metrics": "server-metrics.json",
    "relaydeck/gateway/v1/authorization-request": "authorization-request.json",
    "relaydeck/gateway/v1/snapshot": "snapshot.json",
    "relaydeck/gateway/v1/metadata-event": "metadata-event.json",
    "relaydeck/gateway/v1/command-submit": "command-submit.json",
    "relaydeck/gateway/v1/command-record": "command-record.json",
    "relaydeck/gateway/v1/command-dispatch": "command-dispatch.json",
    "relaydeck/gateway/v1/error": "error.json",
    "relaydeck/gateway/v1/token-response": "token-response.json",
    "relaydeck/gateway/v1/terminal-control": "terminal-control-ack.json",
    "relaydeck/gateway/v1/terminal-ticket-request": "terminal-ticket-request.json",
    "relaydeck/gateway/v1/terminal-ticket": "terminal-ticket.json",
    "relaydeck/gateway/v1/enrollment-create": "enrollment-create.json",
    "relaydeck/gateway/v1/enrollment": "enrollment.json",
    "relaydeck/gateway/v1/enrollment-redeem": "enrollment-redeem.json",
    "relaydeck/gateway/v1/enrollment-result": "enrollment-result.json",
    "relaydeck/gateway/v1/me": "me.json",
    "relaydeck/gateway/v1/hosts": "hosts.json",
    "relaydeck/gateway/v1/agents-page": "agents-page.json",
    "relaydeck/gateway/v1/agent-read": "agent-read.json",
    "relaydeck/gateway/v1/devices": "devices.json",
    "relaydeck/gateway/v1/device-revoke": "device-revoke.json",
    "relaydeck/gateway/v1/device-registration": "device-registration.json",
    "relaydeck/gateway/v1/deep-link-resolution": "deep-link-resolution.json",
    "relaydeck/gateway/v1/host-revoke": "host-revoke.json",
    "relaydeck/gateway/v1/enrollment-status": "enrollment-status.json",
}


def _load(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text())


def test_every_schema_has_a_valid_example() -> None:
    covered = set(EXAMPLES)
    covered |= {
        "relaydeck/gateway/v1/token-exchange",
        "relaydeck/gateway/v1/nonce-claim",
    }
    assert covered == set(SCHEMAS)
    for schema_id, name in EXAMPLES.items():
        validate(schema_id, _load(name))
    validate(
        "relaydeck/gateway/v1/token-exchange",
        {"grant": "authorization_code", "code": "code-12345678", "verifier": "v" * 43},
    )
    validate(
        "relaydeck/gateway/v1/token-exchange",
        {"grant": "refresh_token", "refresh_token": "r" * 16},
    )
    validate(
        "relaydeck/gateway/v1/nonce-claim",
        {"nonce": "n" * 16, "host_id": "home-mac"},
    )


def test_unknown_schema_rejected() -> None:
    with pytest.raises(SchemaError):
        validate("relaydeck/gateway/v9/nope", {})


@pytest.mark.parametrize("value", [None, True, 1, "", "a" * 63, "a" * 65, "A" * 64])
def test_terminal_holder_revision_shape_is_closed(value: Any) -> None:
    for schema_id, field, base in (
        ("terminal-attach-request", "expected_control_revision", {"kind": "agent"}),
        ("terminal-control-holder", "control_revision", {"held": False, "mine": False}),
    ):
        with pytest.raises(SchemaError):
            validate(f"relaydeck/gateway/v1/{schema_id}", {**base, field: value})


def test_terminal_ownership_fields_remain_optional_for_legacy_requests() -> None:
    validate("relaydeck/gateway/v1/terminal-attach-request", {"kind": "agent", "takeover": True})
    validate("relaydeck/gateway/v1/terminal-lease-request", {"mode": "control"})
    for value in (None, True, 0, -1, 4294967296):
        with pytest.raises(SchemaError):
            validate(
                "relaydeck/gateway/v1/terminal-lease-request",
                {
                    "mode": "control",
                    "expected_generation": value,
                },
            )


def test_unknown_fields_rejected() -> None:
    doc = _load("command-record.json")
    doc["transcript"] = "rm -rf /"
    with pytest.raises(SchemaError):
        validate("relaydeck/gateway/v1/command-record", doc)


def test_bad_kinds_and_statuses_rejected() -> None:
    doc = _load("command-submit.json")
    bad = copy.deepcopy(doc)
    bad["kind"] = "agent.approve"
    with pytest.raises(SchemaError):
        validate("relaydeck/gateway/v1/command-submit", bad)
    bad = copy.deepcopy(doc)
    bad["command_id"] = "has spaces!"
    with pytest.raises(SchemaError):
        validate("relaydeck/gateway/v1/command-submit", bad)
    rec = _load("command-record.json")
    bad = copy.deepcopy(rec)
    bad["status"] = "approved"
    with pytest.raises(SchemaError):
        validate("relaydeck/gateway/v1/command-record", bad)


def test_missing_required_rejected() -> None:
    doc = _load("snapshot.json")
    bad = copy.deepcopy(doc)
    del bad["agents"]
    with pytest.raises(SchemaError):
        validate("relaydeck/gateway/v1/snapshot", bad)
    bad = copy.deepcopy(doc)
    bad["agents"][0]["revision"] = -1
    with pytest.raises(SchemaError):
        validate("relaydeck/gateway/v1/snapshot", bad)


def test_error_codes_closed() -> None:
    doc = _load("error.json")
    bad = copy.deepcopy(doc)
    bad["code"] = "something_else"
    with pytest.raises(SchemaError):
        validate("relaydeck/gateway/v1/error", bad)


def test_terminal_control_bounds() -> None:
    doc = _load("terminal-control-ack.json")
    bad = copy.deepcopy(doc)
    bad["applied"] = -1
    with pytest.raises(SchemaError):
        validate("relaydeck/gateway/v1/terminal-control", bad)
    bad = copy.deepcopy(doc)
    bad["kind"] = "resize"
    with pytest.raises(SchemaError):
        validate("relaydeck/gateway/v1/terminal-control", bad)
    bad = copy.deepcopy(doc)
    bad["kind"] = "paste"
    with pytest.raises(SchemaError):
        validate("relaydeck/gateway/v1/terminal-control", bad)


def test_token_shapes_bounded() -> None:
    with pytest.raises(SchemaError):
        validate("relaydeck/gateway/v1/token-exchange", {"grant": "password"})
    with pytest.raises(SchemaError):
        validate(
            "relaydeck/gateway/v1/token-response",
            {"access_token": "short", "refresh_token": "r" * 16, "expires_in": 900},
        )


def test_input_failure_control_example_is_generation_bound() -> None:
    doc = _load("terminal-control-input-failed.json")
    validate("relaydeck/gateway/v1/terminal-control", doc)
    for field in ("generation", "input_seq"):
        for invalid in (True, 0, -1, 0x100000000):
            bad = dict(doc, **{field: invalid})
            with pytest.raises(SchemaError):
                validate("relaydeck/gateway/v1/terminal-control", bad)


def test_new_workspace_launch_and_bounded_creation_results() -> None:
    validate("relaydeck/gateway/v1/command-submit", _load("command-new-workspace.json"))
    for name in ("command-created.json", "command-partial-creation.json"):
        validate("relaydeck/gateway/v1/command-record", _load(name))
        bad = _load(name)
        bad["result"]["terminal_text"] = "private"
        with pytest.raises(SchemaError):
            validate("relaydeck/gateway/v1/command-record", bad)
