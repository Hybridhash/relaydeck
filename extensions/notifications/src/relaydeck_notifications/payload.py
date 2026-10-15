"""Push payload guard.

Only generic attention payloads are accepted: notification id, opaque
device/host/session/event references, category, timestamps. Any
transcript text, repository path, command, secret, or approval decision
fails closed. The key set includes device_ref/host_ref/session_ref; the
refs stay optional here so older forwarders (Dart sends the 3-key
subset) keep validating; the server always emits the full set and the
lock-screen audit test asserts it exactly.
"""

from __future__ import annotations

from dataclasses import dataclass

ALLOWED_KEYS = frozenset(
    {
        "notification_id",
        "device_ref",
        "host_ref",
        "session_ref",
        "event_ref",
        "category",
        "created_at",
        "expires_at",
    }
)

REQUIRED_KEYS = frozenset({"notification_id", "event_ref", "category"})

ALLOWED_CATEGORIES = frozenset({"attention", "completion"})

MAX_VALUE_LEN = 512


@dataclass(frozen=True)
class PushPayload:
    notification_id: str
    event_ref: str
    category: str
    created_at: str = ""
    expires_at: str = ""
    device_ref: str = ""
    host_ref: str = ""
    session_ref: str = ""


def parse_push_payload(data: dict[str, str]) -> PushPayload:
    unknown = set(data) - ALLOWED_KEYS
    if unknown:
        raise ValueError(f"push payload has unknown keys: {sorted(unknown)}")
    for key, value in data.items():
        if len(value) > MAX_VALUE_LEN:
            raise ValueError(f"push payload value too long for {key}")
        if any(ord(c) < 0x20 and c not in ("\t",) for c in value):
            raise ValueError(f"push payload value has control chars for {key}")
    for required in sorted(REQUIRED_KEYS):
        if not data.get(required):
            raise ValueError(f"push payload missing {required}")
    if data["category"] not in ALLOWED_CATEGORIES:
        raise ValueError(f"push payload bad category {data['category']!r}")
    return PushPayload(
        notification_id=data["notification_id"],
        event_ref=data["event_ref"],
        category=data["category"],
        created_at=data.get("created_at", ""),
        expires_at=data.get("expires_at", ""),
        device_ref=data.get("device_ref", ""),
        host_ref=data.get("host_ref", ""),
        session_ref=data.get("session_ref", ""),
    )
