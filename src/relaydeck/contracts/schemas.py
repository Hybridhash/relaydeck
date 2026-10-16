"""Versioned gateway JSON schemas.

Every client↔gateway JSON document validates against exactly one schema
here. Schemas use a constrained Draft 2020-12 subset enforced by
`validate` in this module (stdlib only, no third-party validator).
Wire version: v1. Unknown fields are rejected unless a schema opts in.
"""

from __future__ import annotations

import re
from typing import Any

from relaydeck.contracts.catalog import CATALOG_SCHEMA
from relaydeck.contracts.launch import RESULT_SCHEMA
from relaydeck.contracts.pairing import PAIRING_SCHEMAS
from relaydeck.contracts.pictures import PICTURE_SCHEMAS
from relaydeck.contracts.terminal_ownership import TERMINAL_OWNERSHIP_SCHEMAS

WIRE_VERSION = "v1"

# envelope marker. Write paths and the event stream carry
# it so a Herdr passthrough line can never be mistaken for a gateway
# document (and vice versa). Auth-layer envelopes (error, token) omit it.
GATEWAY_PROTOCOL = "relaydeck.gateway.v1"

# Strict UTC RFC 3339 (`Z` only; the gateway mints and compares these).
RFC3339_UTC = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$"

CAPABILITIES = (
    "sessions.read",
    "terminal.view",
    "terminal.control",
    "agents.prompt",
    "agents.start",
    "shells.create",
    "terminal.attach",
    "hosts.enroll",
    "attachments.upload",
)

COMMAND_KINDS = (
    "agent.prompt",
    "agent.start",
    "shell.create",
    "session.close",
)

COMMAND_STATUSES = (
    "accepted",
    "dispatched",
    "succeeded",
    "failed",
    "rejected",
    "unknown",
)

# stable envelope codes (exactly these; see mapping below).
ERROR_CODES = (
    "unauthenticated",
    "forbidden",
    "not_found",
    "stale_epoch",
    "stale_revision",
    "command_conflict",
    "unsupported_capability",
    "host_offline",
    "lease_conflict",
    "lease_expired",
    "upstream_incompatible",
    "outcome_unknown",
    "too_large",
    "rate_limited",
)

# Deliberate error mappings:
# - Malformed JSON, schema violations, unknown fields -> 400 + forbidden (details.reason).
# - Unknown operation kinds -> 400 + unsupported_capability; missing capability -> 403 + same.
# - Unknown host/command/agent/pane/tab/workspace -> 404 + not_found.
# - Invalid/expired/forged cursor -> 409 + stale_epoch, details.reason=cursor (resnapshot).
# - Stale writer generation -> 409 + stale_revision.
# - Herdr transport failure -> 503 + host_offline (retryable); protocol drift -> 500 +
#   upstream_incompatible (not retryable).
# - Herdr command outcomes (agent_blocked, agent_not_ready, pane_not_found, ...) are not envelope
#   codes: they ride command records and event payloads as details.upstream_code.
# - Unhandled gateway faults -> 500 + upstream_incompatible with a safe correlation id only.
ERROR_STATUS = {
    "unauthenticated": 401,
    "forbidden": 400,
    "not_found": 404,
    "stale_epoch": 409,
    "stale_revision": 409,
    "command_conflict": 409,
    "unsupported_capability": 403,
    "host_offline": 503,
    "lease_conflict": 409,
    "lease_expired": 410,
    "upstream_incompatible": 500,
    "outcome_unknown": 409,
    "too_large": 413,
    "rate_limited": 429,
}

