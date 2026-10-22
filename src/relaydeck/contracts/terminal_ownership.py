"""Optional compare-and-acquire fields for gateway terminal ownership."""

import re
from typing import Any, Literal, NotRequired, TypedDict

CONTROL_REVISION_PATTERN = r"^[0-9a-f]{64}$"


def is_control_revision(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(CONTROL_REVISION_PATTERN, value) is not None


TERMINAL_OWNERSHIP_SCHEMAS: dict[str, dict[str, Any]] = {
    "relaydeck/gateway/v1/terminal-attach-request": {
        "type": "object",
        "additionalProperties": False,
        "required": ["kind"],
        "properties": {
            "kind": {"enum": ["agent", "shell"]},
            "takeover": {"type": "boolean"},
            "cols": {"type": "integer", "minimum": 1, "maximum": 1024},
            "rows": {"type": "integer", "minimum": 1, "maximum": 1024},
            "expected_control_revision": {
                "type": "string",
                "minLength": 64,
                "maxLength": 64,
                "pattern": CONTROL_REVISION_PATTERN,
            },
        },
    },
    "relaydeck/gateway/v1/terminal-control-holder": {
        "type": "object",
        "additionalProperties": False,
        "required": ["held", "mine", "control_revision"],
        "properties": {
            "held": {"type": "boolean"},
            "mine": {"type": "boolean"},
            "control_revision": {
                "type": "string",
                "minLength": 64,
                "maxLength": 64,
                "pattern": CONTROL_REVISION_PATTERN,
            },
            "holder": {
                "type": "object",
                "additionalProperties": False,
                "required": ["device_id"],
                "properties": {"device_id": {"type": "string", "maxLength": 256}},
            },
        },
    },
    "relaydeck/gateway/v1/terminal-lease-request": {
        "type": "object",
        "additionalProperties": False,
        "required": ["mode"],
        "properties": {
            "mode": {"enum": ["view", "control"]},
            "expected_generation": {"type": "integer", "minimum": 1, "maximum": 4294967295},
        },
        "oneOf": [
            {"properties": {"mode": {"const": "control"}}},
            {"properties": {"mode": {"const": "view"}}, "additionalProperties": False},
        ],
    },
}


# Typed bodies for the schemas above; tests keep them identical to the JSON Schemas.
class TerminalAttachRequest(TypedDict):
    kind: Literal["agent", "shell"]
    takeover: NotRequired[bool]
    cols: NotRequired[int]
    rows: NotRequired[int]
    expected_control_revision: NotRequired[str]


class TerminalControlHolderHolder(TypedDict):
    device_id: str


class TerminalControlHolder(TypedDict):
    held: bool
    mine: bool
    control_revision: str
    holder: NotRequired[TerminalControlHolderHolder]


class TerminalLeaseRequest(TypedDict):
    mode: Literal["view", "control"]
    expected_generation: NotRequired[int]
