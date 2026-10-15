"""Opaque, tamper-evident stream cursors.

Cursor = base64url(json).base64url(HMAC-SHA256(secret, json)). Hosts and
sequences inside are authenticated; clients treat cursors as opaque.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from typing import Any

from relaydeck.contracts.canonical import canonical_json

# compat: remove in Q4-cleanup after remaining server/test importers migrate.
from relaydeck.contracts.cursors import (
    AFTER_SEQUENCE_HEADER as AFTER_SEQUENCE_HEADER,
)
from relaydeck.contracts.cursors import (
    MAX_CURSOR_LEN as MAX_CURSOR_LEN,
)

# Authenticated SSE response: the server-verified replay boundary for the
# requested opaque cursor, never an upstream Herdr field.


class CursorError(ValueError):
    pass


def _b64e(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _b64d(data: str) -> bytes:
    pad = "=" * (-len(data) % 4)
    try:
        return base64.urlsafe_b64decode(data + pad)
    except Exception as exc:
        raise CursorError("bad cursor encoding") from exc


def encode_cursor(secret: bytes, payload: dict[str, Any]) -> str:
    if not secret:
        raise CursorError("missing cursor secret")
    body = canonical_json(payload).encode()
    sig = hmac.new(secret, body, hashlib.sha256).digest()
    cursor = f"{_b64e(body)}.{_b64e(sig)}"
    if len(cursor) > MAX_CURSOR_LEN:
        raise CursorError("cursor too long")
    return cursor


def decode_cursor(secret: bytes, cursor: str) -> dict[str, Any]:
    if not secret or not cursor or len(cursor) > MAX_CURSOR_LEN:
        raise CursorError("bad cursor")
    try:
        body_b64, sig_b64 = cursor.split(".", 1)
    except ValueError:
        raise CursorError("bad cursor shape") from None
    body = _b64d(body_b64)
    try:
        sig = _b64d(sig_b64)
    except CursorError:
        raise CursorError("bad cursor signature") from None
    expect = hmac.new(secret, body, hashlib.sha256).digest()
    if not hmac.compare_digest(expect, sig):
        raise CursorError("bad cursor signature")
    try:
        payload = json.loads(body)
    except ValueError as exc:
        raise CursorError("bad cursor body") from exc
    if not isinstance(payload, dict):
        raise CursorError("bad cursor body")
    return payload
