"""App-link OAuth callback parsing.

Accepts the custom-scheme callback and the verified https app link.
State comparison is constant-time; any mismatch fails closed without
touching stored tokens.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlparse


@dataclass(frozen=True)
class ParsedCallback:
    code: str | None
    state: str
    error: str | None
    error_description: str


class CallbackError(ValueError):
    pass


def parse_callback(uri: str, *, https_hosts: frozenset[str]) -> ParsedCallback:
    try:
        parsed = urlparse(uri)
    except ValueError as exc:
        raise CallbackError("malformed callback") from exc
    scheme = parsed.scheme.lower()
    if scheme == "dev.relaydeck.app":
        if parsed.netloc.lower() != "oauth":
            raise CallbackError("bad callback host")
    elif scheme == "https":
        if (parsed.hostname or "").lower() not in https_hosts:
            raise CallbackError("untrusted callback host")
        if not parsed.path.startswith("/app/oauth"):
            raise CallbackError("bad callback path")
    else:
        raise CallbackError("bad callback scheme")
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    state = query.get("state", "")
    if not state:
        raise CallbackError("missing state")
    error = query.get("error")
    if error:
        return ParsedCallback(
            code=None,
            state=state,
            error=error,
            error_description=query.get("error_description", "")[:256],
        )
    code = query.get("code", "")
    if not code:
        raise CallbackError("missing code")
    return ParsedCallback(code=code, state=state, error=None, error_description="")


def state_matches(expected: str, presented: str) -> bool:
    if not expected or not presented:
        return False
    return secrets.compare_digest(expected, presented)