SCHEMAS: dict[str, dict[str, Any]] = {
    "relaydeck/gateway/v1/snapshot": {
        "$id": "relaydeck/gateway/v1/snapshot",
        "type": "object",
        "required": ["host", "cursor", "workspaces", "tabs", "panes", "agents"],
        "additionalProperties": False,
        "properties": {
            "host": {"$ref": "#/$defs/host"},
            "cursor": {"type": "string", "minLength": 1, "maxLength": 512},
            "workspaces": {"type": "array", "items": {"$ref": "#/$defs/workspace"}},
            "tabs": {"type": "array", "items": {"$ref": "#/$defs/tab"}},
            "panes": {"type": "array", "items": {"$ref": "#/$defs/pane"}},
            "agents": {"type": "array", "items": {"$ref": "#/$defs/agent"}},
        },
        "$defs": {
            "host": {
                "type": "object",
                "required": ["id", "epoch", "reachable"],
                "additionalProperties": False,
                "properties": {
                    "id": {"type": "string", "minLength": 1, "maxLength": 128},
                    "label": {"type": "string", "maxLength": 128},
                    "agent_catalog": CATALOG_SCHEMA,
                    "epoch": {"type": "string", "minLength": 1, "maxLength": 128},
                    "reachable": {"type": "boolean"},
                    "protocol": {"type": "integer", "minimum": 0},
                    "version": {"type": "string", "maxLength": 32},
                    "capabilities": {
                        "type": "array",
                        "items": {"type": "string", "enum": list(CAPABILITIES)},
                    },
                },
            },
            "workspace": {
                "type": "object",
                "required": ["id", "host_id"],
                "additionalProperties": False,
                "properties": {
                    "id": {"type": "string", "minLength": 1, "maxLength": 128},
                    "host_id": {"type": "string", "minLength": 1, "maxLength": 128},
                    "number": {"type": "integer", "minimum": 0},
                    "label": {"type": "string", "maxLength": 128},
                    "focused": {"type": "boolean"},
                    "pane_count": {"type": "integer", "minimum": 0},
                    "tab_count": {"type": "integer", "minimum": 0},
                    "active_tab_id": {"type": "string", "maxLength": 128},
                    "agent_status": {"$ref": "#/$defs/agent_status"},
                },
            },
            "tab": {
                "type": "object",
                "required": ["id", "host_id", "workspace_id"],
                "additionalProperties": False,
                "properties": {
                    "id": {"type": "string", "minLength": 1, "maxLength": 128},
                    "host_id": {"type": "string", "minLength": 1, "maxLength": 128},
                    "workspace_id": {"type": "string", "minLength": 1, "maxLength": 128},
                    "number": {"type": "integer", "minimum": 0},
                    "label": {"type": "string", "maxLength": 128},
                    "focused": {"type": "boolean"},
                    "pane_count": {"type": "integer", "minimum": 0},
                    "agent_status": {"$ref": "#/$defs/agent_status"},
                },
            },
            "pane": {
                "type": "object",
                "required": ["id", "host_id", "workspace_id", "tab_id"],
                "additionalProperties": False,
                "properties": {
                    "id": {"type": "string", "minLength": 1, "maxLength": 128},
                    "host_id": {"type": "string", "minLength": 1, "maxLength": 128},
                    "workspace_id": {"type": "string", "minLength": 1, "maxLength": 128},
                    "tab_id": {"type": "string", "minLength": 1, "maxLength": 128},
                    "focused": {"type": "boolean"},
                    "agent_status": {"$ref": "#/$defs/agent_status"},
                },
            },
            "agent": {
                "type": "object",
                "required": [
                    "terminal_id",
                    "host_id",
                    "workspace_id",
                    "tab_id",
                    "pane_id",
                    "status",
                    "revision",
                ],
                "additionalProperties": False,
                "properties": {
                    "terminal_id": {"type": "string", "minLength": 1, "maxLength": 128},
                    "host_id": {"type": "string", "minLength": 1, "maxLength": 128},
                    "workspace_id": {"type": "string", "minLength": 1, "maxLength": 128},
                    "tab_id": {"type": "string", "minLength": 1, "maxLength": 128},
                    "pane_id": {"type": "string", "minLength": 1, "maxLength": 128},
                    "name": {"type": "string", "maxLength": 128},
                    "agent": {"type": "string", "maxLength": 64},
                    "title": {"type": "string", "maxLength": 256},
                    "status": {"$ref": "#/$defs/agent_status"},
                    "focused": {"type": "boolean"},
                    "launch_pending": {"type": "boolean"},
                    "interactive_ready": {"type": "boolean"},
                    "state_change_seq": {"type": "integer", "minimum": 0},
                    "revision": {"type": "integer", "minimum": 0},
                },
            },
            "agent_status": {
                "type": "string",
                "enum": ["idle", "working", "blocked", "done", "unknown"],
            },
        },
    },
    "relaydeck/gateway/v1/metadata-event": {
        "$id": "relaydeck/gateway/v1/metadata-event",
        "type": "object",
        "required": [
            "protocol",
            "event_id",
            "host_id",
            "host_epoch",
            "sequence",
            "cursor",
            "type",
            "observed_at",
        ],
        "additionalProperties": False,
        "properties": {
            "protocol": {"const": GATEWAY_PROTOCOL},
            "event_id": {"type": "string", "minLength": 1, "maxLength": 128},
            "host_id": {"type": "string", "minLength": 1, "maxLength": 128},
            "host_epoch": {"type": "string", "minLength": 1, "maxLength": 128},
            "sequence": {"type": "integer", "minimum": 1},
            "cursor": {"type": "string", "minLength": 1, "maxLength": 512},
            "type": {
                "type": "string",
                "enum": [
                    "host.status",
                    "workspace.upserted",
                    "tab.upserted",
                    "pane.upserted",
                    "agent.upserted",
                    "resource.removed",
                    "command.updated",
                    "capabilities.changed",
                    "resync.required",
                ],
            },
            "resource_id": {"type": "string", "maxLength": 128},
            "resource_revision": {"type": "integer", "minimum": 0},
            "observed_at": {"type": "string", "pattern": RFC3339_UTC},
            "payload": {"type": "object"},
        },
    },
    "relaydeck/gateway/v1/command-submit": {
        "$id": "relaydeck/gateway/v1/command-submit",
        "type": "object",
        "required": [
            "protocol",
            "command_id",
            "host_id",
            "host_epoch",
            "kind",
            "target",
            "payload",
        ],
        "additionalProperties": False,
        "properties": {
            "protocol": {"const": GATEWAY_PROTOCOL},
            "command_id": {
                "type": "string",
                "minLength": 8,
                "maxLength": 128,
                "pattern": "^[A-Za-z0-9_-]+$",
            },
            "host_id": {"type": "string", "minLength": 1, "maxLength": 128},
            "host_epoch": {"type": "string", "minLength": 1, "maxLength": 128},
            "kind": {"type": "string", "enum": list(COMMAND_KINDS)},
            "expected_revision": {"type": "integer", "minimum": 0},
            "target": {
                "type": "object",
                "minProperties": 1,
                "additionalProperties": {"type": "string"},
            },
            "payload": {"type": "object"},
            "expires_at": {"type": "string", "pattern": RFC3339_UTC},
        },
    },
    # TODO: wire gap -- v1 command-record lacks `acked`/`expired`
    # statuses + `result_digest`; relaydeck/journal.py:119-128 maps them until this grows.
    "relaydeck/gateway/v1/command-record": {
        "$id": "relaydeck/gateway/v1/command-record",
        "type": "object",
        "required": ["protocol", "command_id", "host_id", "kind", "target", "status"],
        "additionalProperties": False,
        "properties": {
            "protocol": {"const": GATEWAY_PROTOCOL},
            "command_id": {"type": "string", "minLength": 1, "maxLength": 128},
            "host_id": {"type": "string", "minLength": 1, "maxLength": 128},
            "kind": {"type": "string", "enum": list(COMMAND_KINDS)},
            "target": {
                "type": "object",
                "minProperties": 1,
                "additionalProperties": {"type": "string"},
            },
            "status": {"type": "string", "enum": list(COMMAND_STATUSES)},
            "error_code": {"type": "string", "maxLength": 128},
            "updated_ms": {"type": "integer", "minimum": 0},
            "result": RESULT_SCHEMA,
        },
    },
    "relaydeck/gateway/v1/command-dispatch": {
        "$id": "relaydeck/gateway/v1/command-dispatch",
        "type": "object",
        "required": [
            "kind",
            "command_id",
            "principal",
            "host_id",
            "host_epoch",
            "gateway_kind",
            "target",
            "payload",
            "digest",
            "generation",
            "expires_ms",
        ],
        "additionalProperties": False,
        "properties": {
            "kind": {"const": "command.dispatch"},
            "command_id": {"type": "string", "minLength": 1, "maxLength": 128},
            "principal": {"type": "string", "minLength": 1, "maxLength": 128},
            "host_id": {"type": "string", "minLength": 1, "maxLength": 128},
            "host_epoch": {"type": "string", "minLength": 1, "maxLength": 128},
            "gateway_kind": {"type": "string", "enum": list(COMMAND_KINDS)},
            "target": {
                "type": "object",
                "minProperties": 1,
                "additionalProperties": {"type": "string"},
            },
            "payload": {"type": "object"},
            "digest": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
            "generation": {"type": "integer", "minimum": 0},
            "expires_ms": {"type": "integer", "minimum": 0},
        },
    },
    "relaydeck/gateway/v1/terminal-control": {
        "$id": "relaydeck/gateway/v1/terminal-control",
        "type": "object",
        "required": ["kind"],
        "additionalProperties": False,
        "properties": {
            "kind": {
                "type": "string",
                "enum": ["ack", "gap", "reset", "bye"],
            },
            "applied": {"type": "integer", "minimum": 0},
            "detail": {"type": "string", "maxLength": 256},
            "generation": {"type": "integer", "minimum": 1, "maximum": 0xFFFFFFFF},
            "input_seq": {"type": "integer", "minimum": 1, "maximum": 0xFFFFFFFF},
        },
    },
    "relaydeck/gateway/v1/terminal-ticket-request": {
        "$id": "relaydeck/gateway/v1/terminal-ticket-request",
        "type": "object",
        "required": ["lease_id"],
        "additionalProperties": False,
        "properties": {
            "lease_id": {"type": "string", "minLength": 1, "maxLength": 128},
        },
    },
    "relaydeck/gateway/v1/terminal-ticket": {
        "$id": "relaydeck/gateway/v1/terminal-ticket",
        "type": "object",
        "required": ["ticket", "expires_in"],
        "additionalProperties": False,
        "properties": {
            "ticket": {"type": "string", "minLength": 16, "maxLength": 256},
            "expires_in": {"type": "integer", "minimum": 1, "maximum": 60},
        },
    },
    "relaydeck/gateway/v1/error": {
        "$id": "relaydeck/gateway/v1/error",
        "type": "object",
        "required": ["code", "message"],
        "additionalProperties": False,
        "properties": {
            "code": {"type": "string", "enum": list(ERROR_CODES)},
            "message": {"type": "string", "minLength": 1, "maxLength": 512},
            "retryable": {"type": "boolean"},
            "details": {"type": "object"},
        },
    },
    "relaydeck/gateway/v1/authorization-request": {
        "$id": "relaydeck/gateway/v1/authorization-request",
        "type": "object",
        "additionalProperties": False,
        "required": [
            "response_type",
            "code_challenge",
            "code_challenge_method",
            "state",
            "redirect_uri",
        ],
        "properties": {
            "response_type": {"const": "code"},
            "code_challenge_method": {"const": "S256"},
            "code_challenge": {"type": "string", "pattern": r"^[A-Za-z0-9_-]{43}$"},
            "state": {"type": "string", "pattern": r"^[A-Za-z0-9_-]{16,128}$"},
            "redirect_uri": {"type": "string", "minLength": 1, "maxLength": 2048},
        },
    },
    "relaydeck/gateway/v1/token-exchange": {
        "$id": "relaydeck/gateway/v1/token-exchange",
        "type": "object",
        "required": ["grant"],
        "additionalProperties": False,
        "properties": {
            "grant": {"type": "string", "enum": ["authorization_code", "refresh_token"]},
            "code": {"type": "string", "minLength": 8, "maxLength": 256},
            "verifier": {"type": "string", "minLength": 43, "maxLength": 128},
            "refresh_token": {"type": "string", "minLength": 16, "maxLength": 256},
        },
    },
    "relaydeck/gateway/v1/token-response": {
        "$id": "relaydeck/gateway/v1/token-response",
        "type": "object",
        "required": ["access_token", "refresh_token", "expires_in"],
        "additionalProperties": False,
        "properties": {
            "access_token": {"type": "string", "minLength": 16, "maxLength": 256},
            "refresh_token": {"type": "string", "minLength": 16, "maxLength": 256},
            "expires_in": {"type": "integer", "minimum": 60, "maximum": 86400},
        },
    },
    "relaydeck/gateway/v1/nonce-claim": {
        "$id": "relaydeck/gateway/v1/nonce-claim",
        "type": "object",
        "required": ["nonce", "host_id"],
        "additionalProperties": False,
        "properties": {
            "nonce": {"type": "string", "minLength": 16, "maxLength": 256},
            "host_id": {"type": "string", "minLength": 1, "maxLength": 128},
        },
    },
    "relaydeck/gateway/v1/enrollment-create": {
        "$id": "relaydeck/gateway/v1/enrollment-create",
        "type": "object",
        "required": ["host_label", "pubkey"],
        "additionalProperties": False,
        "properties": {
            "host_label": {"type": "string", "minLength": 1, "maxLength": 128},
            "pubkey": {
                "type": "string",
                "minLength": 56,
                "maxLength": 512,
                "pattern": "^ssh-ed25519 [A-Za-z0-9+/=]+( .*)?$",
            },
            "ttl_seconds": {"type": "integer", "minimum": 60, "maximum": 3600},
        },
    },
    "relaydeck/gateway/v1/enrollment": {
        "$id": "relaydeck/gateway/v1/enrollment",
        "type": "object",
        "required": ["id", "code", "qr", "fingerprint", "expires_at", "host_label"],
        "additionalProperties": False,
        "properties": {
            "id": {"type": "string", "minLength": 8, "maxLength": 128},
            "code": {"type": "string", "minLength": 12, "maxLength": 14},
            "qr": {"type": "string", "minLength": 32, "maxLength": 480},
            "fingerprint": {"type": "string", "minLength": 50, "maxLength": 50},
            "expires_at": {"type": "string", "pattern": RFC3339_UTC},
            "host_label": {"type": "string", "minLength": 1, "maxLength": 128},
        },
    },
    "relaydeck/gateway/v1/enrollment-status": {
        "$id": "relaydeck/gateway/v1/enrollment-status",
        "type": "object",
        "required": ["id", "status"],
        "additionalProperties": False,
        "properties": {
            "id": {"type": "string", "minLength": 8, "maxLength": 128},
            "status": {"enum": ["pending", "claimed", "expired", "burned"]},
        },
    },
    "relaydeck/gateway/v1/enrollment-redeem": {
        "$id": "relaydeck/gateway/v1/enrollment-redeem",
        "type": "object",
        "required": ["code", "host_id", "pubkey", "proof"],
        "additionalProperties": False,
        "properties": {
            "code": {"type": "string", "minLength": 12, "maxLength": 14},
            "host_id": {"type": "string", "minLength": 1, "maxLength": 128},
            "pubkey": {
                "type": "string",
                "minLength": 56,
                "maxLength": 512,
                "pattern": "^ssh-ed25519 [A-Za-z0-9+/=]+( .*)?$",
            },
            "proof": {"type": "string", "minLength": 86, "maxLength": 88},
        },
    },
    "relaydeck/gateway/v1/enrollment-result": {
        "$id": "relaydeck/gateway/v1/enrollment-result",
        "type": "object",
        "required": ["host_id", "host_token", "fingerprint"],
        "additionalProperties": False,
        "properties": {
            "host_id": {"type": "string", "minLength": 1, "maxLength": 128},
            "host_token": {"type": "string", "minLength": 16, "maxLength": 256},
            "fingerprint": {"type": "string", "minLength": 50, "maxLength": 50},
        },
    },
    "relaydeck/gateway/v1/me": {
        "$id": "relaydeck/gateway/v1/me",
        "type": "object",
        "required": ["account", "device_id", "capabilities", "hosts", "protocol_version"],
        "additionalProperties": False,
        "properties": {
            "account": {"type": "string", "minLength": 1, "maxLength": 256},
            "device_id": {"type": "string", "minLength": 1, "maxLength": 128},
            "capabilities": {"type": "array", "items": {"type": "string"}},
            "hosts": {"type": "array", "items": {"type": "string"}},
            "protocol_version": {"const": GATEWAY_PROTOCOL},
        },
    },
    "relaydeck/gateway/v1/hosts": {
        "$id": "relaydeck/gateway/v1/hosts",
        "type": "object",
        "required": ["hosts"],
        "additionalProperties": False,
        "properties": {
            "hosts": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["host_id", "epoch", "reachable", "upstream"],
                    "additionalProperties": False,
                    "properties": {
                        "host_id": {"type": "string", "minLength": 1, "maxLength": 128},
                        "label": {"type": "string", "maxLength": 128},
                        "agent_catalog": CATALOG_SCHEMA,
                        "epoch": {"type": "string", "maxLength": 128},
                        "reachable": {"type": "boolean"},
                        "last_seen_at": {"type": "string", "pattern": RFC3339_UTC},
                        "fingerprint": {"type": "string", "maxLength": 50},
                        "upstream": {
                            "type": "object",
                            "required": ["protocol", "compatible"],
                            "additionalProperties": False,
                            "properties": {
                                "protocol": {"type": "integer", "minimum": 0},
                                "version": {"type": "string", "maxLength": 64},
                                "compatible": {"type": "boolean"},
                            },
                        },
                    },
                },
            },
        },
    },
    "relaydeck/gateway/v1/agents-page": {
        "$id": "relaydeck/gateway/v1/agents-page",
        "type": "object",
        "required": ["host_id", "agents"],
        "additionalProperties": False,
        "properties": {
            "host_id": {"type": "string", "minLength": 1, "maxLength": 128},
            "host_epoch": {"type": "string", "minLength": 1, "maxLength": 128},
            "total": {"type": "integer", "minimum": 0, "maximum": 4096},
            "agents": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": [
                        "terminal_id",
                        "name",
                        "agent",
                        "title",
                        "status",
                        "focused",
                        "revision",
                    ],
                    "additionalProperties": False,
                    "properties": {
                        "terminal_id": {"type": "string", "minLength": 1, "maxLength": 128},
                        "name": {"type": "string", "maxLength": 128},
                        "agent": {"type": "string", "maxLength": 64},
                        "title": {"type": "string", "maxLength": 256},
                        "status": {"$ref": "#/$defs/agent_status"},
                        "focused": {"type": "boolean"},
                        "revision": {"type": "integer", "minimum": 0},
                        "workspace_id": {"type": "string", "maxLength": 128},
                        "tab_id": {"type": "string", "maxLength": 128},
                        "pane_id": {"type": "string", "maxLength": 128},
                        "project": {"type": "string", "maxLength": 128},
                        "last_activity_ms": {"type": "integer", "minimum": 0},
                        "activity_id": {"type": "string", "pattern": "^([a-f0-9]{64})?$"},
                        "unread": {"type": "boolean"},
                    },
                },
            },
            "next_page_token": {"type": "string", "minLength": 1, "maxLength": 512},
        },
        "$defs": {
            "agent_status": {
                "type": "string",
                "enum": ["idle", "working", "blocked", "done", "unknown"],
            },
        },
    },
    "relaydeck/gateway/v1/device-registration": {
        "$id": "relaydeck/gateway/v1/device-registration",
        "type": "object",
        "additionalProperties": False,
        "required": ["device_id", "platform", "status"],
        "properties": {
            "device_id": {"type": "string", "minLength": 1, "maxLength": 128},
            "platform": {"const": "android"},
            "status": {"const": "active"},
        },
    },
    "relaydeck/gateway/v1/deep-link-resolution": {
        "$id": "relaydeck/gateway/v1/deep-link-resolution",
        "type": "object",
        "additionalProperties": False,
        "required": [
            "notification_id",
            "event_ref",
            "host_ref",
            "session_ref",
            "category",
            "host_id",
            "session_id",
            "route",
        ],
        "properties": {
            **{
                key: {"type": "string", "minLength": 1, "maxLength": 256}
                for key in (
                    "notification_id",
                    "event_ref",
                    "host_ref",
                    "session_ref",
                    "host_id",
                    "session_id",
                )
            },
            "category": {"enum": ["attention", "completion"]},
            "route": {
                "type": "object",
                "additionalProperties": False,
                "required": ["host_id", "session_id"],
                "properties": {
                    key: {"type": "string", "minLength": 1, "maxLength": 256}
                    for key in ("host_id", "session_id")
                },
            },
        },
    },
    "relaydeck/gateway/v1/devices": {
        "$id": "relaydeck/gateway/v1/devices",
        "type": "object",
        "required": ["current_device_id", "devices", "truncated"],
        "additionalProperties": False,
        "properties": {
            "current_device_id": {"type": "string", "minLength": 1, "maxLength": 128},
            "truncated": {"type": "boolean"},
            "devices": {
                "type": "array",
                "maxItems": 200,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "device_id",
                        "current",
                        "platform",
                        "status",
                        "auth_sessions",
                        "push_status",
                    ],
                    "properties": {
                        "device_id": {"type": "string", "minLength": 1, "maxLength": 128},
                        "current": {"type": "boolean"},
                        "platform": {"type": "string", "enum": ["android", "web", "unknown"]},
                        "status": {
                            "type": "string",
                            "enum": ["active", "refresh_only", "notifications_only", "revoked"],
                        },
                        "auth_sessions": {"type": "integer", "minimum": 0},
                        "push_status": {"type": "string", "enum": ["active", "tombstoned", "none"]},
                        "access_expires_at": {"type": "string", "format": "date-time"},
                        "refresh_expires_at": {"type": "string", "format": "date-time"},
                        "registered_at": {"type": "string", "format": "date-time"},
                    },
                },
            },
        },
    },
    "relaydeck/gateway/v1/host-revoke": {
        "$id": "relaydeck/gateway/v1/host-revoke",
        "type": "object",
        "required": ["ok"],
        "additionalProperties": False,
        "properties": {"ok": {"const": True}},
    },
    "relaydeck/gateway/v1/device-revoke": {
        "$id": "relaydeck/gateway/v1/device-revoke",
        "type": "object",
        "required": ["ok"],
        "additionalProperties": False,
        "properties": {"ok": {"const": True}},
    },
    "relaydeck/gateway/v1/agent-read": {
        "$id": "relaydeck/gateway/v1/agent-read",
        "type": "object",
        "additionalProperties": False,
        "required": ["host_id", "host_epoch", "agent_id", "activity_id", "unread"],
        "properties": {
            "host_id": {"type": "string", "minLength": 1, "maxLength": 128},
            "host_epoch": {"type": "string", "minLength": 1, "maxLength": 128},
            "agent_id": {"type": "string", "minLength": 1, "maxLength": 128},
            "activity_id": {"type": "string", "pattern": "^([a-f0-9]{64})?$"},
            "unread": {"type": "boolean"},
        },
    },
}


