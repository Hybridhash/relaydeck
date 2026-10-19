"""Combined web entry: Flet UI + gateway API on one origin.

Supported Flet ASGI integration seam (selected and documented here):

* One FastAPI app serves the gateway API routes, two auth routes, and
  the Flet app (``flet.fastapi.app(main)``) mounted last. Single origin
  means no CORS and shared cookies. Served by one uvicorn worker
  (``--workers 1``); every registry below is in-memory and a second
  worker would split-brain sessions, ownership, and WS closers.
* ``rd_session`` cookies are HMAC-signed session IDs (HttpOnly,
  SameSite=Lax, Secure on https). Gateway tokens live ONLY in the
  server-side ``WebSessionStore``; the browser never sees them.
* A pure-ASGI guard middleware enforces exact Origin on the ``/ws``
  handshake, permits anonymous login screens and requires exactly one
  initial REGISTER_CLIENT frame. Reconnect IDs stay bound to a current
  cookie identity. A rejected reconnect closes before Flet sees the ID;
  an independent browser dialog offers deliberate sign-in recovery.
* ``POST /v1/auth/logout`` (exact-Origin CSRF gate) revokes the
  gateway session, severs device terminal sockets, closes every Flet
  WS for the sid, deletes the Flet sessions, and clears the cookie.
* ``GET /v1/auth/callback`` completes the gateway code exchange
  server-side (state-bound, single-use) and mints the cookie. The
  upstream identity provider that issues codes is still a
  human decision; the mechanism is proven with seeded codes.

Server-only module: importing it requires the ``web`` + ``gateway``
groups. It is never imported by the device app (boundary test).

Pinned against flet 1.0.3 internals (REGISTER_CLIENT action code,
frame prefix, unique session key shape). Drives the real
protocol, so a Flet upgrade that moves these fails loudly.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
from collections.abc import Callable
from contextlib import asynccontextmanager
from contextvars import ContextVar, copy_context
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, TypeVar
from urllib.parse import urlencode, urlparse

import msgpack  # type: ignore[import-untyped]
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

from relaydeck.client.features.auth.web_recovery import (
    RECOVERY_PATH,
    RECOVERY_REASON,
    RECOVERY_SCRIPT,
    BrowserRecoveryMiddleware,
)
from relaydeck.client.features.terminal.diagnostics import diagnostics_logger
from relaydeck.contracts.schemas import validate
from relaydeck.devtools.dev_server import (  # noqa: F401  (re-exported for tests and the runner)
    _DEV_HOSTS,
    _DEV_LOGIN_TOKEN,
    DEMO_AGENT_ID,
    DEMO_AGENT_TAB,
    DEMO_GREETING,
    DEMO_SHELL_TAB,
    DemoBackend,
    _dev_login,
    dev_demo_snapshot,
    dev_snapshot,
)
from relaydeck.gateway.app import BodyCapMiddleware, GatewayState
from relaydeck.gateway.backend import FakeBackend
from relaydeck.gateway.identity import REFRESH_TTL_MS, Session, SessionStore
from relaydeck.gateway.push_io import push_io
from relaydeck.persistence_contracts import Record, StorageUnavailable, Table

if TYPE_CHECKING:
    from relaydeck.companion.persistence import PersistentStore

logger = logging.getLogger(__name__)

COOKIE_NAME = "rd_session"
WS_PATH = "/ws"
REGISTER_ACTION = 1  # flet.messaging.protocol.MessageAction.REGISTER_CLIENT
REGISTER_TIMEOUT_S = 10.0
MAX_REGISTER_BYTES = 64 * 1024
MAX_VISIBILITY_BYTES = 512
FLET_HANDOFF_TIMEOUT_S = 5.0

# Web sign-in lifetime. Sliding: 30 days without use; every verified live use (the access-token
# rotation) moves the expiry to now + 30 days, at most once per hour so storage writes stay low.
# Absolute: 90 days after the original pairing, a re-pair is required (bounds a stolen cookie).
# The gateway's refresh grant is also 30 days sliding, and the web session never outlives it.
SESSION_TTL_S = 30 * 24 * 3600
SESSION_MAX_S = 90 * 24 * 3600
SESSION_EXTEND_INTERVAL_S = 3600
LEGACY_SESSION_TTL_S = 12 * 3600  # rows written before the sliding rule carry no pairing time
LOGIN_TTL_S = 600


def _flet_session_timeout() -> int:
    """Match the pinned Flet mount's default and nonempty environment override."""
    from flet_web.fastapi import flet_app  # type: ignore[import-untyped]

    configured = os.getenv("FLET_SESSION_TIMEOUT")
    return int(configured) if configured else int(flet_app.DEFAULT_FLET_SESSION_TIMEOUT)


@dataclass(frozen=True)
class WebIdentity:
    account: str
    device_id: str
    sid: str = field(repr=False)


@dataclass
class _WebSession:
    identity: WebIdentity
    access_token: str = field(repr=False)
    refresh_token: str = field(repr=False)
    access_expires_ms: int
    expires_ms: int
    refresh_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    generation: int = 1
    paired_ms: int = 0  # the original pairing: the absolute cap counts from here
    extended_ms: int = 0  # when the expiry was last moved (throttles extension writes)


@dataclass(frozen=True)
class _Handshake:
    ip: str
    user_agent: str
    page_name: str


@dataclass(eq=False)
class _FletTransport:
    close: Callable[..., Any]
    closed: asyncio.Event
    done: asyncio.Event = field(default_factory=asyncio.Event)
    flet_id: str = ""


@dataclass
class _PageVisibility:
    identity: WebIdentity
    session: Any
    report: Callable[[str, bool], None]
    since: float = float("-inf")
    count: int = 0


_PrivateResult = TypeVar("_PrivateResult")


