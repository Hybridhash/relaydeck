"""Explicit client origins and same-Mac transport endpoints."""

from __future__ import annotations

import re
from urllib.parse import urlparse, urlsplit


def origin(value: str, *, loopback: bool = False, tailscale: bool = False) -> str:
    """Accept an origin, never credentials, a route, or a TLS bypass."""
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError as exc:
        raise ValueError("invalid endpoint") from exc
    if (
        not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.path not in ("", "/")
        or parts.query
        or parts.fragment
        or any(c.isspace() for c in value)
        or (port is not None and not 1 <= port <= 65535)
    ):
        raise ValueError("endpoint must be an origin without credentials or a path")
    if loopback:
        if parts.scheme != "http" or parts.hostname not in ("127.0.0.1", "::1"):
            raise ValueError("local endpoint must be explicit HTTP on 127.0.0.1 or ::1")
    elif parts.scheme != "https":
        raise ValueError("remote origin requires validated HTTPS")
    if tailscale and (
        not parts.hostname.endswith(".ts.net")
        or len(parts.hostname.split(".")) < 4
        or not re.fullmatch(r"[a-z0-9-]+(?:\.[a-z0-9-]+)+", parts.hostname)
    ):
        raise ValueError("private profile requires the Mac's Tailscale HTTPS DNS origin")
    host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
    suffix = f":{port}" if port is not None else ""
    return f"{parts.scheme}://{host}{suffix}"


def is_trusted_endpoint(url: str, plaintext_hosts: frozenset[str]) -> bool:
    """TLS by default. Plaintext only for loopback or explicitly allowed
    hosts (owner-authorized, development-only;)."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme in ("https", "wss"):
        return True
    if parsed.scheme not in ("http", "ws"):
        return False
    host = (parsed.hostname or "").lower()
    if host in ("localhost", "127.0.0.1", "::1"):
        return True
    return host in plaintext_hosts
