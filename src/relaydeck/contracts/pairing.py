"""RelayDeck device pairing v1; distinct from connector enrollment and OIDC."""

from typing import Any

DEVICE_CAPABILITIES = (
    "sessions.read",
    "terminal.view",
    "terminal.control",
    "agents.prompt",
    "agents.start",
    "shells.create",
    "terminal.attach",
    "attachments.upload",
)
MAX_BODY_BYTES = 2048
MAX_WINDOW_S = 300
REQUEST_TTL_MS = 300_000
MAX_REQUESTS = 20
POLL_INTERVAL_MS = 2000
MAX_POLLS = 150  # REQUEST_TTL_MS / POLL_INTERVAL_MS: polling lasts as long as the request

CONTEXT = {
    "origin": {"type": "string", "minLength": 8, "maxLength": 256},
    "host_id": {"type": "string", "minLength": 1, "maxLength": 128},
}
PROOF = {
    **CONTEXT,
    "verifier": {"type": "string", "pattern": r"^[A-Za-z0-9._~-]{43,128}$"},
}


def document(name: str, properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "$id": f"relaydeck/gateway/v1/{name}",
        "type": "object",
        "additionalProperties": False,
        "required": list(properties),
        "properties": properties,
    }


PAIRING_SCHEMAS = {
    "relaydeck/gateway/v1/pairing-request": document(
        "pairing-request",
        {
            **CONTEXT,
            "code_challenge": {"type": "string", "pattern": r"^[A-Za-z0-9_-]{43}$"},
            "code_challenge_method": {"const": "S256"},
            "device_label": {
                "type": "string",
                "pattern": r"^[^\x00-\x1f\x7f-\x9f]{1,80}$",
            },
        },
    ),
    "relaydeck/gateway/v1/pairing-proof": document("pairing-proof", PROOF),
    "relaydeck/gateway/v1/pairing-status": document(
        "pairing-status",
        {
            **CONTEXT,
            "request_id": {"type": "string", "pattern": r"^rdp_[A-Za-z0-9_-]{32}$"},
            "matching_code": {"type": "string", "pattern": r"^\d{3}-\d{3}$"},
            "device_id": {"type": "string", "pattern": r"^device_[A-Za-z0-9_-]{32}$"},
            "host_fingerprint": {"type": "string", "minLength": 1, "maxLength": 64},
            "capabilities": {
                "type": "array",
                "minItems": len(DEVICE_CAPABILITIES),
                "maxItems": len(DEVICE_CAPABILITIES),
                "items": {"enum": list(DEVICE_CAPABILITIES)},
            },
            "state": {"enum": ["pending", "approved", "denied"]},
            "expires_at": {"type": "string", "pattern": r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$"},
            "poll_interval_ms": {"const": POLL_INTERVAL_MS},
        },
    ),
}