SCHEMAS.update(PAIRING_SCHEMAS)
SCHEMAS.update(PICTURE_SCHEMAS)
SCHEMAS.update(TERMINAL_OWNERSHIP_SCHEMAS)


class SchemaError(ValueError):
    pass


def _resolve(schema: dict[str, Any], root: dict[str, Any]) -> dict[str, Any]:
    ref = schema.get("$ref")
    if not ref:
        return schema
    if not ref.startswith("#/$defs/"):
        raise SchemaError(f"unsupported $ref {ref}")
    node: Any = root["$defs"][ref[len("#/$defs/") :]]
    if not isinstance(node, dict):
        raise SchemaError(f"bad $ref target {ref}")
    return _resolve(node, root)


def _check_type(value: Any, expected: Any) -> bool:
    if isinstance(expected, list):
        return any(_check_type(value, t) for t in expected)
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    if expected == "number":
        return isinstance(value, int | float) and not isinstance(value, bool)
    raise SchemaError(f"unsupported type {expected}")


def _validate(schema: dict[str, Any], value: Any, root: dict[str, Any], path: str) -> None:
    schema = _resolve(schema, root)
    if "enum" in schema and value not in schema["enum"]:
        raise SchemaError(f"{path}: {value!r} not in enum")
    if "const" in schema and value != schema["const"]:
        raise SchemaError(f"{path}: {value!r} != const")
    if "type" in schema and not _check_type(value, schema["type"]):
        raise SchemaError(f"{path}: expected {schema['type']}")
    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            raise SchemaError(f"{path}: too short")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            raise SchemaError(f"{path}: too long")
        if "pattern" in schema and not re.search(schema["pattern"], value):
            raise SchemaError(f"{path}: pattern mismatch")
    if isinstance(value, int) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            raise SchemaError(f"{path}: below minimum")
        if "maximum" in schema and value > schema["maximum"]:
            raise SchemaError(f"{path}: above maximum")
    if isinstance(value, dict):
        if "minProperties" in schema and len(value) < schema["minProperties"]:
            raise SchemaError(f"{path}: too few properties")
        if "maxProperties" in schema and len(value) > schema["maxProperties"]:
            raise SchemaError(f"{path}: too many properties")
        for key in schema.get("required", []):
            if key not in value:
                raise SchemaError(f"{path}: missing {key}")
        props = schema.get("properties", {})
        for key, item in value.items():
            if key in props:
                _validate(props[key], item, root, f"{path}.{key}")
            else:
                extra = schema.get("additionalProperties", True)
                if extra is False:
                    raise SchemaError(f"{path}: unknown field {key}")
                if isinstance(extra, dict):
                    _validate(extra, item, root, f"{path}.{key}")
    if isinstance(value, list) and "items" in schema:
        for index, item in enumerate(value):
            _validate(schema["items"], item, root, f"{path}[{index}]")
    for keyword in ("anyOf", "oneOf"):
        if keyword in schema:
            matches = 0
            for option in schema[keyword]:
                try:
                    _validate(option, value, root, path)
                except SchemaError:
                    continue
                matches += 1
            want = 1 if keyword == "oneOf" else 1
            if matches < want or (keyword == "oneOf" and matches > 1):
                raise SchemaError(f"{path}: {keyword} mismatch")
    for option in schema.get("allOf", []):
        _validate(option, value, root, path)