@dataclass
class WebSessionStore:
    """Server-side web sessions, pending logins, and Flet ownership."""

    public_origin: str
    cookie_secret: bytes = field(repr=False)
    session_ttl_s: int = SESSION_TTL_S
    session_max_s: int = SESSION_MAX_S
    extend_interval_s: int = SESSION_EXTEND_INTERVAL_S
    login_ttl_s: int = LOGIN_TTL_S
    now_ms: Callable[[], int] = lambda: int(time.time() * 1000)
    persistence: PersistentStore | None = field(default=None, repr=False)
    gateway_sessions: SessionStore | None = field(default=None, repr=False)
    session_allowed: Callable[[Session], bool] | None = field(default=None, repr=False)
    _sessions: dict[str, _WebSession] = field(default_factory=dict, repr=False)
    _pending: dict[str, tuple[str, int]] = field(default_factory=dict)  # state -> (verifier, exp)
    _owners: dict[str, str | None] = field(default_factory=dict)  # flet raw id -> sid|None
    _handshakes: dict[str, _Handshake] = field(default_factory=dict)
    _closers: dict[str, list[Callable[[], Any]]] = field(default_factory=dict)
    command_store: Any = None
    command_cleanup_error: str = ""
    browsers: dict[str, int] = field(default_factory=dict)
    browser_pages: dict[str, str] = field(default_factory=dict)
    pairing_flows: dict[str, Any] = field(default_factory=dict)
    pairing_ready: dict[str, tuple[int, dict[str, Any], dict[str, Any], int]] = field(
        default_factory=dict
    )
    pairing_tasks: dict[tuple[str, str, int], Any] = field(default_factory=dict, repr=False)
    _denied_sids: set[str] = field(default_factory=set, repr=False)
    _draft_tabs: dict[str, str] = field(default_factory=dict, repr=False)
    _tab_claims: dict[tuple[str, str, str], str] = field(default_factory=dict, repr=False)
    _pending_revocations: dict[str, _WebSession] = field(default_factory=dict, repr=False)
    _cookie_expiry: dict[str, int] = field(default_factory=dict, repr=False)
    _visibility_pages: dict[str, _PageVisibility] = field(default_factory=dict, repr=False)
    _visibility_clock: Callable[[], float] = field(default=time.monotonic, repr=False)

    async def run_private(self, operation: Callable[[], _PrivateResult]) -> _PrivateResult:
        if self.persistence is not None:
            context = copy_context()
            return await self.persistence.run_draft(lambda: context.run(operation))
        return operation()

    @staticmethod
    def _sid_key(sid: str) -> str:
        return hashlib.sha256(sid.encode()).hexdigest()

    def _web_record(self, session: _WebSession) -> Record:
        assert self.gateway_sessions is not None
        doc = {
            "account": session.identity.account,
            "device_id": session.identity.device_id,
            "access_token": session.access_token,
            "refresh_token": session.refresh_token,
            "access_hash": SessionStore.verifier(session.access_token),
            "access_expires_ms": session.access_expires_ms,
            "expires_ms": session.expires_ms,
            "paired_ms": session.paired_ms,
            "extended_ms": session.extended_ms,
            "auth_generation": self.gateway_sessions._generation,
        }
        return Record(
            Table.WEB_SESSIONS,
            self._sid_key(session.identity.sid),
            (self.public_origin, session.identity.account, session.identity.device_id),
            json.dumps(doc, sort_keys=True).encode(),
            session.generation,
            self.now_ms(),
            session.expires_ms,
        )

    def _read_web(self, sid: str) -> _WebSession | None:
        assert self.persistence is not None
        authority = self.gateway_sessions
        if authority is None or not authority.check_generation() or sid in self._denied_sids:
            return None
        with authority._lock, self.persistence.transaction() as tx:
            row = tx.get(Table.WEB_SESSIONS, self._sid_key(sid))
            if row is None or row.scope[0] != self.public_origin:
                return None
            try:
                doc = json.loads(row.body)
                if (
                    row.scope != (self.public_origin, doc["account"], doc["device_id"])
                    or doc["auth_generation"] != authority._generation
                    or doc["expires_ms"] < self.now_ms()
                ):
                    return None
                grant = tx.get(
                    Table.GATEWAY_VERIFIERS, "refresh:" + authority.verifier(doc["refresh_token"])
                )
                verified = authority._decode_authority(grant) if grant else None
                if (
                    verified is None
                    or verified[0].refresh_expires_ms < self.now_ms()
                    or verified[0].account != doc["account"]
                    or verified[0].device_id != doc["device_id"]
                    or verified[1] != doc["access_hash"]
                    or verified[1] in authority._denied
                    or verified[2] in authority._denied
                    or any(row.scope[: len(scope)] == scope for scope in authority._denied_scopes)
                    or (self.session_allowed is not None and not self.session_allowed(verified[0]))
                ):
                    return None
                authority._known_pairs[verified[1]] = verified[0], verified[2]
                return _WebSession(
                    WebIdentity(doc["account"], doc["device_id"], sid),
                    doc["access_token"],
                    doc["refresh_token"],
                    doc["access_expires_ms"],
                    doc["expires_ms"],
                    generation=row.revision,
                    paired_ms=self._lifetime(doc)[0],
                    extended_ms=self._lifetime(doc)[1],
                )
            except (KeyError, TypeError, ValueError):
                raise StorageUnavailable("corrupt") from None

    @staticmethod
    def _lifetime(doc: dict[str, Any]) -> tuple[int, int]:
        """Pairing and extension times of a stored row. Rows from before the sliding rule have
        neither: they were paired one legacy lifetime before their expiry and are not extended
        by being restored."""
        expires = int(doc["expires_ms"])
        paired = int(doc.get("paired_ms") or expires - LEGACY_SESSION_TTL_S * 1000)
        return paired, int(doc.get("extended_ms") or paired)

    def _extension(
        self, session: _WebSession, now: int, refresh_expires_ms: int
    ) -> tuple[int, int]:
        """(expires_ms, extended_ms) after a verified live use. The expiry moves to now + the
        idle lifetime, but never past the absolute cap from pairing or past the refresh grant the
        session holds, and at most once per `extend_interval_s`."""
        if now - session.extended_ms < self.extend_interval_s * 1000:
            return session.expires_ms, session.extended_ms
        cap = min(session.paired_ms + self.session_max_s * 1000, refresh_expires_ms)
        moved = min(now + self.session_ttl_s * 1000, cap)
        if moved <= session.expires_ms:
            return session.expires_ms, session.extended_ms
        return moved, now

    def _current_session(
        self, sid: str, *, restore: bool = False, strict: bool = False
    ) -> _WebSession | None:
        if sid in self._denied_sids:
            return None
        session = self._sessions.get(sid)
        if self.persistence is not None:
            if session is None and not restore:
                return None  # Restoration requires a verified presented cookie.
            try:
                stored = self._read_web(sid)
            except StorageUnavailable:
                if strict:
                    raise  # a read-only observer must tell "unavailable" from "signed out"
                return None
            if stored is None:
                return None
            if session is None:
                self._sessions[sid] = session = stored
            elif stored.generation != session.generation:
                session.access_token, session.refresh_token = (
                    stored.access_token,
                    stored.refresh_token,
                )
                session.access_expires_ms, session.generation = (
                    stored.access_expires_ms,
                    stored.generation,
                )
                # Another writer rotated (and may have extended) it: follow the stored lifetime.
                session.expires_ms = stored.expires_ms
                session.paired_ms, session.extended_ms = stored.paired_ms, stored.extended_ms
        return session if session is not None and session.expires_ms >= self.now_ms() else None

    def begin_revocation(self, sid: str) -> WebIdentity | None:
        """No access lookup: fence first and keep retry identity on failure."""
        self._denied_sids.add(sid)
        session = self._pending_revocations.get(sid) or self._sessions.get(sid)
        if session is not None:
            self._pending_revocations[sid] = session
            if self.gateway_sessions is not None:
                self.gateway_sessions.begin_revocation(
                    session.access_token, refresh=session.refresh_token
                )
        return session.identity if session is not None else None

    def revoke_sid(self, sid: str) -> WebIdentity | None:
        """Revocation-only, after signature verification; errors propagate.

        Ignore access-denial/expiry, never scope. Absence counts as success
        only after a successful durable read/commit, not a failed lookup.
        """
        session = self._pending_revocations.get(sid)
        authority = self.gateway_sessions
        if self.persistence is not None:
            if authority is None:
                raise StorageUnavailable("unavailable")
            with authority._lock, self.persistence.transaction() as tx:
                row = tx.get(Table.WEB_SESSIONS, self._sid_key(sid))
                if row is not None:
                    if row.scope[0] != self.public_origin:
                        return None  # Successful read: no credential in this origin's scope.
                    try:
                        doc = json.loads(row.body)
                        identity = WebIdentity(doc["account"], doc["device_id"], sid)
                        if (
                            row.scope != (self.public_origin, identity.account, identity.device_id)
                            or doc["access_hash"] != authority.verifier(doc["access_token"])
                            or (session is not None and session.identity != identity)
                        ):
                            raise ValueError
                        session = _WebSession(
                            identity,
                            doc["access_token"],
                            doc["refresh_token"],
                            doc["access_expires_ms"],
                            doc["expires_ms"],
                            generation=row.revision,
                            paired_ms=self._lifetime(doc)[0],
                            extended_ms=self._lifetime(doc)[1],
                        )
                    except (KeyError, TypeError, ValueError):
                        raise StorageUnavailable("corrupt") from None
                    self._pending_revocations[sid] = session
                if session is not None:
                    scope = (
                        self.public_origin,
                        session.identity.account,
                        session.identity.device_id,
                    )
                    authority.revoke_in_transaction(tx, session.access_token, scope=scope)
                    remaining = tx.get(Table.WEB_SESSIONS, self._sid_key(sid))
                    if remaining is not None:
                        tx.delete(
                            remaining.table, remaining.key, expected_revision=remaining.revision
                        )
                    for alias in authority._rows(tx, Table.TAB_ALIASES, scope):
                        if json.loads(alias.body)["sid_hash"] == self._sid_key(sid):
                            tx.delete(alias.table, alias.key, expected_revision=alias.revision)
            if session is not None:
                authority.finish_revocation(authority.verifier(session.access_token))
        elif session is not None and authority is not None:
            authority.revoke_session(session.access_token)
        self._pending_revocations.pop(sid, None)
        return session.identity if session is not None else None

    def rotate_web(self, sid: str, expected_generation: int) -> str | None:
        assert self.persistence is not None and self.gateway_sessions is not None
        authority = self.gateway_sessions
        with authority._lock, self.persistence.transaction() as tx:
            row = tx.get(Table.WEB_SESSIONS, self._sid_key(sid))
            if (
                row is None
                or row.revision != expected_generation
                or row.expires_ms is None
                or row.expires_ms < self.now_ms()
                or not authority.check_generation()
            ):
                return None
            session = self._sessions.get(sid)
            if session is None or session.generation != expected_generation:
                return None
            rotated = authority.prepare_refresh(
                tx, session.refresh_token, self.now_ms(), self.session_allowed
            )
            if rotated is None:
                return None
            pair, principal, _, _ = rotated
            # A verified live use extends the sign-in; the extension is written in this same
            # transaction as the rotation (restoring a session never gets here).
            expires_ms, extended_ms = self._extension(
                session, self.now_ms(), principal.refresh_expires_ms
            )
            next_session = _WebSession(
                session.identity,
                pair[0],
                pair[1],
                principal.access_expires_ms,
                expires_ms,
                generation=expected_generation + 1,
                paired_ms=session.paired_ms,
                extended_ms=extended_ms,
            )
            if not tx.put(self._web_record(next_session), expected_revision=expected_generation):
                raise StorageUnavailable("revision_conflict")
        authority.publish_refresh(rotated)
        # Publish only after the verifiers AND encrypted bearer row committed.
        session.access_token, session.refresh_token = pair
        session.access_expires_ms = principal.access_expires_ms
        session.generation = expected_generation + 1
        session.expires_ms, session.extended_ms = expires_ms, extended_ms
        return pair[0]

    def claim_draft_tab(self, sid: str, requested_id: str, flet_id: str) -> str:
        identity = self.identity(sid)

        def current() -> bool:
            return _transport_is_live() and (
                _current_transport.get() is None or self.owner_of(flet_id) == sid
            )

        if identity is None or not current():
            return ""
        if flet_id in self._draft_tabs:
            return self._draft_tabs[flet_id]
        tab = secrets.token_urlsafe(24)
        if self.persistence is not None:
            assert self.gateway_sessions is not None
            scope = (self.public_origin, identity.account, identity.device_id)

            def key(fid: str) -> str:
                return hashlib.sha256(json.dumps([scope, fid]).encode()).hexdigest()

            with self.persistence.transaction() as tx:
                old = tx.get(Table.TAB_ALIASES, key(requested_id)) if requested_id else None
                if (
                    old
                    and old.scope == scope
                    and old.expires_ms is not None
                    and old.expires_ms >= self.now_ms()
                ):
                    doc = json.loads(old.body)
                    if (
                        doc["sid_hash"] == self._sid_key(sid)
                        and doc["auth_generation"] == self.gateway_sessions._generation
                    ):
                        candidate = doc["tab"]
                        if (
                            identity.account,
                            identity.device_id,
                            candidate,
                        ) not in self._tab_claims:
                            tab = candidate
                doc = {
                    "tab": tab,
                    "sid_hash": self._sid_key(sid),
                    "auth_generation": self.gateway_sessions._generation,
                }
                previous = tx.get(Table.TAB_ALIASES, key(flet_id))
                tx.put(
                    Record(
                        Table.TAB_ALIASES,
                        key(flet_id),
                        scope,
                        json.dumps(doc).encode(),
                        previous.revision + 1 if previous else 1,
                        self.now_ms(),
                        self._sessions[sid].expires_ms,
                    ),
                    expected_revision=previous.revision if previous else None,
                )
        if not current():
            return ""  # A late storage completion cannot publish over its successor.
        self._draft_tabs[flet_id] = tab
        self._tab_claims[(identity.account, identity.device_id, tab)] = flet_id
        return tab

    def draft_tab_for(self, sid: str, flet_id: str) -> str:
        return self._draft_tabs.get(flet_id, "") if self.owner_of(flet_id) == sid else ""

    def browser_identity(self, cookie: str) -> str | None:
        signed, _, _sig = cookie.rpartition(".")
        if not signed.startswith("browser:") or not hmac.compare_digest(
            self.sign_sid(signed), cookie
        ):
            return None
        browser = signed.removeprefix("browser:")
        return browser if self.browsers.get(browser, 0) > self.now_ms() else None

    def issue_browser_cookie(self) -> str:
        self.browsers = {
            key: expiry for key, expiry in self.browsers.items() if expiry > self.now_ms()
        }
        if len(self.browsers) >= 256:
            raise ValueError("Browser attempt capacity reached")
        browser = secrets.token_urlsafe(24)
        self.browsers[browser] = self.now_ms() + 600_000
        secure = "; Secure" if self.public_origin.startswith("https://") else ""
        return (
            f"rd_browser={self.sign_sid('browser:' + browser)}; Path=/; HttpOnly; "
            f"SameSite=Lax{secure}; Max-Age=600"
        )

    # -- cookies ------------------------------------------------------

    def sign_sid(self, sid: str) -> str:
        sig = hmac.new(self.cookie_secret, sid.encode(), hashlib.sha256).hexdigest()
        return f"{sid}.{sig}"

    def signed_sid(self, value: str) -> str | None:
        """Verify possession only; this alone NEVER grants app access."""
        sid, sep, sig = value.partition(".")
        if not sep or not sid:
            return None
        expected = hmac.new(self.cookie_secret, sid.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, sig):
            return None
        return sid

    def verify_cookie(self, value: str) -> str | None:
        sid = self.signed_sid(value)
        if sid is None:
            return None
        session = self._current_session(sid, restore=True)
        if session is None or session.expires_ms < self.now_ms():
            return None
        return sid

    def cookie_headers(self, sid: str) -> dict[str, str]:
        """The session cookie. Max-Age is the time left on the server session, so the browser
        keeps it exactly as long as the server will honour it."""
        secure = "; Secure" if urlparse(self.public_origin).scheme == "https" else ""
        session = self._sessions.get(sid)
        if session is None:
            max_age = self.session_ttl_s
        else:
            max_age = max(0, (session.expires_ms - self.now_ms()) // 1000)
            self._cookie_expiry[sid] = session.expires_ms
        return {
            "Set-Cookie": (
                f"{COOKIE_NAME}={self.sign_sid(sid)}; Path=/; HttpOnly"
                f"; SameSite=Lax{secure}; Max-Age={max_age}"
            )
        }

    def cookie_renewal(self, value: str) -> dict[str, str] | None:
        """A fresh Set-Cookie when the session behind this (signed) cookie was extended after
        the cookie was last issued. Memory only: no storage read, no access is granted, and a
        retired or unknown session gets nothing."""
        sid = self.signed_sid(value)
        if sid is None or sid in self._denied_sids:
            return None
        session = self._sessions.get(sid)
        if (
            session is None
            or session.expires_ms < self.now_ms()
            or session.expires_ms <= self._cookie_expiry.get(sid, 0)
        ):
            return None
        return self.cookie_headers(sid)

    @staticmethod
    def clear_cookie_headers() -> dict[str, str]:
        return {"Set-Cookie": f"{COOKIE_NAME}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0"}

    # -- sessions -----------------------------------------------------

    def create_session(
        self, account: str, device_id: str, access: str, refresh: str, access_expires_ms: int
    ) -> str:
        sid = secrets.token_urlsafe(24)
        now = self.now_ms()
        session = _WebSession(
            WebIdentity(account, device_id, sid),
            access,
            refresh,
            access_expires_ms,
            now + self.session_ttl_s * 1000,
            paired_ms=now,
            extended_ms=now,
        )
        if self.persistence is not None:
            if self.gateway_sessions is None or not self.gateway_sessions.check_generation():
                raise StorageUnavailable("unavailable")
            principal = self.gateway_sessions.lookup(access, now)
            if (
                principal is None
                or principal.account != account
                or principal.device_id != device_id
            ):
                raise StorageUnavailable("invalid_record")
            with self.persistence.transaction() as tx:
                tx.put(self._web_record(session), expected_revision=None)
        self._sessions[sid] = session
        return sid

    def identity(self, sid: str | None) -> WebIdentity | None:
        if not sid:
            return None
        session = self._current_session(sid)
        if session is None or session.expires_ms < self.now_ms():
            return None
        return session.identity

    def observe(self, cookie: str) -> tuple[WebIdentity, str] | None:
        """Signed-in lookup for a read-only check; raises StorageUnavailable, never hides it."""
        sid = self.signed_sid(cookie)
        if sid is None:
            return None
        session = self._current_session(sid, restore=True, strict=True)
        if session is None or session.expires_ms < self.now_ms():
            return None
        return session.identity, session.access_token

    def take_session(self, sid: str) -> tuple[str, str] | None:
        """Pop a session's gateway tokens (logout path)."""
        session = self._current_session(sid)
        if session is None:
            return None
        return session.access_token, session.refresh_token

    def retire_sid(self, sid: str) -> None:
        self._denied_sids.add(sid)
        if self.persistence is not None:
            with self.persistence.transaction() as tx:
                row = tx.get(Table.WEB_SESSIONS, self._sid_key(sid))
                if row is not None:
                    tx.delete(row.table, row.key, expected_revision=row.revision)
        self.retire_local(sid)

    def retire_local(self, sid: str) -> None:
        """Local writers/pages retire even when durable logout must retry."""
        self._denied_sids.add(sid)
        self._sessions.pop(sid, None)
        for flet_id, owner in list(self._owners.items()):
            if owner == sid:
                tab = self._draft_tabs.pop(flet_id, "")
                for claim, holder in list(self._tab_claims.items()):
                    if holder == flet_id and claim[2] == tab:
                        del self._tab_claims[claim]
                del self._owners[flet_id]
                self._handshakes.pop(flet_id, None)
                self._visibility_pages.pop(flet_id, None)
        self._closers.pop(sid, None)

    async def ensure_access(
        self, sid: str, refresher: Callable[[str], Any], *, force: bool = False
    ) -> str | None:
        """Valid access token, refreshing via the gateway when stale."""
        cached = self._sessions.get(sid)
        original_access = cached.access_token if cached is not None else ""
        in_progress = cached.refresh_lock.locked() if cached is not None else False
        session = await self.run_private(lambda: self._current_session(sid))
        if session is None or session.expires_ms < self.now_ms():
            return None
        async with session.refresh_lock:
            now = self.now_ms()
            if self._sessions.get(sid) is not session or session.expires_ms < now:
                return None
            if in_progress or (force and session.access_token != original_access):
                return session.access_token
            if not force and session.access_expires_ms - 60_000 > now:
                return session.access_token
            if self.persistence is not None:
                return await self.run_private(lambda: self.rotate_web(sid, session.generation))
            rotated = await refresher(session.refresh_token)
            if rotated is None or self._sessions.get(sid) is not session:
                return None
            access, refresh, expires_in = rotated
            session.access_token = access
            session.refresh_token = str(refresh)
            session.access_expires_ms = now + int(expires_in) * 1000
            # The gateway slid the refresh grant to 30 days from this refresh.
            session.expires_ms, session.extended_ms = self._extension(
                session, now, now + REFRESH_TTL_MS
            )
            return str(access)

    # -- login --------------------------------------------------------

    def begin_login(self, gateway_base: str) -> tuple[str, str]:
        state = secrets.token_urlsafe(24)
        verifier = secrets.token_urlsafe(48)[:64]
        digest = hashlib.sha256(verifier.encode()).digest()
        challenge = base64.urlsafe_b64encode(digest).decode().rstrip("=")
        self._pending[state] = (verifier, self.now_ms() + self.login_ttl_s * 1000)
        request = {
            "response_type": "code",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": state,
            "redirect_uri": self.public_origin.rstrip("/") + "/v1/auth/callback",
        }
        validate("relaydeck/gateway/v1/authorization-request", request)
        url = gateway_base.rstrip("/") + "/v1/authorize?" + urlencode(request)
        return state, url

    def take_pending(self, state: str) -> str | None:
        entry = self._pending.pop(state, None)
        if entry is None or entry[1] < self.now_ms():
            return None
        return entry[0]

    # -- flet ownership -----------------------------------------------

    def bind_flet(self, sid: str | None, flet_id: str, handshake: _Handshake) -> None:
        self._bind_verified(sid, flet_id, handshake, self.identity(sid))

    def _bind_verified(
        self, sid: str | None, flet_id: str, handshake: _Handshake, identity: WebIdentity | None
    ) -> None:
        if identity is None or identity.sid != sid or sid in self._denied_sids:
            sid = None
        self._owners[flet_id] = sid
        self._handshakes[flet_id] = handshake

    def owner_of(self, flet_id: str) -> str | None:
        return self._owners.get(flet_id)

    def flet_ids(self, sid: str) -> list[str]:
        return [fid for fid, owner in self._owners.items() if owner == sid]

    def unique_key(self, flet_id: str) -> str | None:
        """Rebuild Flet's unique session key for server-side deletion."""
        handshake = self._handshakes.get(flet_id)
        if handshake is None:
            return None
        digest = hashlib.sha1(f"{handshake.ip}{handshake.user_agent}".encode()).hexdigest()
        return f"{handshake.page_name}_{flet_id}_{digest}"

    # -- ws closers ----------------------------------------------------

    def add_closer(self, sid: str | None, closer: Callable[[], Any]) -> None:
        if sid:
            self._closers.setdefault(sid, []).append(closer)

    def remove_closer(self, sid: str | None, closer: Callable[[], Any]) -> None:
        held = self._closers.get(sid or "")
        if held is not None and closer in held:
            held.remove(closer)
            if not held:
                self._closers.pop(sid or "", None)

    async def close_all(self, sid: str) -> int:
        closers = self._closers.pop(sid, [])
        closed = 0
        for closer in closers:
            try:
                await closer()
                closed += 1
            except Exception:  # noqa: BLE001 - best-effort sever
                pass
        return closed


def _client_message(frame: bytes) -> Any:
    """Decode a Flet protocol packet; leave DataChannel bytes opaque."""
    if len(frame) < 2 or frame[0] != 0x00:
        return None
    try:
        return msgpack.unpackb(frame[1:], raw=False)
    except Exception:  # noqa: BLE001 - malformed packets fail the registration gate
        return None


def parse_register(frame: bytes) -> tuple[bool, str]:
    """(is_valid_register, session_id) from a binary frame; never raises."""
    payload = _client_message(frame)
    if payload is None:
        return False, ""
    if not isinstance(payload, list | tuple) or len(payload) != 2:
        return False, ""
    action, body = payload
    if type(action) is not int or action != REGISTER_ACTION or not isinstance(body, dict):
        return False, ""
    session_id = body.get("session_id")
    if session_id is not None and not isinstance(session_id, str):
        return False, ""
    return True, session_id or ""


def _register_attempt(frame: bytes) -> bool:
    payload = _client_message(frame)
    # Python Enum coercion also accepts True/1.0 as action 1. Reject every
    # such attempt after registration, including malformed register bodies.
    return isinstance(payload, list | tuple) and len(payload) == 2 and payload[0] == REGISTER_ACTION


# Set by the guard middleware per WebSocket handshake; read by before_main
# and on_connect handlers (context flows through Flet's task chain).
_current_handshake: ContextVar[tuple[str | None, _Handshake] | None] = ContextVar(
    "relaydeck_handshake", default=None
)
_current_browser: ContextVar[str | None] = ContextVar("relaydeck_browser", default=None)
_requested_flet: ContextVar[str] = ContextVar("relaydeck_requested_flet", default="")
_current_transport: ContextVar[_FletTransport | None] = ContextVar(
    "relaydeck_flet_transport", default=None
)


def _transport_is_live() -> bool:
    transport = _current_transport.get()
    return transport is None or not (transport.closed.is_set() or transport.done.is_set())


def make_before_main(store: WebSessionStore) -> Any:
    """Bind Flet session ownership deterministically before main() runs."""

    async def before_main(page: Any) -> None:
        held = _current_handshake.get()
        sid, handshake = held if held is not None else (None, None)
        try:
            flet_id = page.session.id
        except RuntimeError:
            return
        if handshake is None:
            handshake = _Handshake(ip="", user_agent="", page_name="")
        identity = await store.run_private(lambda: store.identity(sid))
        if not _transport_is_live():
            return
        store._bind_verified(sid, flet_id, handshake, identity)
        if sid is not None:
            await store.run_private(
                lambda: store.claim_draft_tab(sid, _requested_flet.get(), flet_id)
            )
        if not _transport_is_live():
            return
        if browser := _current_browser.get():
            store.browser_pages[flet_id] = browser

    return before_main


async def rebind_on_connect(store: WebSessionStore, page: Any) -> None:
    """Adopt ownership on reattach; call from page.on_connect, then refresh."""
    held = _current_handshake.get()
    if held is None:
        return
    if not _transport_is_live():
        return  # A delayed connect callback from a replaced socket has no authority.
    sid, handshake = held
    try:
        flet_id = page.session.id
    except RuntimeError:
        return
    identity = await store.run_private(lambda: store.identity(sid))
    if not _transport_is_live():
        return
    store._bind_verified(sid, flet_id, handshake, identity)
    if sid is not None:
        await store.run_private(lambda: store.claim_draft_tab(sid, _requested_flet.get(), flet_id))
    if not _transport_is_live():
        return
    if browser := _current_browser.get():
        store.browser_pages[flet_id] = browser


def _cookies(headers: dict[str, str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in headers.get("cookie", "").split(";"):
        name, sep, value = part.strip().partition("=")
        if sep:
            out[name] = value
    return out


class PictureUploadMiddleware:
    """Translate a validated same-origin browser session into upload proof.

    Flet's web picker sends same-origin cookies but cannot set bearer headers.
    Native uploads supply their own bearer; a grant URL is never sufficient.
    """

    def __init__(self, app: Any, store: WebSessionStore) -> None:
        self._app, self._store = app, store

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> Any:
        if not (
            scope["type"] == "http"
            and scope.get("method") == "PUT"
            and re.fullmatch(r"/v1/pictures/[a-f0-9]{32}/content", scope.get("path", ""))
        ):
            return await self._app(scope, receive, send)
        headers = [(key.lower(), value) for key, value in scope.get("headers", [])]
        authorization = [value for key, value in headers if key == b"authorization"]
        if len(authorization) == 1:
            return await self._app(scope, receive, send)  # Gateway validates native bearer.
        origins = [value.decode("latin1") for key, value in headers if key == b"origin"]
        cookies = [value.decode("latin1") for key, value in headers if key == b"cookie"]
        sid = (
            await self._store.run_private(
                lambda: self._store.verify_cookie(
                    _cookies({"cookie": cookies[0]}).get(COOKIE_NAME, "")
                )
            )
            if not authorization and len(cookies) == 1 and origins == [self._store.public_origin]
            else None
        )
        tokens = (
            await self._store.run_private(lambda: self._store.take_session(sid))
            if sid is not None
            else None
        )
        if tokens is None:
            logger.warning(
                "Web picture upload refused: cookies=%d origin_ok=%s session=%s",
                len(cookies),
                origins == [self._store.public_origin],
                sid is not None,
            )
            response = JSONResponse(
                {"code": "unauthenticated", "message": "Pair with your Mac again."},
                status_code=401,
                headers={"Cache-Control": "private, no-store"},
            )
            return await response(scope, receive, send)
        scope = {
            **scope,
            "headers": [*headers, (b"authorization", b"Bearer " + tokens[0].encode())],
        }
        return await self._app(scope, receive, send)


class SessionCookieRenewalMiddleware:
    """Re-issue the session cookie after the server extended the session, so the browser's
    Max-Age follows the sliding lifetime. Reads memory only and never grants access."""

    def __init__(self, app: Any, store: WebSessionStore) -> None:
        self._app, self._store = app, store

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> Any:
        if scope["type"] != "http" or scope.get("method") != "GET":
            return await self._app(scope, receive, send)
        cookies = [
            v.decode("latin1") for k, v in scope.get("headers", []) if k.lower() == b"cookie"
        ]
        renewal = (
            self._store.cookie_renewal(_cookies({"cookie": cookies[0]}).get(COOKIE_NAME, ""))
            if len(cookies) == 1
            else None
        )
        if renewal is None:
            return await self._app(scope, receive, send)

        async def renewed(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start" and message.get("status", 200) < 400:
                headers = list(message.get("headers", []))
                if not any(k.lower() == b"set-cookie" for k, _ in headers):
                    headers.append((b"set-cookie", renewal["Set-Cookie"].encode("latin1")))
                    message = {**message, "headers": headers}
            await send(message)

        return await self._app(scope, receive, renewed)


class FletGuardMiddleware:
    """Pure-ASGI guard: Origin, cookie auth, reconnect ownership (OWASP WS)."""

    def __init__(self, app: Any, store: WebSessionStore, ws_path: str = WS_PATH) -> None:
        self._app = app
        self._store = store
        self._ws_path = ws_path
        self._transports: dict[str, _FletTransport] = {}
        self._handoffs: dict[str, _FletTransport] = {}

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> Any:
        if scope["type"] == "websocket" and scope.get("path") == self._ws_path:
            return await self._guard_ws(scope, receive, send)
        if (
            scope["type"] == "http"
            and scope.get("path") == "/v1/auth/logout"
            and scope.get("method") == "POST"
        ):
            headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
            if headers.get("origin", "") != self._store.public_origin:
                body = json.dumps({"code": "forbidden", "message": "bad origin"}).encode()
                await send(
                    {
                        "type": "http.response.start",
                        "status": 403,
                        "headers": [(b"content-type", b"application/json")],
                    }
                )
                await send({"type": "http.response.body", "body": body})
                return None
        return await self._app(scope, receive, send)

    async def _guard_ws(self, scope: dict[str, Any], receive: Any, send: Any) -> Any:
        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        origins = [v.decode() for k, v in scope.get("headers", []) if k.lower() == b"origin"]
        if origins != [self._store.public_origin]:
            logger.info("ws_guard_refused branch=origin code=1008")
            await send({"type": "websocket.close", "code": 1008, "reason": "forbidden"})
            return None
        handshake = _Handshake(
            ip=self._client_ip(scope),
            user_agent=headers.get("user-agent", ""),
            page_name=scope.get("path", "").rsplit("/", 1)[0].lstrip("/"),
        )
        closed = False
        closed_event = asyncio.Event()

        async def closer(reason: str = "logout", code: int = 1008, branch: str = "store") -> None:
            nonlocal closed
            if not closed:
                closed = True
                # why a socket was refused, without cookies, ids or IPs.
                logger.info("ws_guard_refused branch=%s code=%s", branch, code)
                closed_event.set()  # Wake a half-open downstream receive before sending close.
                try:
                    await send({"type": "websocket.close", "code": code, "reason": reason})
                except (OSError, RuntimeError):
                    pass  # The peer may have already closed; downstream still must finish.

        # Validate REGISTER before creating a FletApp. The pinned FletApp
        # starts its send-loop before registration; rejection inside that
        # loop leaves it waiting without a session to tear down.
        connect = await receive()
        if connect["type"] != "websocket.connect":
            return None
        await send({"type": "websocket.accept"})
        try:
            first = await asyncio.wait_for(receive(), REGISTER_TIMEOUT_S)
        except TimeoutError:
            await closer("forbidden", branch="register_timeout")
            return None
        if first["type"] == "websocket.disconnect":
            return None
        frame = first.get("bytes") or b""
        is_register, session_id = (
            parse_register(frame) if len(frame) <= MAX_REGISTER_BYTES else (False, "")
        )
        if first["type"] != "websocket.receive" or not is_register:
            await closer("forbidden", branch="not_register")
            return None
        sid = await self._store.run_private(
            lambda: self._store.verify_cookie(_cookies(headers).get(COOKIE_NAME, ""))
        )
        browser = self._store.browser_identity(_cookies(headers).get("rd_browser", ""))
        owner = self._store.owner_of(session_id) if session_id else None
        held_browser = self._store.browser_pages.get(session_id)
        if owner is None and held_browser is not None and browser != held_browser:
            await closer(RECOVERY_REASON, branch="browser_mismatch")
            return None
        if owner is not None and (
            owner != sid or await self._store.run_private(lambda: self._store.identity(sid)) is None
        ):
            await closer(RECOVERY_REASON, branch="owner_mismatch")
            return None
        transport = _FletTransport(closer, closed_event)
        canonical = self._store._handshakes.get(session_id) if owner is not None else None
        if owner is not None:
            if (
                canonical is None
                or canonical.user_agent != handshake.user_agent
                or canonical.page_name != handshake.page_name
                or session_id in self._handoffs
            ):
                await closer(RECOVERY_REASON, branch="handshake_mismatch")
                return None
            # Claim before any await. Concurrent registrations never close a successor.
            self._handoffs[session_id] = transport
            scope = {
                **scope,
                "relaydeck.actual_client": scope.get("client"),
                "client": (canonical.ip, (scope.get("client") or ("", 0))[1]),
            }
            handshake = canonical
        self._store.add_closer(sid, closer)
        initial = iter((connect, first))

        def transport_current() -> bool:
            """Loop-local fence; no await between this check and publication."""
            if closed or closed_event.is_set() or transport.done.is_set():
                return False
            if transport.flet_id and self._transports.get(transport.flet_id) is not transport:
                return False
            if owner is not None:
                if (
                    self._store.owner_of(session_id) != owner
                    or self._store._handshakes.get(session_id) != canonical
                ):
                    return False
                if not transport.flet_id:
                    return self._handoffs.get(session_id) is transport
                return self._handoffs.get(session_id) in (None, transport)
            return True

        def authority_current(identity: WebIdentity | None) -> bool:
            # A completed private read is not authority to undo a concurrent
            # logout/revocation. Recheck its local denial fences without I/O.
            if sid is None:
                return identity is None
            held = self._store._sessions.get(sid)
            if (
                identity is None
                or identity.sid != sid
                or sid in self._store._denied_sids
                or held is None
                or held.identity != identity
                or held.expires_ms <= self._store.now_ms()
            ):
                return False
            authority = self._store.gateway_sessions
            if authority is not None:
                scope_key = (self._store.public_origin, identity.account, identity.device_id)
                if (
                    authority.verifier(held.access_token) in authority._denied
                    or authority.verifier(held.refresh_token) in authority._denied
                    or any(
                        scope_key[: len(blocked)] == blocked
                        for blocked in tuple(authority._denied_scopes)
                    )
                ):
                    return False
            return True

        async def guarded_receive() -> Any:
            nonlocal closed
            if closed:
                return {"type": "websocket.disconnect", "code": 1008}
            queued = next(initial, None)
            if queued is not None:
                return queued
            reader = asyncio.create_task(receive())
            shutdown = asyncio.create_task(closed_event.wait())
            try:
                await asyncio.wait((reader, shutdown), return_when=asyncio.FIRST_COMPLETED)
                if closed:
                    return {"type": "websocket.disconnect", "code": 1008}
                message = reader.result()
            finally:
                reader.cancel()
                shutdown.cancel()
                # Not gather(): if this task is cancelled meanwhile, gather raises a fresh
                # CancelledError without the canceller's message, so an enclosing cancel scope
                # no longer recognises it as its own and the cancellation escapes the scope.
                await asyncio.wait((reader, shutdown))
                for task in (reader, shutdown):
                    if not task.cancelled():
                        task.exception()  # retrieved here; a failed read is reported above
            if closed:
                return {"type": "websocket.disconnect", "code": 1008}
            if message["type"] == "websocket.disconnect":
                closed = True
                closed_event.set()
            if message["type"] == "websocket.receive":
                if _register_attempt(message.get("bytes") or b""):
                    # A second registration cannot retarget this socket.
                    await closer("forbidden", branch="second_register")
                    return {"type": "websocket.disconnect", "code": 1008}
            return message

        async def guarded_send(message: Any) -> None:
            nonlocal closed
            # Only swallow Flet's duplicate accept. It needs the original
            # ASGI connect message even though this guard accepted already.
            if message["type"] != "websocket.accept" and transport_current():
                registered, registered_id = parse_register(message.get("bytes") or b"")
                if registered:
                    identity = (
                        await self._store.run_private(lambda: self._store.identity(sid))
                        if sid is not None
                        else None
                    )
                    if (
                        not registered_id
                        or (owner is not None and registered_id != session_id)
                        or not transport_current()
                        or not authority_current(identity)
                        or (owner is not None and self._store.owner_of(registered_id) != sid)
                        or self._store.owner_of(registered_id) not in (None, sid)
                        or self._transports.get(registered_id) not in (None, transport)
                        or self._handoffs.get(registered_id) not in (None, transport)
                    ):
                        await closer(RECOVERY_REASON, branch="register_identity")
                        return
                    transport.flet_id = registered_id
                    self._transports[registered_id] = transport
                    if self._handoffs.get(session_id) is transport:
                        del self._handoffs[session_id]
                if message["type"] == "websocket.close":
                    closed = True
                    closed_event.set()
                try:
                    await send(message)
                except (OSError, RuntimeError):
                    # a failed downstream send means the peer is gone
                    # (uvicorn raises ClientDisconnected(OSError); starlette raises
                    # RuntimeError after a close). Wake the half-open receive so the app
                    # exits and the cleanup runs; re-raise unchanged.
                    closed = True
                    closed_event.set()
                    raise

        try:
            if owner is not None:
                from flet.fastapi import app_manager

                old = self._transports.get(session_id)
                if old is not None:
                    try:
                        async with asyncio.timeout(FLET_HANDOFF_TIMEOUT_S):
                            await old.close("transport_replaced", 1000)
                            # Flet disconnects unconditionally at the END of handle(),
                            # so awaiting only the close frame would kill the new socket.
                            await old.done.wait()
                    except TimeoutError:
                        await closer(RECOVERY_REASON, branch="handoff_timeout")
                        return None
                unique = self._store.unique_key(session_id)
                candidate = await app_manager.get_session(unique) if unique is not None else None
                verified_sid = await self._store.run_private(
                    lambda: self._store.verify_cookie(_cookies(headers).get(COOKIE_NAME, ""))
                )
                held = self._store._sessions.get(sid) if sid is not None else None
                if (
                    not transport_current()
                    or verified_sid != sid
                    or not authority_current(held.identity if held is not None else None)
                ):
                    await closer(RECOVERY_REASON, branch="handoff_identity")
                    return None
                if candidate is None:
                    await closer(RECOVERY_REASON, branch="candidate_missing")
                    return None
                if candidate.connection is not None:
                    await closer(RECOVERY_REASON, branch="candidate_attached")
                    return None
            token = _current_handshake.set((sid, handshake))
            browser_token = _current_browser.set(browser)
            transport_token = _current_transport.set(transport)
            requested_token = _requested_flet.set(session_id)
            try:
                return await self._app(scope, guarded_receive, guarded_send)
            finally:
                try:
                    # Flet 1.0.3 skips detachment when its sender raises. Only
                    # this registered socket may finish that teardown; a valid
                    # handoff waits for done before attaching its successor.
                    if transport.flet_id and self._transports.get(transport.flet_id) is transport:
                        closed = True
                        closed_event.set()
                        from flet.fastapi import app_manager

                        unique = self._store.unique_key(transport.flet_id)
                        session = (
                            await app_manager.get_session(unique) if unique is not None else None
                        )
                        if (
                            unique is not None
                            and self._transports.get(transport.flet_id) is transport
                            and session is not None
                            and session.connection is not None
                        ):
                            await app_manager.disconnect_session(unique, _flet_session_timeout())
                            logger.info(
                                "flet_stale_connection_detached flet=%s", transport.flet_id[:8]
                            )
                except Exception:
                    logger.warning("flet_stale_connection_cleanup_failed", exc_info=True)
                finally:
                    _current_handshake.reset(token)
                    _current_browser.reset(browser_token)
                    _current_transport.reset(transport_token)
                    _requested_flet.reset(requested_token)
        finally:
            self._store.remove_closer(sid, closer)
            if self._handoffs.get(session_id) is transport:
                del self._handoffs[session_id]
            if self._transports.get(transport.flet_id) is transport:
                del self._transports[transport.flet_id]
            transport.done.set()

    @staticmethod
    def _client_ip(scope: dict[str, Any]) -> str:
        client = scope.get("client") or ("", 0)
        ip = client[0]
        return "" if ip in ("127.0.0.1", "::1") else ip


def identity_for_page(store: WebSessionStore, page: Any) -> WebIdentity | None:
    """App-code helper: gateway identity behind a Flet page, if authed."""
    try:
        flet_id = page.session.id
    except RuntimeError:
        return None
    return store.identity(store.owner_of(flet_id))


async def teardown_web_sid(
    store: WebSessionStore,
    gateway_state: GatewayState,
    sid: str,
    *,
    keep_flet_ids: frozenset[str] = frozenset(),
) -> None:
    """Shared logout teardown: revoke gateway tokens, sever device
    sockets, delete this sid's Flet sessions (except kept ones), close
    per-sid resources, retire the sid. POST /v1/auth/logout keeps
    nothing; the in-app button keeps the live page so it can flip to
    the login branch."""
    import flet.fastapi as flet_fastapi

    if store.gateway_sessions is None:
        store.gateway_sessions = gateway_state.sessions
    identity = store.begin_revocation(sid)
    flet_ids = store.flet_ids(sid)
    committed = False
    try:
        identity = await store.run_private(lambda: store.revoke_sid(sid)) or identity
        committed = True
    finally:
        # Even unavailable/failed storage must leave no local writers. Retry
        # metadata is separate from both access authorization and live pages.
        pending = store._pending_revocations.get(sid)
        identity = identity or (pending.identity if pending is not None else None)
        if identity is not None:
            gateway_state.metadata.revoke(account=identity.account, device_id=identity.device_id)
            gateway_state.backend.revoke_leases_device(identity.account, identity.device_id)
            await gateway_state.sockets.close_device(
                identity.account, identity.device_id, "unauthenticated"
            )
            await push_io(
                gateway_state.push,
                "revoke_device",
                identity.account,
                identity.device_id,
                store.now_ms(),
            )
        await store.close_all(sid)
        for flet_id in flet_ids:
            if flet_id in keep_flet_ids:
                continue
            unique = store.unique_key(flet_id)
            if unique is not None:
                try:
                    await flet_fastapi.app_manager.delete_session(unique)
                except Exception:  # noqa: BLE001 - teardown is best-effort
                    pass
        store.retire_local(sid)
    if committed and identity is not None and store.command_store is not None:
        from relaydeck.client.pending_commands import PendingCommands

        key = PendingCommands(
            store.command_store, store.public_origin, identity.account, identity.device_id
        ).key
        try:
            await store.command_store.delete(key)
        except Exception:
            store.command_cleanup_error = "pending_command_cleanup_unavailable"


def build_web_app(
    *,
    gateway_state: GatewayState,
    gateway_app: FastAPI,
    flet_main: Any,
    public_origin: str,
    cookie_secret: bytes,
    before_main: Any = None,
    store: WebSessionStore | None = None,
    dev_login: bool = False,
) -> tuple[FastAPI, WebSessionStore]:
    """One origin: auth routes + gateway API + Flet app (mounted last)."""
    import flet.fastapi as flet_fastapi

    if store is None:
        store = WebSessionStore(public_origin=public_origin, cookie_secret=cookie_secret)
    if store.persistence is not None:
        from relaydeck.gateway.app import durable_session_allowed

        store.gateway_sessions = gateway_state.sessions
        store.session_allowed = lambda session: durable_session_allowed(gateway_state, session)
    guard_before_main = make_before_main(store)
    if before_main is None:
        before_main = guard_before_main
    else:
        user_before_main = before_main

        async def chained_before_main(page: Any) -> None:
            await guard_before_main(page)
            if callable(user_before_main):
                result = user_before_main(page)
                if hasattr(result, "__await__"):
                    await result

        before_main = chained_before_main

    @asynccontextmanager
    async def lifespan(app: FastAPI):  # type: ignore[no-untyped-def]
        # Copying gateway routes does not carry its lifespan. Keep the
        # revoke consumer and Flet manager under the same cleanup boundary.
        async with gateway_app.router.lifespan_context(gateway_app):
            await flet_fastapi.app_manager.start()
            try:
                yield
            finally:
                await flet_fastapi.app_manager.shutdown()

    app = FastAPI(lifespan=lifespan)
    app.state.rd_store = store
    app.state.rd_gateway_state = gateway_state

    @app.exception_handler(StorageUnavailable)
    async def storage_unavailable(request: Request, exc: StorageUnavailable) -> JSONResponse:
        from relaydeck.gateway.app import error_response

        return error_response(
            "unauthenticated",
            "Protected sign-in storage unavailable",
            status=503,
            details={"reason": "storage_unavailable", "storage_reason": exc.code},
        )

    @app.get(RECOVERY_PATH)
    async def auth_recovery_script() -> Response:
        return Response(
            RECOVERY_SCRIPT,
            media_type="application/javascript",
            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
        )

    @app.post("/v1/auth/pairing/browser")
    async def pairing_browser(request: Request) -> Any:
        if request.headers.getlist("origin") != [store.public_origin]:
            return JSONResponse(status_code=403, content={"code": "forbidden"})
        cookies = _cookies(dict(request.headers))
        if store.browser_identity(cookies.get("rd_browser", "")):
            return Response(status_code=204)
        try:
            value = store.issue_browser_cookie()
        except ValueError:
            return Response(status_code=429)
        # Existing authenticated pages retain their live Flet parent/drafts.
        authed = (
            await store.run_private(lambda: store.verify_cookie(cookies.get(COOKIE_NAME, "")))
            is not None
        )
        return Response(
            status_code=204 if authed else 201,
            headers={"Set-Cookie": value, "Cache-Control": "no-store"},
        )

    @app.get("/v1/auth/session/recovery")
    async def session_recovery_page() -> Response:
        return Response(
            '<!doctype html><html><head><meta name="viewport" '
            'content="width=device-width, initial-scale=1">'
            "<title>RelayDeck connection recovery</title>"
            f'<script src="{RECOVERY_PATH}"></script></head><body></body></html>',
            media_type="text/html",
            headers={"Cache-Control": "no-store"},
        )

    @app.post("/v1/auth/session/recovery")
    async def session_recovery(request: Request) -> Response:
        """Explicit fresh-page transition; never resurrect a revoked controller."""
        import httpx

        if request.headers.getlist("origin") != [store.public_origin]:
            return Response(status_code=403)
        sid = await store.run_private(
            lambda: store.verify_cookie(_cookies(dict(request.headers)).get(COOKIE_NAME, ""))
        )
        identity = await store.run_private(lambda: store.identity(sid))
        pair = await store.run_private(lambda: store.take_session(sid)) if sid is not None else None
        if identity is not None and pair is not None:
            try:
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=gateway_app), base_url="http://gw"
                ) as http:
                    response = await http.get(
                        "/v1/me", headers={"Authorization": f"Bearer {pair[0]}"}
                    )
                if response.status_code == 200:
                    principal = response.json()
                    validate("relaydeck/gateway/v1/me", principal)
                    if (principal["account"], principal["device_id"]) == (
                        identity.account,
                        identity.device_id,
                    ):
                        return Response(status_code=204, headers={"Cache-Control": "no-store"})
                elif response.status_code not in (401, 403):
                    return Response(status_code=503, headers={"Cache-Control": "no-store"})
            except Exception:
                return Response(status_code=503, headers={"Cache-Control": "no-store"})
        if sid is not None:
            await teardown_web_sid(store, gateway_state, sid)
        return Response(
            status_code=401, headers={**store.clear_cookie_headers(), "Cache-Control": "no-store"}
        )

    @app.post("/v1/auth/session/check")
    async def session_check(request: Request) -> Response:
        """Read-only: is this browser signed in right now. Never tears down or clears."""
        import httpx

        headers = {"Cache-Control": "no-store"}
        if request.headers.getlist("origin") != [store.public_origin]:
            return Response(status_code=403, headers=headers)
        try:
            seen = await store.run_private(
                lambda: store.observe(_cookies(dict(request.headers)).get(COOKIE_NAME, ""))
            )
        except StorageUnavailable:
            return Response(status_code=503, headers=headers)
        if seen is None:
            return Response(status_code=401, headers=headers)
        identity, access = seen
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=gateway_app), base_url="http://gw"
            ) as http:
                response = await http.get("/v1/me", headers={"Authorization": f"Bearer {access}"})
            if response.status_code in (401, 403):
                return Response(status_code=401, headers=headers)
            if response.status_code != 200:
                return Response(status_code=503, headers=headers)
            principal = response.json()
            validate("relaydeck/gateway/v1/me", principal)
        except Exception:  # noqa: BLE001 - authority could not be established right now
            return Response(status_code=503, headers=headers)
        if (principal["account"], principal["device_id"]) != (
            identity.account,
            identity.device_id,
        ):
            return Response(status_code=401, headers=headers)
        return Response(status_code=204, headers=headers)

    @app.post("/v1/ui/visibility")
    async def ui_visibility(request: Request) -> Response:
        response = Response(status_code=204, headers={"Cache-Control": "no-store"})
        if request.headers.getlist("origin") != [store.public_origin]:
            return response
        raw = bytearray()
        try:
            async for part in request.stream():
                if len(raw) + len(part) > MAX_VISIBILITY_BYTES:
                    return response
                raw.extend(part)

            def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
                result: dict[str, Any] = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError
                    result[key] = value
                return result

            body = json.loads(raw, object_pairs_hook=unique)
            if (
                not isinstance(body, dict)
                or set(body) != {"state", "persisted", "flet"}
                or body["state"] not in ("visible", "hidden")
                or type(body["persisted"]) is not bool
                or not isinstance(body["flet"], str)
            ):
                return response
            cookie = _cookies(dict(request.headers)).get(COOKIE_NAME, "")
            seen = await store.run_private(lambda: store.observe(cookie))
        except (ValueError, StorageUnavailable):
            return response
        if seen is None:
            return response
        identity, _access = seen
        flet_id = body["flet"]
        binding = store._visibility_pages.get(flet_id)
        unique_key = store.unique_key(flet_id)
        if binding is None or binding.identity != identity or unique_key is None:
            return response
        try:
            current = await store.run_private(lambda: store.observe(cookie))
        except StorageUnavailable:
            return response
        live = await flet_fastapi.app_manager.get_session(unique_key)
        if (
            current is None
            or current[0] != identity
            or store.owner_of(flet_id) != identity.sid
            or store._visibility_pages.get(flet_id) is not binding
            or live is not binding.session
            or store.unique_key(flet_id) != unique_key
        ):
            return response
        now = store._visibility_clock()
        if now - binding.since >= 1.0:
            binding.since, binding.count = now, 0
        if binding.count < 4:
            binding.count += 1
            binding.report(body["state"], body["persisted"])
        return response

    @app.get("/v1/auth/pairing/finish")
    async def pairing_finish() -> Response:
        # No credential/id interpolated into HTML or URL. Existing per-tab Flet
        # ID plus the HttpOnly browser cookie select the server-held attempt.
        return Response(
            '<!doctype html><meta name="viewport" content="width=device-width">'
            '<p id="status">Opening protected Console…</p>'
            '<script src="/v1/auth/pairing/finish.js"></script>',
            media_type="text/html",
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/v1/auth/pairing/finish.js")
    async def pairing_finish_script() -> Response:
        return Response(
            """(() => {
 const status = document.getElementById('status');
 const drop = () => {try {window.sessionStorage.removeItem('_flet_session_id');} catch (_) {}};
 const link = () => {const a=document.createElement('a');a.href='/';
   a.textContent='Return to Connect';document.body.append(a);};
 const refused = () => {drop();
   status.textContent='Connection expired or changed. '
     +'Return to RelayDeck and explicitly pair again.';
   link();};
 const retryable = () => {
   status.textContent="Couldn't reach RelayDeck. Check the connection and try again.";
   const b=document.createElement('button');b.textContent='Retry';
   b.onclick=() => {b.remove();status.textContent='Opening protected Console…';run();};
   document.body.append(b);};
 const home = () => {drop(); window.location.replace('/');};
 const run = async () => {
  try {
   const id = window.sessionStorage.getItem('_flet_session_id');
   const r = await fetch('/v1/auth/pairing/complete', {
     method:'POST',credentials:'same-origin',cache:'no-store',
     headers:{'Content-Type':'application/json'},body:JSON.stringify({flet_session_id:id})});
   if (r.ok) return home();
   if (r.status !== 404 && r.status !== 403) return retryable();
   // Another completer of this attempt may have finished first: signed in is success.
   const s = await fetch('/v1/auth/session/check', {
     method:'POST',credentials:'same-origin',cache:'no-store'});
   if (s.status === 204) return home();
   if (s.status === 401) return refused();
   retryable();
  } catch (_) {retryable();}
 };
 run();})();""",
            media_type="application/javascript",
            headers={"Cache-Control": "no-store"},
        )

    @app.post("/v1/auth/pairing/complete")
    async def pairing_complete(request: Request) -> Any:
        if request.headers.getlist("origin") != [store.public_origin]:
            return JSONResponse(status_code=403, content={"code": "forbidden"})
        try:
            raw = await request.body()
            if len(raw) > 512:
                raise ValueError

            def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
                result: dict[str, Any] = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError
                    result[key] = value
                return result

            body = json.loads(raw, object_pairs_hook=unique)
            if (
                not isinstance(body, dict)
                or set(body) != {"flet_session_id"}
                or not isinstance(body["flet_session_id"], str)
            ):
                raise ValueError
            flet_id = body["flet_session_id"]
        except ValueError:
            return JSONResponse(status_code=400, content={"code": "forbidden"})
        browser = store.browser_identity(_cookies(dict(request.headers)).get("rd_browser", ""))
        if not browser:
            return JSONResponse(status_code=404, content={"code": "not_found"})
        # Join the exact operation still running for this caller, even after its held result
        # was consumed; an authenticated page never reaches another page's attempt.
        cookie_sid = await store.run_private(
            lambda: store.verify_cookie(_cookies(dict(request.headers)).get(COOKIE_NAME, ""))
        )
        authenticated = (
            store.identity(store.owner_of(flet_id)) is not None
            or await store.run_private(lambda: store.identity(cookie_sid)) is not None
        )
        selected = _select_attempt(browser, flet_id, authenticated)
        if selected is None:
            return JSONResponse(status_code=404, content={"code": "not_found"})
        if isinstance(selected, asyncio.Future):
            running = selected
        else:
            page_id, held = selected
            # Callers of the same attempt share one completion; another attempt never
            # shares work or a cookie.
            key = (browser, page_id, held[0])
            running = asyncio.ensure_future(_complete_pairing(browser, page_id, held))
            store.pairing_tasks[key] = running
            running.add_done_callback(lambda _t: store.pairing_tasks.pop(key, None))
        status, content, sid = await asyncio.shield(running)
        headers = {"Cache-Control": "no-store"}
        if sid is not None:
            headers.update(store.cookie_headers(sid))
        return JSONResponse(content, status_code=status, headers=headers)

    def _select_attempt(browser: str, flet_id: str, authenticated: bool) -> Any:
        """One resolver: a running task or a held attempt, exact and never ambiguous.

        The page's current flow generation decides; older generations are never used. A
        page that is not signed in may take over another page's attempt only when exactly
        one candidate exists in this browser.
        """

        def current(page_id: str) -> int | None:
            flow = store.pairing_flows.get(page_id)
            return flow.generation if flow is not None else None

        def candidates(page_id: str) -> list[Any]:
            if store.browser_pages.get(page_id) != browser:
                return []
            generation = current(page_id)
            if generation is None:
                return []
            found: list[Any] = []
            # Same generation means the same attempt: a running task is joined before its
            # held result (still present until the authority check) can start a second one.
            task = store.pairing_tasks.get((browser, page_id, generation))
            if task is not None and not task.done():
                found.append(task)
            held = store.pairing_ready.get(page_id)
            if held is not None and held[0] == generation and held[3] > store.now_ms():
                found.append((page_id, held))
            # One attempt per page and generation, counted once (task first).
            return found[:1]

        own = candidates(flet_id)
        if own:
            return own[0]
        if authenticated:
            return None
        others = [
            item
            for page_id, holder in tuple(store.browser_pages.items())
            if holder == browser and page_id != flet_id
            for item in candidates(page_id)
        ]
        return others[0] if len(others) == 1 else None

    async def _complete_pairing(
        browser: str, flet_id: str, held: tuple[int, dict[str, Any], dict[str, Any], int]
    ) -> tuple[int, dict[str, Any], str | None]:
        import httpx

        flow = store.pairing_flows.get(flet_id)
        if flow is None or store.browser_pages.get(flet_id) != browser:
            return 404, {"code": "not_found"}, None
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=gateway_app), base_url="http://gw"
        ) as http:
            me = await http.get(
                "/v1/me", headers={"Authorization": f"Bearer {held[1]['access_token']}"}
            )
        try:
            principal = me.json()
            validate("relaydeck/gateway/v1/me", principal)
        except (ValueError, TypeError):
            return 403, {"code": "unauthenticated"}, None
        if (
            me.status_code != 200
            or principal != held[2]
            or flow.generation != held[0]
            or held[3] <= store.now_ms()
            or store.pairing_ready.get(flet_id) is not held
        ):
            return 403, {"code": "unauthenticated"}, None
        # Consumed only after the authority check passed.
        store.pairing_ready.pop(flet_id, None)
        tokens = held[1]
        sid = await store.run_private(
            lambda: store.create_session(
                principal["account"],
                principal["device_id"],
                tokens["access_token"],
                tokens["refresh_token"],
                store.now_ms() + tokens["expires_in"] * 1000,
            )
        )
        return 200, {"ok": True}, sid

    if dev_login:
        app.get("/v1/auth/dev-login")(_dev_login)

    @app.get("/v1/auth/callback")
    async def auth_callback(request: Request) -> Any:
        import httpx

        state = request.query_params.get("state", "")
        code = request.query_params.get("code", "")
        verifier = store.take_pending(state)
        if not verifier or not code:
            return JSONResponse(
                status_code=400,
                content={"code": "forbidden", "message": "bad login attempt"},
            )
        transport = httpx.ASGITransport(app=gateway_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://gw") as http:
            token_response = await http.post(
                "/v1/token",
                json={"grant": "authorization_code", "code": code, "verifier": verifier},
            )
            if token_response.status_code != 200:
                return JSONResponse(
                    status_code=token_response.status_code, content=token_response.json()
                )
            tokens = token_response.json()
            me = await http.get(
                "/v1/me", headers={"Authorization": f"Bearer {tokens['access_token']}"}
            )
            if me.status_code != 200:
                return JSONResponse(status_code=502, content={"code": "upstream_incompatible"})
        principal = me.json()
        sid = await store.run_private(
            lambda: store.create_session(
                principal["account"],
                principal["device_id"],
                tokens["access_token"],
                tokens["refresh_token"],
                store.now_ms() + tokens.get("expires_in", 900) * 1000,
            )
        )
        return RedirectResponse(url="/", status_code=302, headers=store.cookie_headers(sid))

    @app.post("/v1/auth/logout")
    async def auth_logout(request: Request) -> Any:
        sid = store.signed_sid(
            _cookies({k.lower(): v for k, v in request.headers.items()}).get(COOKIE_NAME, "")
        )
        if sid is not None:
            await teardown_web_sid(store, gateway_state, sid)
        return Response(status_code=204, headers=store.clear_cookie_headers())

    for route in gateway_app.routes:
        app.routes.append(route)
    from relaydeck.client.asset_paths import CLIENT_ASSETS

    app.mount(
        "/",
        flet_fastapi.app(
            flet_main,
            before_main=before_main,
            assets_dir=str(CLIENT_ASSETS),
            session_timeout_seconds=_flet_session_timeout(),
        ),
    )
    app.add_middleware(SessionCookieRenewalMiddleware, store=store)  # type: ignore[arg-type]
    app.add_middleware(FletGuardMiddleware, store=store)  # type: ignore[arg-type]
    app.add_middleware(PictureUploadMiddleware, store=store)  # type: ignore[arg-type]
    app.add_middleware(BrowserRecoveryMiddleware)  # type: ignore[arg-type]
    app.add_middleware(BodyCapMiddleware)  # type: ignore[arg-type]
    return app, store


# ---------------------------------------------------------------------------
# Production web entry (wiring + local runner)
# ---------------------------------------------------------------------------


def web_pairing_flow(store: WebSessionStore, flet_id: str, gateway_app: FastAPI) -> Any:
    """One server-held attempt per cookie-bound anonymous Flet page; no SSRF."""
    from relaydeck.client.features.pairing.memory import MemorySecrets
    from relaydeck.client.features.pairing.service import PairingFlow

    browser = store.browser_pages.get(flet_id)
    if not browser or store.browsers.get(browser, 0) <= store.now_ms():
        return None
    for page_id, holder in tuple(store.browser_pages.items()):
        if store.browsers.get(holder, 0) <= store.now_ms():
            store.browser_pages.pop(page_id, None)
            old = store.pairing_flows.pop(page_id, None)
            if old is not None:
                old.invalidate()
            store.pairing_ready.pop(page_id, None)
    if flet_id in store.pairing_flows:
        return store.pairing_flows[flet_id]
    if len(store.pairing_flows) >= 256:
        return None

    async def request(method: str, url: str, body: Any = None, headers: Any = None) -> Any:
        import httpx

        if not url.startswith(store.public_origin + "/"):
            raise ValueError("Only the configured companion can be contacted")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=gateway_app), base_url="http://gw"
        ) as http:
            response = await http.request(method, urlparse(url).path, json=body, headers=headers)
        return response.status_code, response.json()

    async def accept(tokens: dict[str, Any], principal: dict[str, Any], current: Any) -> bool:
        if (
            not current()
            or store.browser_pages.get(flet_id) != browser
            or store.browsers.get(browser, 0) <= store.now_ms()
        ):
            return False
        # Five minutes: long enough to switch apps on a phone and come back
        # (the reload then finishes the handoff via the boot script).
        store.pairing_ready[flet_id] = (
            flow.generation,
            tokens,
            principal,
            store.now_ms() + 300_000,
        )
        return True

    flow = PairingFlow(
        MemorySecrets(),
        lambda url, body: request("POST", url, body),
        lambda url, headers: request("GET", url, headers=headers),
        accept,
        now_ms=store.now_ms,
    )
    store.pairing_flows[flet_id] = flow
    return flow


