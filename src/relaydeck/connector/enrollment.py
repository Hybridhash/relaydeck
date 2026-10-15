"""Connector enrollment keys and credentials.

The connector provisions its own Ed25519 identity (`keygen`) and prints
its OpenSSH fingerprint for the owner to verify. The owner binds an
invitation to that public key; the connector redeems by proving
possession of the seed. The seed never leaves the home host (0600).
"""

from __future__ import annotations

import base64
import json
import os
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from relaydeck.connector.pubkey import (
    fingerprint_of_openssh,
    generate,
    parse_qr_payload,
    sign_proof,
)

DEFAULT_CREDS = "~/.config/relaydeck/connector.json"
DEFAULT_KEY = "~/.config/relaydeck/connector_key.json"


class EnrollmentError(RuntimeError):
    pass


@dataclass(frozen=True)
class ConnectorCreds:
    gateway_url: str
    host_id: str
    host_token: str


@dataclass(frozen=True)
class ConnectorKeys:
    seed_b64: str
    public_openssh: str

    @property
    def fingerprint(self) -> str:
        return fingerprint_of_openssh(self.public_openssh)


def creds_path(path: str = DEFAULT_CREDS) -> Path:
    return Path(os.path.expanduser(path))


def key_path(path: str = DEFAULT_KEY) -> Path:
    return Path(os.path.expanduser(path))


def _write_private(target: Path, body: bytes) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
    try:
        try:
            # os.write may write fewer bytes than asked; a credentials file must never be
            # truncated, and it must be on disk before it replaces the previous one.
            view = memoryview(body)
            while view:
                view = view[os.write(fd, view) :]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
        os.replace(tmp, target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return target


def save_creds(creds: ConnectorCreds, path: str = DEFAULT_CREDS) -> Path:
    return _write_private(
        creds_path(path),
        json.dumps(
            {
                "gateway_url": creds.gateway_url,
                "host_id": creds.host_id,
                "host_token": creds.host_token,
            }
        ).encode(),
    )


def load_creds(path: str = DEFAULT_CREDS) -> ConnectorCreds | None:
    target = creds_path(path)
    if not target.exists():
        return None
    try:
        doc = json.loads(target.read_text())
    except ValueError as exc:
        raise EnrollmentError(f"credentials corrupt: {target}") from exc
    try:
        return ConnectorCreds(
            gateway_url=str(doc["gateway_url"]),
            host_id=str(doc["host_id"]),
            host_token=str(doc["host_token"]),
        )
    except KeyError as exc:
        raise EnrollmentError(f"credentials incomplete: {target}") from exc


def wipe_creds(path: str = DEFAULT_CREDS) -> bool:
    target = creds_path(path)
    if not target.exists():
        return False
    target.unlink()
    return True


def generate_keys() -> ConnectorKeys:
    seed, public = generate()
    return ConnectorKeys(base64.b64encode(seed).decode(), public)


def save_keys(keys: ConnectorKeys, path: str = DEFAULT_KEY) -> Path:
    return _write_private(
        key_path(path),
        json.dumps({"seed_b64": keys.seed_b64, "public_openssh": keys.public_openssh}).encode(),
    )


def load_keys(path: str = DEFAULT_KEY) -> ConnectorKeys | None:
    target = key_path(path)
    if not target.exists():
        return None
    try:
        doc = json.loads(target.read_text())
        keys = ConnectorKeys(
            seed_b64=str(doc["seed_b64"]), public_openssh=str(doc["public_openssh"])
        )
    except (ValueError, KeyError) as exc:
        raise EnrollmentError(f"keys corrupt: {target}") from exc
    try:
        seed = base64.b64decode(keys.seed_b64, validate=True)
        _ = keys.fingerprint
    except ValueError as exc:
        raise EnrollmentError(f"keys invalid: {target}") from exc
    if len(seed) != 32:
        raise EnrollmentError(f"keys invalid: {target}")
    return keys


def parse_invitation(payload: str) -> tuple[str, str, str, str]:
    """Split a scanned QR reference into (origin, id, code, expiry)."""
    try:
        doc = parse_qr_payload(payload)
    except ValueError as exc:
        raise EnrollmentError(f"bad invitation: {exc}") from exc
    return doc["gw"], doc["id"], doc["code"], doc["exp"]


async def claim_enrollment(
    post_json: Callable[[str, dict[str, Any]], Any],
    gateway_url: str,
    host_id: str,
    invitation_id: str,
    code: str,
    keys: ConnectorKeys,
) -> tuple[str, str]:
    """Redeem an invitation with a proof of possession.

    Returns (host_token, fingerprint). The echoed fingerprint must match
    the local key or enrollment aborts (mutual verification).
    """
    if not host_id or not invitation_id or not code:
        raise EnrollmentError("host id, invitation id, and code are required")
    try:
        seed = base64.b64decode(keys.seed_b64, validate=True)
    except ValueError as exc:
        raise EnrollmentError("keys invalid") from exc
    signature = sign_proof(seed, invitation_id, host_id)
    status, payload = await post_json(
        f"{gateway_url.rstrip('/')}/v1/enrollments/{invitation_id}/redeem",
        {
            "code": code,
            "host_id": host_id,
            "pubkey": keys.public_openssh,
            "proof": base64.urlsafe_b64encode(signature).decode().rstrip("="),
        },
    )
    if status != 200 or not isinstance(payload, dict):
        detail = (
            payload.get("message", "enroll failed")
            if isinstance(payload, dict)
            else "enroll failed"
        )
        raise EnrollmentError(str(detail))
    token = payload.get("host_token", "")
    echoed = payload.get("fingerprint", "")
    if not token:
        raise EnrollmentError("gateway issued no token")
    if echoed != keys.fingerprint:
        raise EnrollmentError("gateway echoed a different fingerprint")
    return str(token), str(echoed)
