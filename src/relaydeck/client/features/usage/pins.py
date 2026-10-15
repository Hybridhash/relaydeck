"""Pinned spaces for the Space picker.

Pins are per device and per account and host. The storage key is an opaque digest of the account
identity and the host id, so the key carries no personal data. Pure functions do the bounded
bookkeeping; the async adapter reads and writes one JSON list through client storage and never
raises, so the page still works without storage (the pins then live in memory for the session).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

MAX_PINS = 50  # a device never keeps more than this many pins
NOT_LINKED_ID = "not_linked"  # synthetic; Not linked is never pinnable
KEY_PREFIX = "relaydeck.usage.pins.v1."


def pin_key(identity: object, host_id: str) -> str:
    """The storage key for one account on one host: `KEY_PREFIX` plus a 24-hex-digit digest of
    both."""
    digest = hashlib.sha256(f"{identity}\x1f{host_id}".encode()).hexdigest()
    return f"{KEY_PREFIX}{digest[:24]}"


def clean(raw: Any) -> tuple[str, ...]:
    """Stored pins, tolerantly: only non-empty strings, no duplicates, no Not linked, bounded."""
    if not isinstance(raw, list):
        return ()
    out: list[str] = []
    for item in raw:
        if isinstance(item, str) and item and item != NOT_LINKED_ID and item not in out:
            out.append(item)
    return tuple(out[:MAX_PINS])


def toggle(pins: Iterable[str], space_id: str) -> tuple[str, ...]:
    """Pin or unpin one space. Pinning a space when the bound is reached changes nothing."""
    current = list(clean(list(pins)))
    if space_id == NOT_LINKED_ID or not space_id:
        return tuple(current)
    if space_id in current:
        current.remove(space_id)
    elif len(current) < MAX_PINS:
        current.append(space_id)
    return tuple(current)


def encode(pins: Iterable[str]) -> str:
    return json.dumps(list(clean(list(pins))))


async def load(get: Callable[[str], Awaitable[Any]], key: str) -> tuple[str, ...]:
    """The stored pins for `key`. Missing, corrupt or unavailable storage: no pins."""
    try:
        stored = await get(key)
    except Exception:  # noqa: BLE001 - storage unavailable: no pins
        return ()
    if not isinstance(stored, str):
        return ()
    try:
        return clean(json.loads(stored))
    except ValueError:
        return ()


async def save(set_: Callable[[str, str], Awaitable[Any]], key: str, pins: Iterable[str]) -> bool:
    """Write the pins. Returns False when storage is unavailable; the caller keeps them in
    memory."""
    try:
        await set_(key, encode(pins))
    except Exception:  # noqa: BLE001 - storage unavailable: keep in memory
        return False
    return True


__all__ = [
    "KEY_PREFIX",
    "MAX_PINS",
    "NOT_LINKED_ID",
    "clean",
    "encode",
    "load",
    "pin_key",
    "save",
    "toggle",
]
