"""Generic JSON-RPC stdio rate-limits provider.

Spawns the configured argv, runs the `initialize` handshake plus the
`initialized` notification, then calls `account/rateLimits/read` exactly once
and turns `rateLimitsByLimitId` (falling back to `rateLimits`) into one
snapshot per limit id. The child is always killed and reaped. `account_ref`
is a keyed digest of `accountId`, never the raw value; without a key there is
no digest and therefore no last-good reuse.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

from relaydeck.usage.contracts import (
    LimitError,
    LimitSnapshot,
    LimitStatus,
    LimitWindow,
    UsageUnavailable,
)
from relaydeck.usage.limits_plane.contracts import FetchContext, ProviderResult
from relaydeck.usage.limits_plane.scheduler import (
    ExchangeStep,
    OutputCapExceeded,
    run_line_session,
)

FORMAT_ID = "jsonrpc-rate-limits-stdio-v1"

_AUTH_TOKENS = frozenset(
    {
        "auth",
        "authenticate",
        "authentication",
        "oauth",
        "unauthorized",
        "unauthenticated",
        "forbidden",
        "login",
        "credential",
        "credentials",
    }
)
_AUTH_CODES = frozenset({401, 403, "401", "403"})
_TOKEN_RE = re.compile(r"[a-z0-9]+")


def package_version() -> str:
    """Installed relaydeck version for the handshake clientInfo (never blank)."""
    try:
        found = version("relaydeck")
    except PackageNotFoundError:
        return "0.0.0"
    return found if found else "0.0.0"


def _match_id(want: int) -> Callable[[bytes], bool]:
    def match(line: bytes) -> bool:
        try:
            obj = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return False
        return (
            isinstance(obj, dict) and obj.get("id") == want and ("result" in obj or "error" in obj)
        )

    return match


def _response_object(line: bytes) -> dict[str, Any] | None:
    try:
        obj = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None


def _classify_error(error: Any) -> LimitError:
    if isinstance(error, dict):
        code, message = error.get("code"), error.get("message")
    else:
        code, message = None, error
    if isinstance(code, int | str) and not isinstance(code, bool) and code in _AUTH_CODES:
        return LimitError.AUTH_REQUIRED
    words = set(_TOKEN_RE.findall(str(message or "").lower()))
    # "token" alone is deliberately absent: quota messages mention tokens
    # without meaning auth failure.
    if words & _AUTH_TOKENS:
        return LimitError.AUTH_REQUIRED
    return LimitError.UNAVAILABLE


def _window(name: str, raw: Any) -> LimitWindow | None:
    if not isinstance(raw, dict):
        return None
    used = raw.get("usedPercent")
    if (
        isinstance(used, bool)
        or not isinstance(used, int | float)
        or not math.isfinite(used)
        or not 0 <= used <= 100
    ):
        return None
    minutes = raw.get("windowDurationMins")
    if minutes is None:
        mins: int | None = None
    elif isinstance(minutes, bool) or not isinstance(minutes, int) or minutes <= 0:
        return None
    else:
        mins = minutes
    resets = raw.get("resetsAt")
    if resets is None:
        reset_ms: int | None = None
    elif (
        isinstance(resets, bool)
        or not isinstance(resets, int | float)
        or not math.isfinite(resets)
        or resets < 0
    ):
        return None
    else:
        reset_ms = int(resets * 1000)  # server seconds -> ms
    return LimitWindow(name, used, mins, reset_ms, LimitStatus.FRESH)


def _load_or_create_key(path: Path) -> bytes:
    """Read the per-install key, creating it (32 random bytes, mode 0600, via
    atomic exclusive create) when missing. Any failure raises OSError."""
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        pass
    else:
        if not data:
            raise OSError("empty limits key") from None
        return data
    token = secrets.token_bytes(32)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        data = path.read_bytes()
        if not data:
            raise OSError("empty limits key") from None
        return data
    try:
        os.write(fd, token)
    finally:
        os.close(fd)
    return token


def account_ref_for(key_path: Path | None, account_id: Any) -> str | None:
    """First 16 hex chars of HMAC(key, "format:accountId"), or None when the
    account is unknown or the key is missing and cannot be created. The raw
    id is never logged, stored, or returned."""
    if key_path is None or not isinstance(account_id, str) or not account_id:
        return None
    try:
        key = _load_or_create_key(key_path)
    except OSError:
        return None
    digest = hmac.new(key, f"{FORMAT_ID}:{account_id}".encode(), hashlib.sha256).hexdigest()
    return digest[:16]


def _snapshots(ctx: FetchContext, result: dict[str, Any]) -> tuple[LimitSnapshot, ...] | None:
    by_id = result.get("rateLimitsByLimitId")
    if isinstance(by_id, dict):
        buckets: Any = by_id
    else:
        legacy = result.get("rateLimits")
        if not isinstance(legacy, dict):
            return None
        buckets = legacy
    account_ref = account_ref_for(ctx.key_path, result.get("accountId"))
    snapshots: list[LimitSnapshot] = []
    for limit_id, bucket in buckets.items():
        if not isinstance(limit_id, str) or not limit_id or not isinstance(bucket, dict):
            return None
        windows: list[LimitWindow] = []
        for name in ("primary", "secondary"):
            raw = bucket.get(name)
            if raw is None:
                continue  # absent window, never a 0 % invention
            pane = _window(name, raw)
            if pane is None:
                return None
            windows.append(pane)
        # planType is display text only: intentionally not stored.
        snapshots.append(
            LimitSnapshot(
                ctx.kind,
                limit_id,
                ctx.now_ms,
                tuple(windows),
                LimitStatus.FRESH,
                FORMAT_ID,
                account_ref,
                None,
            )
        )
    return tuple(snapshots)


@dataclass
class JsonRpcRateLimitsProvider:
    """Stateless singleton: argv and key path arrive per fetch in the context."""

    format_id: str = FORMAT_ID

    def fetch(self, ctx: FetchContext) -> ProviderResult:
        if not ctx.command:
            return ProviderResult((), LimitError.UNAVAILABLE)
        init = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"clientInfo": {"name": "relaydeck", "version": package_version()}},
            }
        ).encode("utf-8")
        initialized = json.dumps({"jsonrpc": "2.0", "method": "initialized"}).encode("utf-8")
        read = json.dumps({"jsonrpc": "2.0", "id": 2, "method": "account/rateLimits/read"}).encode(
            "utf-8"
        )
        try:
            responses = run_line_session(
                ctx.command,
                (
                    ExchangeStep(init, _match_id(1)),
                    ExchangeStep(initialized, None),
                    ExchangeStep(read, _match_id(2)),
                ),
                timeout_s=ctx.timeout_s,
            )
        except TimeoutError:
            return ProviderResult((), LimitError.TIMEOUT)
        except OutputCapExceeded:
            return ProviderResult((), LimitError.PARSE)
        except (EOFError, OSError, ValueError, UsageUnavailable):
            return ProviderResult((), LimitError.UNAVAILABLE)
        init_line, _, read_line = responses
        if init_line is None or read_line is None:
            return ProviderResult((), LimitError.PARSE)
        init_resp = _response_object(init_line)
        read_resp = _response_object(read_line)
        if init_resp is None or read_resp is None:
            return ProviderResult((), LimitError.PARSE)
        for resp in (init_resp, read_resp):
            error = resp.get("error")
            if error is not None:
                return ProviderResult((), _classify_error(error))
        result = read_resp.get("result")
        if not isinstance(result, dict):
            return ProviderResult((), LimitError.PARSE)
        snapshots = _snapshots(ctx, result)
        if snapshots is None:
            return ProviderResult((), LimitError.PARSE)
        return ProviderResult(snapshots, None)