def make_relaydeck_main(
    *,
    store: WebSessionStore,
    base_url: str,
    gateway_app: FastAPI,
    gateway_state: GatewayState,
    host_id: str,
    setup_report: Callable[[], tuple[tuple[str, str], ...]] | None = None,
    draft_store: Any = None,
) -> Any:
    """Production Flet main: authenticated users get Console, others get Connect."""

    def flet_main(page: Any, verified_initial: tuple[WebIdentity | None] | None = None) -> None:
        import flet as ft

        from relaydeck.client.app import run_app
        from relaydeck.client.components.appearance import configure_page_theme
        from relaydeck.client.components.design_system.primitives import DottedGround
        from relaydeck.client.components.setup import SetupPanel
        from relaydeck.client.features.pairing.components import PairingPanel

        login_appearance: list[Any] = [None]

        @ft.component
        def LoginPrompt() -> Any:
            flow = web_pairing_flow(store, page.session.id, gateway_app)
            launcher: Any = ft.use_ref(None)
            if launcher.current is None:
                launcher.current = ft.UrlLauncher()

            async def open_finish(snapshot: Any) -> None:
                from dataclasses import replace

                generation = flow.generation
                try:
                    if not await launcher.current.launch_url(
                        store.public_origin + "/v1/auth/pairing/finish",
                        web_only_window_name="_self",
                    ):
                        raise RuntimeError("Navigation unavailable")
                except Exception:  # noqa: BLE001 - no credentials reflected in recovery
                    if flow.generation == generation:
                        flow.emit(
                            replace(
                                snapshot,
                                phase="failed",
                                busy=False,
                                message=("Return to RelayDeck to finish pairing, or pair again."),
                            )
                        )

            def navigate(snapshot: Any) -> None:
                if snapshot.phase == "ready":
                    asyncio.create_task(open_finish(snapshot))

            def effect() -> Any:
                context = copy_context()
                return (
                    flow.subscribe(lambda snapshot: context.run(navigate, snapshot))
                    if flow is not None
                    else None
                )

            unsubscribe: Any = ft.use_ref(None)

            def subscribe() -> None:
                unsubscribe.current = effect()

            def cleanup() -> None:
                if unsubscribe.current is not None:
                    unsubscribe.current()

            ft.use_effect(subscribe, dependencies=[flow], cleanup=cleanup)
            return DottedGround(
                ft.SafeArea(
                    expand=True,
                    content=ft.Column(
                        expand=True,
                        scroll=ft.ScrollMode.AUTO,
                        horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
                        controls=[
                            PairingPanel(
                                flow,
                                endpoint=store.public_origin,
                                host_id=host_id,
                                label="My browser",
                            )
                            if flow is not None
                            else ft.Column(
                                controls=[
                                    ft.Text(
                                        "Reload to establish a protected Connect attempt.",
                                        key="login-hint",
                                    ),
                                    ft.Button(
                                        "Reload Connect",
                                        key="connect-reload",
                                        height=48,
                                        on_click=lambda e: asyncio.create_task(
                                            launcher.current.launch_url(
                                                store.public_origin + "/",
                                                web_only_window_name="_self",
                                            )
                                        ),
                                    ),
                                ]
                            ),
                            *(
                                [SetupPanel(report=setup_report, appearance=login_appearance[0])]
                                if setup_report
                                else []
                            ),
                        ],
                    ),
                )
            )

        async def refresher(refresh_token: str) -> Any:
            import httpx

            transport = httpx.ASGITransport(app=gateway_app)
            async with httpx.AsyncClient(transport=transport, base_url="http://gw") as http:
                response = await http.post(
                    "/v1/token",
                    json={"grant": "refresh_token", "refresh_token": refresh_token},
                )
                if response.status_code != 200:
                    return None
                doc = response.json()
                return (
                    doc["access_token"],
                    doc["refresh_token"],
                    doc.get("expires_in", 900),
                )

        async def get_token() -> str:
            # Resolve the owner on every call: a reattached session may
            # have rebound to a different sid since this page was created.
            current = await store.run_private(lambda: identity_for_page(store, page))
            if current is None:
                return ""
            token = await store.ensure_access(current.sid, refresher)
            return token or ""

        async def refresh_identity() -> bool:
            current = await store.run_private(lambda: identity_for_page(store, page))
            if current is None:
                return False
            pair = await store.run_private(lambda: store.take_session(current.sid))
            if pair is not None:
                principal = await store.run_private(
                    lambda: gateway_state.sessions.lookup(pair[0], gateway_state.now_ms())
                )
                if principal is not None:
                    return (
                        principal.account == current.account
                        and principal.device_id == current.device_id
                    )
            return bool(await store.ensure_access(current.sid, refresher, force=True))

        # Identity lineage, not just the authenticated/anonymous branch.
        rendered: dict[str, Any] = {}

        def show_login() -> None:
            page.title = "RelayDeck — Connect to your Mac"
            login_appearance[0] = configure_page_theme(page)
            page.render(LoginPrompt)

        def on_logout() -> None:
            try:
                flet_id = page.session.id
            except RuntimeError:
                flet_id = ""
            current = rendered.get("identity")  # Retained only to complete revocation.

            async def run() -> None:
                if current is not None:
                    keep = frozenset({flet_id}) if flet_id else frozenset()
                    await teardown_web_sid(store, gateway_state, current.sid, keep_flet_ids=keep)
                refresh((None,))

            asyncio.create_task(run())

        def show_app(current: WebIdentity) -> None:
            if store.command_store is None:
                from relaydeck.client.pending_commands import MemoryCommandStore

                store.command_store = MemoryCommandStore()
            resources = run_app(
                page,
                base_url=base_url,
                get_token=get_token,
                refresh_identity=refresh_identity,
                recovery_current=lambda: identity_for_page(store, page) == current,
                host_id=host_id,
                account=current.account if current is not None else "",
                device_id=current.device_id if current is not None else "",
                on_logout=on_logout,
                on_session_recovery=lambda: asyncio.create_task(
                    ft.UrlLauncher().launch_url(
                        store.public_origin + "/v1/auth/session/recovery",
                        web_only_window_name="_self",
                    )
                ),
                command_store=store.command_store,
                command_origin=store.public_origin,
                draft_store=draft_store,
            )
            rendered["resources"] = resources

            def visibility(state: str, persisted: bool) -> None:
                if (
                    resources is None
                    or rendered.get("resources") is not resources
                    or rendered.get("identity") != current
                    or getattr(resources, "closing", False)
                ):
                    return
                if state == "hidden":
                    resources.note_hidden()
                diagnostics_logger().info(
                    "ui_visibility state=%s persisted=%s since_hide_ms=%s",
                    state,
                    persisted,
                    resources.hidden_elapsed_ms(),
                )
                if state == "visible" and getattr(page.session, "connection", None) is not None:
                    # The report arrives over HTTP, outside the page's context;
                    # activation re-renders, so it must run as a page task. Without a
                    # live socket the reconnect handler activates instead.
                    async def activate() -> None:
                        resources.activate("visible")

                    page.run_task(activate)

            store._visibility_pages[page.session.id] = _PageVisibility(
                current, page.session, visibility
            )
            if current is not None:
                store.add_closer(current.sid, resources.close)

        def refresh(verified: tuple[WebIdentity | None] | None = None) -> None:
            # Durable main and async event handlers pass identities checked
            # on the private worker. Legacy in-memory reads stay synchronous.
            identity = identity_for_page(store, page) if verified is None else verified[0]
            if "identity" in rendered and rendered["identity"] == identity:
                return
            rendered["identity"] = identity
            show_app(identity) if identity is not None else show_login()

        async def on_reconnect(_: Any) -> None:
            if not _transport_is_live():
                return  # No UI resume from a delayed, superseded connect callback.
            resources = rendered.get("resources")
            if resources is not None:
                resources.fence_replaced_transport(_current_transport.get())
            # Adopt ownership on reattach: this handshake's cookie wins. Re-render only when the
            # verified identity changed, so ordinary reconnects keep app state.
            await rebind_on_connect(store, page)
            identity = await store.run_private(lambda: identity_for_page(store, page))
            if not _transport_is_live():
                return
            same_identity = identity is not None and rendered.get("identity") == identity
            refresh((identity,))
            if same_identity:
                resources = rendered.get("resources")
                if resources is not None:
                    transport = _current_transport.get()
                    held = _current_handshake.get()
                    if (
                        transport is not None
                        and held is not None
                        and identity is not None
                        and held[0] == identity.sid
                        and transport.flet_id == page.session.id
                        and page.session.connection is not None
                    ):
                        connection = page.session.connection
                        flet_id = page.session.id

                        def current() -> bool:
                            return (
                                not transport.closed.is_set()
                                and not transport.done.is_set()
                                and rendered.get("resources") is resources
                                and identity_for_page(store, page) == identity
                                and page.session.id == flet_id
                                and page.session.connection is connection
                            )

                        # A session reconnect is itself proof the page is displayed; no
                        # lifecycle event is guaranteed to accompany it.
                        resources.mark_page_visible()
                        resources.resume_terminals(
                            transport=transport, current=current, closed=transport.closed
                        )
                        resources.activate("connect", probe=False)
                    resources.controller.resume_ui()

        page.on_connect = on_reconnect

        def on_lifecycle(event: Any) -> None:
            # Web focus loss reports hide while the page stays on screen, so a lifecycle hide
            # is not authoritative; transport loss signals a real background. Only show or resume
            # counts as life.
            resources = rendered.get("resources")
            if resources is None:
                return
            if event.state in set(ft.AppLifecycleState):
                diagnostics_logger().info("ui_lifecycle state=%s", event.state.value)
            if event.state in {ft.AppLifecycleState.HIDE, ft.AppLifecycleState.PAUSE}:
                resources.note_hidden()
            elif event.state in {ft.AppLifecycleState.SHOW, ft.AppLifecycleState.RESUME}:
                resources.activate(event.state.value)

        page.on_app_lifecycle_state_change = on_lifecycle
        refresh(verified_initial)

    if store.persistence is not None:

        async def durable_main(page: Any) -> None:
            identity = await store.run_private(lambda: identity_for_page(store, page))
            if _transport_is_live():
                flet_main(page, (identity,))

        return durable_main

    return flet_main


