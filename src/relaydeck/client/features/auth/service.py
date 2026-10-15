"""Client authentication service.

Owns the RFC 8252 login flow against the gateway: PKCE + state in secure
storage, system-browser authorize URL, app-link callback completion,
token refresh with rotation, and logout cleanup. HTTP and browser opening
are injected seams (httpx + UrlLauncher in production, fakes in tests).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import inspect
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any
from urllib.parse import urlencode

import httpx

from relaydeck.client.adapters.ports import CredentialStore
from relaydeck.client.features.auth.callback import (
    CallbackError,
    parse_callback,
    state_matches,
)
from relaydeck.contracts.endpoints import is_trusted_endpoint
from relaydeck.contracts.schemas import SchemaError, validate

VERIFIER_KEY = "oauth_verifier"
STATE_KEY = "oauth_state"
ACCESS_KEY = "access_token"
REFRESH_KEY = "refresh_token"
EXPIRES_KEY = "access_expires_ms"


class AuthState(str, Enum):
    SIGNED_OUT = "signed_out"
    BROWSER_OPEN = "browser_open"
    WAITING_CALLBACK = "waiting_callback"
    EXCHANGING = "exchanging"
    SIGNED_IN = "signed_in"
    REFRESHING = "refreshing"
    ERROR = "error"


@dataclass(frozen=True)
class AuthSnapshot:
    state: AuthState
    account: str = ""
    error: str = ""
    can_retry: bool = False
    notice: str = ""


@dataclass(frozen=True)
class GatewayConfig:
    base_url: str
    plaintext_hosts: frozenset[str] = frozenset()


class AuthError(RuntimeError):
    pass


class HttpxTransport:
    """Production HTTP seam (httpx, connection-pooled by the app)."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def post_json(
        self, url: str, body: dict[str, Any], headers: dict[str, str] | None = None
    ) -> tuple[int, Any]:
        try:
            response = await self._client.post(url, json=body, headers=headers, timeout=30.0)
        except httpx.HTTPError:
            return 503, {
                "code": "host_offline",
                "message": "Identity response unavailable. Start a new sign-in.",
            }
        try:
            payload: Any = response.json()
        except ValueError:
            payload = {}
        return response.status_code, payload


