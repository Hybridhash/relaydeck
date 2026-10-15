"""Keyed working-folder digests for inferred session-to-space linking.

One module, used identically by log readers and the connector: readers digest a
session's working folder from its own log records, the connector digests pane
cwd values in memory, and the store joins them by digest equality. Raw paths
are never stored, logged or put on the wire: only the digest leaves this module.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import unicodedata
from pathlib import Path

LINK_KEY_NAME = "usage-link.key"
CWD_DOMAIN = "cwd-v1:"
REPO_DOMAIN = "repokey-v1:"
DIGEST_CHARS = 32
MAX_PATH_CHARS = 4096


def load_or_create_link_key(state_dir: Path) -> bytes | None:
    """Read the per-install link key, creating it (32 random bytes, mode 0600,
    via atomic exclusive create) when missing. Any failure returns None: the
    caller then emits no digests at all, never unkeyed hashes."""
    path = state_dir / LINK_KEY_NAME
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        pass
    except OSError:
        return None
    else:
        return data if data else None
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        try:
            data = path.read_bytes()
        except OSError:
            return None
        return data if data else None
    except OSError:
        return None
    try:
        os.write(fd, secrets.token_bytes(32))
    except OSError:
        try:
            os.close(fd)
        except OSError:
            pass
        return None
    try:
        os.close(fd)
    except OSError:
        return None
    try:
        data = path.read_bytes()
    except OSError:
        return None
    return data if data else None


def normalize_cwd(value: object) -> str | None:
    """Versioned working-folder normalization, identical on both sides: NFC,
    non-strict realpath (symlink spelling only, the target is never opened),
    no trailing slash. None when the value is not a usable path."""
    if not isinstance(value, str) or not value or len(value) > MAX_PATH_CHARS:
        return None
    try:
        resolved = os.path.realpath(value)
    except (OSError, ValueError):
        return None
    if len(resolved) > 1:
        resolved = resolved.rstrip("/") or "/"
    text = unicodedata.normalize("NFC", resolved)
    if not text or len(text) > MAX_PATH_CHARS:
        return None
    return text


def _hmac_hex(key: bytes, domain: str, text: str) -> str:
    return hmac.new(key, domain.encode() + text.encode(), hashlib.sha256).hexdigest()


def cwd_digest(key: bytes | None, value: object) -> str | None:
    """Keyed digest of one working folder, or None when there is no key or no
    usable path. A missing key yields no digests, never an unkeyed hash."""
    if not key:
        return None
    normalized = normalize_cwd(value)
    if normalized is None:
        return None
    return _hmac_hex(key, CWD_DOMAIN, normalized)[:DIGEST_CHARS]


def repo_fingerprint(key: bytes | None, value: object) -> str | None:
    """Keyed fingerprint of a workspace repo key (itself path-shaped, so never
    stored raw), or None when there is no key or no usable value."""
    if not key or not isinstance(value, str) or not value or len(value) > MAX_PATH_CHARS:
        return None
    return _hmac_hex(key, REPO_DOMAIN, value)[:DIGEST_CHARS]
