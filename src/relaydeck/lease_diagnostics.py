"""Fixed-schema, content-free lease lifecycle diagnostics."""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
import secrets
import time
from dataclasses import dataclass

LOG = logging.getLogger("relaydeck.lease_lifecycle")
_KEY = secrets.token_bytes(32)  # Process-local correlation; never stored or logged.
_LEASE = re.compile(r"lease-[0-9]{1,12}\Z")
_EVENTS = frozenset(
    {
        "lease_issue",
        "lease_renew",
        "lease_release",
        "renew_failure",
        "renew_callback",
        "socket_open",
        "socket_close",
        "watch_retry",
        "viewer_bind",
        "viewer_reopen",
        "ui_resume",
    }
)
_REASONS = frozenset(
    {
        "ok",
        "released",
        "reused",
        "local_close",
        "socket_replaced",
        "peer_disconnect",
        "replaced",
        "receive_failure",
        "cancelled",
        "current",
        "superseded",
        "lease_expired",
        "lease_conflict",
        "stale_revision",
        "unauthenticated",
        "forbidden",
        "not_found",
        "host_offline",
        "rate_limited",
        "resume_failed",
        "protocol_error",
        "probe_timeout",
        "reset_mismatch",
        "unknown_native",
        "instance_changed",
        "offset_future",
        "offset_regressed",
        "offset_evicted",
        "upstream_timeout",
        "too_large",
        "unsupported_capability",
        "bad_frame",
        "bad_control",
        "offset_gap",
        "epoch_changed",
        "reset",
        "gap",
        "bye",
        "overflow",
        "stale_epoch",
        "stale_offset",
        "detached",
        "closed",
        "exited",
        "attach_dead",
        "stream_closed",
        "retry",
        "exhausted",
        "bound",
        "reopened",
        "started",
        "resumed",
        "incomplete",
        "noop",
    }
)
_SOURCES = frozenset(
    {
        "client",
        "server",
        "local_deadline",
        "renew_timeout",
        "server_refusal",
        "invalid_expiry",
        "returned_expired",
        "unexpected",
        "native",
        "ui_transport",
    }
)


def opaque(namespace: str, *parts: str | int) -> str:
    """Hash identifiers without retaining printable terminal/identity content."""
    framed = namespace.encode() + b"\0"
    for part in parts:
        data = str(part).encode() if type(part) in (str, int) else b"unknown"
        framed += len(data).to_bytes(8, "big") + data
    return hmac.new(_KEY, framed, hashlib.sha256).hexdigest()[:16]


def _lease(value: str) -> str:
    if not isinstance(value, str) or not value:
        return "-"
    return value if _LEASE.fullmatch(value) else opaque("lease", value)


def _number(value: int) -> int:
    return value if type(value) is int and 0 <= value <= 2**63 - 1 else 0


def safe_reason(value: str) -> str:
    return value if isinstance(value, str) and value in _REASONS else "other"


def log_ui_resume_incomplete(reason: str) -> None:
    LOG.info("ui_resume_incomplete why=%s", safe_reason(reason))


@dataclass(frozen=True)
class LeaseTrace:
    session: str
    terminal: str
    lease_id: str = ""
    mode: str = ""
    generation: int = 0

    @classmethod
    def for_session(
        cls,
        session: object,
        host: str,
        terminal: str,
        lease_id: str = "",
        mode: str = "",
        generation: int = 0,
    ) -> LeaseTrace:
        return cls(
            opaque("session", id(session)),
            opaque("terminal", host, terminal),
            lease_id,
            mode,
            generation,
        )

    def emit(
        self,
        event: str,
        *,
        reason: str = "",
        source: str = "client",
        attempt: int = 0,
        current_lease_id: str = "",
        viewer: int = 0,
        ui: object | None = None,
        socket: object | None = None,
    ) -> None:
        # No arbitrary **fields, exception strings, reprs, URLs or payloads.
        LOG.info(
            "lease_lifecycle t=%.6f event=%s session=%s terminal=%s lease=%s "
            "current_lease=%s mode=%s generation=%s reason=%s source=%s attempt=%s "
            "viewer=%s ui=%s socket=%s",
            time.monotonic(),
            event if isinstance(event, str) and event in _EVENTS else "unknown",
            opaque("correlation", self.session),
            opaque("correlation", self.terminal),
            _lease(self.lease_id),
            _lease(current_lease_id),
            self.mode
            if isinstance(self.mode, str) and self.mode in ("control", "view")
            else "unknown",
            _number(self.generation),
            safe_reason(reason),
            source if isinstance(source, str) and source in _SOURCES else "other",
            _number(attempt),
            _number(viewer),
            opaque("ui", id(ui)) if ui is not None else "-",
            opaque("socket", id(socket)) if socket is not None else "-",
        )