def main(argv: list[str] | None = None) -> int:
    """Local runner: combined web app on one origin (single worker)."""
    import argparse
    import os
    import sys

    parser = argparse.ArgumentParser(description="Run the RelayDeck web app locally.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--public-origin", default="")
    parser.add_argument("--cookie-secret", default="")
    parser.add_argument("--cursor-secret", default="")
    parser.add_argument("--dev-secret", action="store_true")
    parser.add_argument("--dev-login", action="store_true")
    parser.add_argument("--host-id", default="home-mac")
    parser.add_argument(
        "--dev-hosts",
        default="",
        help="Comma-separated dev snapshot hosts (default: --host-id alone)",
    )
    parser.add_argument(
        "--dev-demo-sessions",
        action="store_true",
        help="Seed synthetic demo shell/agent sessions and greet new "
        "attachments with fixed VT bytes (renderer QA only; no PTY)",
    )
    args = parser.parse_args(argv)

    secret = args.cookie_secret or os.environ.get("RELAYDECK_COOKIE_SECRET", "")
    if not secret and args.dev_secret:
        secret = secrets.token_urlsafe(32)
        print("WARNING: ephemeral dev cookie secret (sessions die on restart).")
    if len(secret.encode()) < 32:
        print(
            "error: set --cookie-secret or RELAYDECK_COOKIE_SECRET "
            "(>=32 bytes) or pass --dev-secret",
            file=sys.stderr,
        )
        return 2
    cursor_secret = args.cursor_secret or os.environ.get("RELAYDECK_CURSOR_SECRET", "")
    if not cursor_secret and args.dev_secret:
        cursor_secret = secrets.token_urlsafe(32)
        print("WARNING: ephemeral dev cursor secret (cursors die on restart).")
    if len(cursor_secret.encode()) < 32:
        print(
            "error: set --cursor-secret or RELAYDECK_CURSOR_SECRET "
            "(>=32 bytes) or pass --dev-secret",
            file=sys.stderr,
        )
        return 2
    cursor_secret_b = cursor_secret.encode()
    if args.host not in ("127.0.0.1", "::1", "localhost") and args.dev_login:
        print("error: --dev-login is loopback-only", file=sys.stderr)
        return 2

    import uvicorn

    from relaydeck.gateway.app import GatewayState, create_app

    dev_hosts = frozenset(h.strip() for h in args.dev_hosts.split(",") if h.strip()) or frozenset(
        {args.host_id}
    )
    if args.host_id not in dev_hosts:
        print("error: --host-id must be one of --dev-hosts", file=sys.stderr)
        return 2
    _DEV_HOSTS["value"] = dev_hosts
    public_origin = args.public_origin or f"http://{args.host}:{args.port}"
    seed = dev_demo_snapshot if args.dev_demo_sessions else dev_snapshot
    backend_cls: Any = DemoBackend if args.dev_demo_sessions else FakeBackend
    gateway_state = GatewayState(
        backend=backend_cls(
            now_ms=lambda: int(time.time() * 1000),
            snapshots={host: seed(host, cursor_secret_b) for host in sorted(dev_hosts)},
        ),
        public_origin=public_origin,
        cursor_secret=cursor_secret_b,
    )
    gateway_app = create_app(gateway_state)
    store = WebSessionStore(public_origin=public_origin, cookie_secret=secret.encode())
    flet_main = make_relaydeck_main(
        store=store,
        base_url=public_origin,
        gateway_app=gateway_app,
        gateway_state=gateway_state,
        host_id=args.host_id,
    )
    token = secrets.token_urlsafe(24) if args.dev_login else ""
    _DEV_LOGIN_TOKEN["value"] = token
    app, _ = build_web_app(
        gateway_state=gateway_state,
        gateway_app=gateway_app,
        flet_main=flet_main,
        public_origin=public_origin,
        cookie_secret=secret.encode(),
        store=store,
        dev_login=args.dev_login,
    )
    if args.dev_login:
        print(f"dev login (single-use, loopback): {public_origin}/v1/auth/dev-login?token={token}")
    print(f"serving RelayDeck web at {public_origin} (single worker)")
    uvicorn.run(app, host=args.host, port=args.port, workers=1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
