"""Connector enrollment keys and proofs.

Each home connector provisions its own Ed25519 identity. The private
seed never leaves the home host (0600 file); the gateway binds the
invitation to the OpenSSH public key and verifies a proof-of-possession
signature at redeem time. Fingerprints use the OpenSSH SHA256 form so
owners can cross-check with `ssh-keygen -lf`.

QR payloads carry a bounded enrollment reference plus public context
only: gateway origin, invitation id, claim code, expiry. Reusable
credentials never enter the QR.
"""

from __future__ import annotations

import base64
import json
import secrets
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from relaydeck.contracts.pubkeys import (
    _ssh_blob as _ssh_blob,
)
from relaydeck.contracts.pubkeys import (
    fingerprint_of_raw as fingerprint_of_raw,
)

# compat: remove in Q4-cleanup after remaining server/test importers migrate.
from relaydeck.contracts.pubkeys import (
    openssh_to_raw as openssh_to_raw,
)
from relaydeck.contracts.pubkeys import (
    raw_to_openssh as raw_to_openssh,
)

PROOF_DOMAIN = b"relaydeck-enrollment-v1"
QR_PREFIX = "RELAYDECK-ENROLL"
QR_VERSION = 1
MAX_QR_LEN = 480

_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
CODE_LEN = 12


def generate() -> tuple[bytes, str]:
    """Fresh Ed25519 identity: (32-byte seed, OpenSSH public line)."""
    private = Ed25519PrivateKey.generate()
    seed = private.private_bytes_raw()
    public = private.public_key().public_bytes_raw()
    blob = _ssh_blob(public)
    line = f"ssh-ed25519 {base64.b64encode(blob).decode()}"
    return seed, line


def fingerprint_of_openssh(line: str) -> str:
    """OpenSSH SHA256 fingerprint (`SHA256:...`, tool-compatible)."""
    return fingerprint_of_raw(openssh_to_raw(line))


def mint_code() -> str:
    return "".join(secrets.choice(_CODE_ALPHABET) for _ in range(CODE_LEN))


def proof_message(enrollment_id: str, host_id: str, raw_pubkey: bytes) -> bytes:
    for field in (enrollment_id, host_id):
        if not field or "\x00" in field or len(field) > 128:
            raise ValueError("bad proof field")
    if len(raw_pubkey) != 32:
        raise ValueError("bad proof key")
    return (
        PROOF_DOMAIN
        + b"\x00"
        + enrollment_id.encode()
        + b"\x00"
        + host_id.encode()
        + b"\x00"
        + raw_pubkey
    )


def sign_proof(seed: bytes, enrollment_id: str, host_id: str) -> bytes:
    if len(seed) != 32:
        raise ValueError("ed25519 seed is 32 bytes")
    private = Ed25519PrivateKey.from_private_bytes(seed)
    raw = private.public_key().public_bytes_raw()
    return private.sign(proof_message(enrollment_id, host_id, raw))


def verify_proof(openssh_pubkey: str, enrollment_id: str, host_id: str, signature: bytes) -> bool:
    try:
        raw = openssh_to_raw(openssh_pubkey)
        Ed25519PublicKey.from_public_bytes(raw).verify(
            signature, proof_message(enrollment_id, host_id, raw)
        )
    except (ValueError, InvalidSignature):
        return False
    return True


def build_qr_payload(gateway_origin: str, enrollment_id: str, code: str, expires_at: str) -> str:
    """Bounded public enrollment reference (no reusable secrets)."""
    doc = {
        "gw": gateway_origin,
        "id": enrollment_id,
        "code": code,
        "exp": expires_at,
        "v": QR_VERSION,
    }
    body = (
        base64.urlsafe_b64encode(json.dumps(doc, separators=(",", ":")).encode())
        .decode()
        .rstrip("=")
    )
    payload = f"{QR_PREFIX}:{QR_VERSION}:{body}"
    if len(payload) > MAX_QR_LEN:
        raise ValueError("qr payload too large")
    return payload


def parse_qr_payload(payload: str) -> dict[str, Any]:
    """Parse and validate a scanned enrollment reference."""
    prefix, sep, rest = payload.partition(":")
    if prefix != QR_PREFIX or not sep:
        raise ValueError("bad qr prefix")
    version, sep, body = rest.partition(":")
    if not sep or version != str(QR_VERSION):
        raise ValueError("unsupported qr version")
    try:
        padded = body + "=" * (-len(body) % 4)
        doc = json.loads(base64.urlsafe_b64decode(padded))
    except ValueError as exc:
        raise ValueError("bad qr encoding") from exc
    if not isinstance(doc, dict) or set(doc) != {"gw", "id", "code", "exp", "v"}:
        raise ValueError("bad qr shape")
    if doc.get("v") != QR_VERSION:
        raise ValueError("bad qr version field")
    origin = str(doc.get("gw", ""))
    if origin and not _origin_ok(origin):
        raise ValueError("bad qr origin")
    if not str(doc.get("id", "")) or not str(doc.get("code", "")):
        raise ValueError("bad qr reference")
    return {k: str(v) for k, v in doc.items()}


def _origin_ok(origin: str) -> bool:
    from urllib.parse import urlparse

    try:
        parsed = urlparse(origin)
    except ValueError:
        return False
    if parsed.scheme == "https" and parsed.hostname:
        return True
    if parsed.scheme == "http" and (parsed.hostname or "").lower() in (
        "localhost",
        "127.0.0.1",
        "::1",
    ):
        return True
    return False