@dataclass
class AuthService:
    config: GatewayConfig
    credentials: CredentialStore
    post_json: Callable[..., Any]  # (url, body, headers=None) -> (status, payload)
    open_url: Callable[[str], Any]
    now_ms: Callable[[], int] = lambda: int(time.time() * 1000)
    _snapshot: AuthSnapshot = AuthSnapshot(AuthState.SIGNED_OUT)
    _listeners: list[Callable[[AuthSnapshot], None]] | None = None
    # Push install ref for the logout revoke,
    # injected by the logout call site as a sync-or-async
    # `() -> str | None` callable (no globals, keeps logout testable).
    # None/empty/failed means "unknown ref" and keeps the `{}` body.
    device_ref_provider: Callable[[], Any] | None = None
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    _completed_state_digest: str = field(default="", repr=False)

    def __post_init__(self) -> None:
        self._listeners = []

    @property
    def snapshot(self) -> AuthSnapshot:
        return self._snapshot

    def subscribe(self, listener: Callable[[AuthSnapshot], None]) -> None:
        assert self._listeners is not None
        self._listeners.append(listener)

    def unsubscribe(self, listener: Callable[[AuthSnapshot], None]) -> None:
        if self._listeners is not None and listener in self._listeners:
            self._listeners.remove(listener)

    def _emit(self, snapshot: AuthSnapshot) -> None:
        self._snapshot = snapshot
        assert self._listeners is not None
        for listener in list(self._listeners):
            listener(snapshot)

    def _base(self) -> str:
        base = self.config.base_url.rstrip("/")
        if not is_trusted_endpoint(base, self.config.plaintext_hosts):
            raise AuthError(f"untrusted gateway endpoint {base}")
        return base

    @staticmethod
    def _verifier() -> str:
        return secrets.token_urlsafe(48)[:64]

    @staticmethod
    def _challenge(verifier: str) -> str:
        digest = hashlib.sha256(verifier.encode()).digest()
        return base64.urlsafe_b64encode(digest).decode().rstrip("=")

    async def restore(self) -> AuthSnapshot:
        """Cold start validates stored authority before reporting signed in."""
        async with self._lock:
            return await self._refresh_locked()

    async def begin_login(self) -> str:
        async with self._lock:
            return await self._begin_login_locked()

    async def _begin_login_locked(self) -> str:
        """Store PKCE + state, emit the authorize URL, open the browser."""
        base = self._base()
        self._completed_state_digest = ""
        verifier = self._verifier()
        state = secrets.token_urlsafe(24)
        await self.credentials.set(VERIFIER_KEY, verifier)
        await self.credentials.set(STATE_KEY, state)
        request = {
            "response_type": "code",
            "code_challenge": self._challenge(verifier),
            "code_challenge_method": "S256",
            "state": state,
            "redirect_uri": "dev.relaydeck.app://oauth",
        }
        validate("relaydeck/gateway/v1/authorization-request", request)
        query = urlencode(request)
        url = f"{base}/v1/authorize?{query}"
        self._emit(AuthSnapshot(AuthState.WAITING_CALLBACK))
        await self.open_url(url)
        return url

    async def complete_callback(self, uri: str) -> AuthSnapshot:
        async with self._lock:
            return await self._complete_callback_locked(uri)

    async def _complete_callback_locked(self, uri: str) -> AuthSnapshot:
        """Exchange an app-link callback for tokens (single attempt per
        callback; failures surface without clearing unrelated state)."""
        try:
            parsed = parse_callback(uri, https_hosts=frozenset({self._gateway_host()}))
        except CallbackError as exc:
            snapshot = AuthSnapshot(AuthState.ERROR, error=str(exc), can_retry=True)
            self._emit(snapshot)
            return snapshot
        expected = await self.credentials.get(STATE_KEY)
        if (
            not expected
            and self._snapshot.state == AuthState.SIGNED_IN
            and hashlib.sha256(parsed.state.encode()).hexdigest() == self._completed_state_digest
        ):
            return self._snapshot  # Duplicate delivery has no exchange path or new authority.
        if not state_matches(expected or "", parsed.state):
            snapshot = AuthSnapshot(AuthState.ERROR, error="state mismatch", can_retry=True)
            self._emit(snapshot)
            return snapshot
        if parsed.error:
            snapshot = AuthSnapshot(
                AuthState.ERROR,
                error=f"{parsed.error}: {parsed.error_description}".rstrip(": "),
                can_retry=True,
            )
            self._emit(snapshot)
            return snapshot
        assert parsed.code is not None
        self._emit(AuthSnapshot(AuthState.EXCHANGING))
        verifier = await self.credentials.get(VERIFIER_KEY)
        # Consume state before a network await. An uncertain exchange is never replayed.
        await self.credentials.delete(VERIFIER_KEY)
        await self.credentials.delete(STATE_KEY)
        status, payload = await self.post_json(
            f"{self._base()}/v1/token",
            {"grant": "authorization_code", "code": parsed.code, "verifier": verifier or ""},
        )
        await self.credentials.delete(VERIFIER_KEY)
        await self.credentials.delete(STATE_KEY)
        if status != 200 or not isinstance(payload, dict):
            message = (
                payload.get("message", "exchange failed")
                if isinstance(payload, dict)
                else "exchange failed"
            )
            snapshot = AuthSnapshot(AuthState.ERROR, error=str(message), can_retry=True)
            self._emit(snapshot)
            return snapshot
        if not await self._save_pair(payload):
            return self._snapshot
        self._completed_state_digest = hashlib.sha256(parsed.state.encode()).hexdigest()
        snapshot = AuthSnapshot(AuthState.SIGNED_IN)
        self._emit(snapshot)
        return snapshot

    async def refresh(self, *, require_signed_in: bool = False) -> AuthSnapshot:
        async with self._lock:
            if require_signed_in and self._snapshot.state != AuthState.SIGNED_IN:
                return self._snapshot
            return await self._refresh_locked()

    async def _refresh_locked(self) -> AuthSnapshot:
        refresh = await self.credentials.get(REFRESH_KEY)
        if not refresh:
            await self.credentials.delete(ACCESS_KEY)
            await self.credentials.delete(EXPIRES_KEY)
            snapshot = AuthSnapshot(AuthState.SIGNED_OUT)
            self._emit(snapshot)
            return snapshot
        self._emit(AuthSnapshot(AuthState.REFRESHING))
        status, payload = await self.post_json(
            f"{self._base()}/v1/token",
            {"grant": "refresh_token", "refresh_token": refresh},
        )
        if status != 200 or not isinstance(payload, dict):
            if status in (400, 401, 403):
                await self.credentials.clear_account()
            snapshot = AuthSnapshot(AuthState.ERROR, error="refresh failed", can_retry=True)
            self._emit(snapshot)
            return snapshot
        if not await self._save_pair(payload):
            return self._snapshot
        snapshot = AuthSnapshot(AuthState.SIGNED_IN)
        self._emit(snapshot)
        return snapshot

    async def logout(self) -> AuthSnapshot:
        async with self._lock:
            return await self._logout_locked()

    async def _logout_locked(self) -> AuthSnapshot:
        """Revoke the server session, then clear local credentials.

        Local state is always cleared; when the server revoke fails
        (offline, expired token) the snapshot carries a notice and the
        server session lingers until its TTL.

        When a push device_ref is known (via the injected
        `device_ref_provider`), it is sent as `{"device_ref": ...}` so
        the server revokes the install subscription (unknown/foreign
        refs are a silent server-side no-op). Without a known ref the
        body stays `{}` (existing behavior).
        """
        self._completed_state_digest = ""
        notice = ""
        access = await self.credentials.get(ACCESS_KEY)
        if access:
            body: dict[str, Any] = {}
            ref = await self._resolve_device_ref()
            if ref is not None:
                body = {"device_ref": ref}
            try:
                status, _ = await self.post_json(
                    f"{self._base()}/v1/logout",
                    body,
                    {"Authorization": f"Bearer {access}"},
                )
                if not (200 <= status < 300):
                    notice = "signed out locally; server revoke failed"
            except Exception:  # noqa: BLE001 - logout always clears local state
                notice = "signed out locally; server revoke failed"
        await self.credentials.clear_account()
        snapshot = AuthSnapshot(AuthState.SIGNED_OUT, notice=notice)
        self._emit(snapshot)
        return snapshot

    async def _resolve_device_ref(self) -> str | None:
        """The known push device_ref, if any. A missing, empty, or failed
        lookup means "unknown" and keeps the `{}` logout body; logout
        never fails on ref resolution."""
        provider = self.device_ref_provider
        if provider is None:
            return None
        try:
            ref = provider()
        except Exception:  # noqa: BLE001 - logout never fails on ref lookup
            return None
        if inspect.isawaitable(ref):
            try:
                ref = await ref
            except Exception:  # noqa: BLE001 - logout never fails on ref lookup
                return None
        if isinstance(ref, str) and ref:
            return ref
        return None

    async def access_token(self) -> str | None:
        async with self._lock:
            if self._snapshot.state == AuthState.ERROR:
                return None
            access = await self.credentials.get(ACCESS_KEY)
            raw_expiry = await self.credentials.get(EXPIRES_KEY)
            try:
                expires = int(raw_expiry or "0")
            except ValueError:
                expires = 0
            if (
                self._snapshot.state == AuthState.SIGNED_IN
                and access
                and expires > self.now_ms() + 30_000
            ):
                return access
            snapshot = await self._refresh_locked()
            if snapshot.state != AuthState.SIGNED_IN:
                return None
            return await self.credentials.get(ACCESS_KEY)

    async def _save_pair(self, payload: dict[str, Any]) -> bool:
        try:
            validate("relaydeck/gateway/v1/token-response", payload)
        except SchemaError:
            await self.credentials.clear_account()
            self._emit(
                AuthSnapshot(AuthState.ERROR, error="Invalid token response", can_retry=True)
            )
            return False
        await self.credentials.set(ACCESS_KEY, payload["access_token"])
        await self.credentials.set(REFRESH_KEY, payload["refresh_token"])
        await self.credentials.set(EXPIRES_KEY, str(self.now_ms() + payload["expires_in"] * 1000))
        return True

    async def accept_pairing(self, tokens: dict[str, Any], current: Callable[[], bool]) -> bool:
        """Pairing's verified device grant; caller serializes endpoint changes."""
        async with self._lock:
            if not current() or not await self.credentials.available():
                return False
            try:
                if not await self._save_pair(tokens):
                    return False
                if not current():
                    await self.credentials.clear_account()
                    return False
            except asyncio.CancelledError:
                await self.credentials.clear_account()
                self._emit(AuthSnapshot(AuthState.SIGNED_OUT))
                raise
            except Exception:  # noqa: BLE001 - no insecure fallback or partial token pair
                await self.credentials.clear_account()
                self._emit(AuthSnapshot(AuthState.SIGNED_OUT))
                return False
            self._emit(AuthSnapshot(AuthState.SIGNED_IN))
            return True

    def _gateway_host(self) -> str:
        from urllib.parse import urlparse

        return (urlparse(self.config.base_url).hostname or "").lower()