def validate(schema_id: str, value: Any) -> None:
    """Validate value against a registered schema; raises SchemaError."""
    try:
        root = SCHEMAS[schema_id]
    except KeyError:
        raise SchemaError(f"unknown schema {schema_id}") from None
    _validate(root, value, root, "$")


# Compatibility rule: unknown event types cannot arrive (closed enum), unknown payload keys are
# ignored, and absent load-bearing keys reject the event.
_EVENT_RESOURCE_REQUIRED = (
    "workspace.upserted",
    "tab.upserted",
    "pane.upserted",
    "agent.upserted",
    "resource.removed",
    "command.updated",
)


def validate_metadata_event(doc: dict[str, Any]) -> None:
    """Envelope validation plus per-type load-bearing rules."""
    validate("relaydeck/gateway/v1/metadata-event", doc)
    kind = doc["type"]
    if kind in _EVENT_RESOURCE_REQUIRED and not doc.get("resource_id"):
        raise SchemaError(f"$.resource_id: required for {kind}")
    payload = doc.get("payload", {})
    if kind == "command.updated":
        if payload.get("status") not in COMMAND_STATUSES:
            raise SchemaError("$.payload.status: required command status")
        target = payload.get("target", {})
        if not isinstance(target, dict) or not target:
            raise SchemaError("$.payload.target: required target object")
    elif kind == "capabilities.changed":
        if not isinstance(payload.get("capabilities"), list):
            raise SchemaError("$.payload.capabilities: required list")
