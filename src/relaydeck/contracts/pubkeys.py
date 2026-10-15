"""Pure OpenSSH Ed25519 public-key parsing and fingerprints."""

from __future__ import annotations

import base64
import hashlib


def openssh_to_raw(line: str) -> bytes:
    """Parse an `ssh-ed25519 ...` line to 32 raw public bytes."""
    parts = line.strip().split()
    if len(parts) < 2 or parts[0] != "ssh-ed25519":
        raise ValueError("not an ssh-ed25519 line")
    try:
        blob = base64.b64decode(parts[1], validate=True)
    except ValueError as exc:
        raise ValueError("bad openssh encoding") from exc
    if len(blob) != 51 or blob[:19] != b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00 ":
        raise ValueError("bad openssh key blob")
    return blob[19:]


def raw_to_openssh(raw: bytes) -> str:
    if len(raw) != 32:
        raise ValueError("ed25519 public key is 32 bytes")
    return f"ssh-ed25519 {base64.b64encode(_ssh_blob(raw)).decode()}"


def _ssh_blob(raw: bytes) -> bytes:
    return b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00 " + raw


def fingerprint_of_raw(raw: bytes) -> str:
    digest = hashlib.sha256(_ssh_blob(raw)).digest()
    return "SHA256:" + base64.b64encode(digest).decode().rstrip("=")
