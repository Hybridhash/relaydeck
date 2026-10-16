"""Gateway HTTP/WebSocket application.

All routes authenticate via bearer token, enforce capability policy,
validate bodies against `relaydeck.contracts.schemas`, and return the
shared error envelope. Terminal streaming rides a WebSocket with the
binary framing in `terminal_wire`; metadata rides SSE with HMAC cursors.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import secrets
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.websockets import WebSocketState

from relaydeck.connector.herdr import EXPECTED_PROTOCOL
from relaydeck.contracts.schemas import (
    ERROR_STATUS,
    GATEWAY_PROTOCOL,
    SchemaError,
    validate,
)
from relaydeck.contracts.tunnel import SnapshotIntake, parse_terminal_input
from relaydeck.gateway import push, terminal_wire
from relaydeck.gateway.authorization import (
    CODE_TTL_MS,
    FLOW_LIMIT,
    AuthorizationError,
    AuthorizationFlow,
    IdentityProvider,
)
from relaydeck.gateway.backend import Backend, BackendError
from relaydeck.gateway.cursors import AFTER_SEQUENCE_HEADER, CursorError, decode_cursor
from relaydeck.gateway.hosts import HostStore, drain_revoke_requests
from relaydeck.gateway.identity import (
    TICKET_TTL_MS,
    EnrollmentStore,
    Session,
    SessionStore,
    TicketGrant,
    TicketStore,
)
from relaydeck.gateway.inventory import InventoryView
from relaydeck.gateway.metadata_streams import MetadataResponse, MetadataStreams
from relaydeck.gateway.notification_transitions import NotificationTransitions, Status
from relaydeck.gateway.pairing import PairingError, PairingStore
from relaydeck.gateway.policy import (
    authorize_command,
    command_limiter,
    input_limiter,
)
from relaydeck.gateway.push import ProviderPort
from relaydeck.gateway.routes_enrollments import install_enrollments
from relaydeck.gateway.sockets import SocketRegistry
from relaydeck.gateway.usage import UsageService
from relaydeck.gateway.usage import install as install_usage
from relaydeck.lease_diagnostics import LeaseTrace, opaque
from relaydeck.persistence_contracts import StorageUnavailable


def error_response(
    code: str,
    message: str,
    retryable: bool = False,
    *,
    status: int | None = None,
    details: dict[str, Any] | None = None,
) -> JSONResponse:
    body: dict[str, Any] = {"code": code, "message": message, "retryable": retryable}
    if details is not None:
        body["details"] = details
    validate("relaydeck/gateway/v1/error", body)
    return JSONResponse(status_code=status or ERROR_STATUS[code], content=body)


# server-enforced bounded expiry for submitted commands.
MAX_EXPIRES_FUTURE_MS = 300_000

# 64 KiB JSON request bound, enforced for every HTTP route.
MAX_JSON_BODY = 64 * 1024

# 1 MiB bounded snapshot messages (compromised-connector DoS cap).
MAX_SNAPSHOT_BYTES = 1024 * 1024


class BodyCapMiddleware:
    """Pure-ASGI request-body cap: buffer the body up to MAX_JSON_BODY;
    over-cap bodies get 413 too_large without reaching any route (the
    bound holds with or without a Content-Length header)."""

    def __init__(self, app: Any) -> None:
        self._app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> Any:
        if scope["type"] != "http":
            return await self._app(scope, receive, send)
        # The authenticated picture route owns its early + streamed 10 MB cap.
        import re

        if scope.get("method") == "PUT" and re.fullmatch(
            r"/v1/pictures/[a-f0-9]{32}/content", scope.get("path", "")
        ):
            return await self._app(scope, receive, send)
        chunks: list[bytes] = []
        total = 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return await self._app(scope, _disconnected_receive, send)
            piece = bytes(message.get("body", b""))
            total += len(piece)
            if total > MAX_JSON_BODY:
                body = {"code": "too_large", "message": "body too large", "retryable": False}
                validate("relaydeck/gateway/v1/error", body)
                payload = json.dumps(body).encode()
                await send(
                    {
                        "type": "http.response.start",
                        "status": ERROR_STATUS["too_large"],
                        "headers": [
                            (b"content-type", b"application/json"),
                            (b"content-length", str(len(payload)).encode()),
                        ],
                    }
                )
                await send({"type": "http.response.body", "body": payload})
                return None
            chunks.append(piece)
            if not message.get("more_body"):
                break
        full = b"".join(chunks)
        replayed = False

        async def _replay() -> dict[str, Any]:
            # One buffered body, then the live channel: downstream
            # disconnect listeners (StreamingResponse under ASGI < 2.4)
            # must still observe http.disconnect.
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": full, "more_body": False}
            live: dict[str, Any] = await receive()
            return live

        return await self._app(scope, _replay, send)


async def _disconnected_receive() -> dict[str, Any]:
    return {"type": "http.disconnect"}


# inventory page bound.
MAX_AGENTS_PER_PAGE = 200

# attachment/lease request bounds. These documents have no schema in
# contracts/schemas.py, so bodies are validated inline here and responses
# are plain versioned dicts (no validate() call) plus the shared error
# envelope.
ATTACHMENT_KINDS = ("agent", "shell")
LEASE_MODES = ("view", "control")
MAX_TERMINAL_ID_CHARS = 128
MAX_ATTACHMENT_ID_CHARS = 128
MAX_LEASE_ID_CHARS = 128
MIN_PTY_CELLS = 1
MAX_PTY_CELLS = 1024
DEFAULT_PTY_COLS = 80
DEFAULT_PTY_ROWS = 24

# SSE keepalive: comment frames the parser skips .
EVENTS_HEARTBEAT_S = 15.0

# live-push + tunnel pump bounds (OWASP WebSocket hardening:
# bounded polls, per-message size limits, single live tunnel per host).
TERMINAL_PUSH_POLL_S = 0.05
TUNNEL_POLL_S = 0.05
TUNNEL_MAX_TEXT = 64 * 1024
TUNNEL_MAX_OUTPUT = 64 * 1024
LOG = logging.getLogger("relaydeck.gateway.app")


def _parse_utc_ms(value: str) -> int | None:
    try:
        moment = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        try:
            moment = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
        except ValueError:
            return None
    return int(moment.timestamp() * 1000)


@dataclass
class GatewayState:
    backend: Backend
    sessions: SessionStore = field(default_factory=SessionStore)
    cursor_secret: bytes = b""
    now_ms: Callable[[], int] = field(default=lambda: int(time.time() * 1000))
    commands: Any = field(default_factory=command_limiter)
    input_bytes: Any = field(default_factory=input_limiter)
    pending_codes: dict[str, dict[str, Any]] = field(default_factory=dict)
    authorization: AuthorizationFlow = field(default_factory=AuthorizationFlow)
    identity_provider: IdentityProvider | None = None
    pairing: PairingStore | None = None
    pictures: Any = None  # Mac companion only; no image store on cloud gateways
    usage: UsageService | None = None
    authorization_slots: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(8))
    hosts: HostStore = field(default_factory=HostStore)
    enrollments: EnrollmentStore = field(default_factory=EnrollmentStore)
    tickets: TicketStore = field(default_factory=TicketStore)
    sockets: SocketRegistry = field(default_factory=SocketRegistry)
    public_origin: str = ""
    # in-memory by default (tests inject a file-backed store).
    push: push.PushStore = field(default_factory=push.PushStore)
    push_provider: ProviderPort | None = None
    # queues browser/home-screen messages for the same eligible devices (set by runtime).
    web_push_sink: Callable[[str, frozenset[str], str, str, str, str], None] | None = None
    revoke_spool: str = ""
    revoke_poll_s: float = 5.0
    inventory: InventoryView = field(default_factory=InventoryView)
    metadata: MetadataStreams = field(default_factory=MetadataStreams)
    notifications: NotificationTransitions = field(default_factory=NotificationTransitions)

    def push_delivery_allowed(self, row: dict[str, Any]) -> bool:
        resolved = self.push.resolve_ref(str(row["host_ref"]))
        if resolved is None or resolved[0] != "host":
            return False
        host, account = resolved[1], str(row["account"])
        record = self.hosts.record_for_host(host)
        if record is None or record.account != account or not self.hosts.allowed(account, host):
            return False
        device = str(row["device_id"])
        # a category the device turned off is never sent, even if already queued.
        return device in self.sessions.notification_devices(
            account, host, self.now_ms(), owned=True
        ) and self.push.category_allowed(account, device, str(row["category"]))

    def emit_notification(self, status: Status, category: str) -> None:
        record = self.hosts.record_for_host(status.host)
        if (
            record is None
            or record.account != status.account
            or not self.hosts.allowed(status.account, status.host)
        ):
            return
        devices = self.sessions.notification_devices(
            status.account, status.host, self.now_ms(), owned=True
        )
        if self.web_push_sink is not None and devices:
            self.web_push_sink(
                status.account,
                devices,
                status.event_id,
                category,
                status.host,
                status.session,
            )
        if devices:
            self.push.notify(
                account=status.account,
                account_hosts=frozenset({status.host}),
                host_id=status.host,
                session_id=status.session,
                event_id=status.event_id,
                category=category,
                now=self.now_ms(),
                device_ids=devices,
            )


def _utc_z(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


async def revoke_account_and_close(state: GatewayState, account: str) -> int:
    """Revoke every session, invalidate every lease, and sever every live
    terminal socket now (logout/revoke kills writers)."""
    try:
        state.sessions.revoke_account(account)
    finally:
        # The authorized scope survives failed durable I/O. Pending denial
        # blocks new credentials; existing writers must also stop now.
        state.metadata.revoke(account=account)
        state.backend.revoke_leases_account(account)
        state.inventory.drop_account(account)
        closed = await state.sockets.close_account(account, "unauthenticated")
    return closed


def durable_session_allowed(state: GatewayState, session: Session) -> bool:
    if not session.host_binding:
        return True
    if len(session.hosts) != 1:
        return False
    record = state.hosts.record_for_host(next(iter(session.hosts)))
    return bool(
        record and record.account == session.account and record.token_prefix == session.host_binding
    )


async def release_lease_and_close(
    state: GatewayState, lease_id: str, account: str, device_id: str
) -> None:
    """Detach one lease (the attachment and remote process survive) and
    sever its sockets. Holder-only: unknown, foreign, or other-device
    leases are BackendError not_found (unlike attachment close, which
    is account-scoped)."""
    attachment = state.backend.release_lease(lease_id, account, device_id)
    await state.sockets.close_lease(lease_id, "detached")
    await publish_terminal_routes(state, attachment.host_id)


async def publish_terminal_routes(state: GatewayState, host_id: str) -> int:
    """Gateway-only ownership mode: observer streams cannot hold Mac sizing."""
    record = state.hosts.record_for_host(host_id)
    if record is not None:
        return await state.sockets.notify_host(
            host_id,
            json.dumps(
                {
                    "kind": "terminal.epochs",
                    "host_id": host_id,
                    "epochs": state.backend.epochs_for_host(host_id, record.account),
                }
            ),
            binding=record,
        )
    return 0


async def reconcile_terminal_routes(state: GatewayState) -> None:
    """Expiry also tears down upstream ownership with no live browser socket."""
    previous: dict[str, list[dict[str, str]]] = {}
    while True:
        for record in state.hosts.hosts():
            routes = state.backend.epochs_for_host(record.host_id, record.account)
            if previous.get(record.host_id) != routes:
                notified = await publish_terminal_routes(state, record.host_id)
                if notified >= state.sockets.host_count(record.host_id, record):
                    previous[record.host_id] = routes
        await asyncio.sleep(1)


async def revoke_host_and_close(state: GatewayState, host_id: str) -> tuple[bool, int, int]:
    """Revoke a host token and sever its tunnels and leases now."""
    dead_sessions: tuple[Session, ...] = ()
    try:
        dead_sessions = state.sessions.revoke_paired_host(host_id)
        revoked = state.hosts.revoke(host_id)
    finally:
        state.metadata.revoke(host_id=host_id)
        # Fence leases before awaiting socket shutdown, even when there is
        # no durable result from which to enumerate paired devices.
        dead = state.backend.revoke_leases_host(host_id)
        for session in dead_sessions:
            state.metadata.revoke(account=session.account, device_id=session.device_id)
            await state.sockets.close_device(session.account, session.device_id, "unauthenticated")
        state.inventory.drop_host(host_id)
        state.notifications.drop_host(host_id)
        tunnels = await state.sockets.close_host(host_id, "unauthenticated")
        leases = 0
        for lease_id in dead:
            leases += await state.sockets.close_lease(lease_id, "lease_expired")
    return revoked, tunnels, leases


def _bearer(request: Request) -> str:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return ""
    return token


def create_app(state: GatewayState) -> FastAPI:
    async def drain_spool() -> None:
        while True:
            for host_id in drain_revoke_requests(state.revoke_spool):
                await revoke_host_and_close(state, host_id)
            await asyncio.sleep(max(0.01, state.revoke_poll_s))

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        picture_task = None
        if state.pictures is not None:
            from relaydeck.companion.pictures import run_picture_cleanup

            await asyncio.to_thread(state.pictures.cleanup, state.now_ms())
            picture_task = asyncio.create_task(run_picture_cleanup(state.pictures, state.now_ms))
        task = asyncio.create_task(drain_spool()) if state.revoke_spool else None
        from relaydeck.gateway.push_worker import PushWorker

        worker = (
            PushWorker(state.push, state.push_provider, authorized=state.push_delivery_allowed)
            if state.push_provider is not None
            else None
        )
        notification_stop = asyncio.Event()
        notification_task = asyncio.create_task(
            state.notifications.run(notification_stop, state.now_ms, state.emit_notification)
        )
        terminal_task = asyncio.create_task(reconcile_terminal_routes(state))
        app.state.push_worker = worker
        if worker is not None:
            worker.start()
        try:
            yield
        finally:
            terminal_task.cancel()
            with suppress(asyncio.CancelledError):
                await terminal_task
            if picture_task is not None:
                picture_task.cancel()
                with suppress(asyncio.CancelledError):
                    await picture_task
            notification_stop.set()
            await notification_task
            try:
                if worker is not None:
                    await worker.stop()
            finally:
                if task is not None:
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task
                close = getattr(state.identity_provider, "close", None)
                if close is not None:
                    await close()

    app = FastAPI(title="RelayDeck gateway", version="1.0.0", lifespan=lifespan)
    app.state.relaydeck = state
    app.add_middleware(BodyCapMiddleware)  # type: ignore[arg-type]

    @app.exception_handler(StorageUnavailable)
    async def storage_unavailable(request: Request, exc: StorageUnavailable) -> JSONResponse:
        return error_response(
            "unauthenticated",
            "Protected sign-in storage unavailable",
            status=503,
            details={"reason": "storage_unavailable", "storage_reason": exc.code},
        )

    def session_allowed(session: Session) -> bool:
        return durable_session_allowed(state, session)

    def current_session(request: Request):  # type: ignore[no-untyped-def]
        token = _bearer(request)
        if not token:
            return None
        session = state.sessions.lookup(token, state.now_ms())
        if session is None or not session_allowed(session):
            return None
        hosts = session.hosts
        if not session.host_binding:
            hosts |= state.hosts.owned_ids(session.account)
        return replace(
            session,
            hosts=frozenset(h for h in hosts if state.hosts.allowed(session.account, h)),
        )

    def device_allowed(session: Session, device_id: str) -> bool:
        # Paired Console grants are not account-wide device administration.
        return not session.host_binding or device_id == session.device_id

    from relaydeck.companion.picture_routes import install as install_pictures

    install_pictures(app, state, current_session, session_allowed, error_response)

    async def publish_epochs(host_id: str) -> None:
        await publish_terminal_routes(state, host_id)

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        worker = getattr(app.state, "push_worker", None)
        return {
            "ok": True,
            "version": "1.0.0",
            "push": {
                "configured": state.push_provider is not None,
                "enqueue_error": state.notifications.error,
                "delivery_error": worker.error if worker else "",
            },
        }

    def emit_command_update(command: dict[str, Any] | None) -> None:
        if command is None:
            return
        try:
            snapshot = state.backend.snapshot(str(command.get("host_id", "")))
            host_epoch = str(snapshot["host"]["epoch"])
        except (BackendError, KeyError):
            return
        state.backend.publish_command_updated(
            command, host_epoch, state.cursor_secret, state.now_ms()
        )

    async def pairing_body(request: Request, schema: str) -> dict[str, Any]:
        from relaydeck.contracts.pairing import MAX_BODY_BYTES

        if state.pairing is None:
            raise PairingError()
        # Native requests have no Origin; browser origins must match exactly.
        origins = request.headers.getlist("origin")
        if origins and origins != [state.public_origin]:
            raise PairingError("forbidden", "Pairing origin mismatch")
        raw = await request.body()
        if len(raw) > MAX_BODY_BYTES:
            raise PairingError("too_large", "Pairing request exceeds 2048 bytes")
        try:

            def unique_fields(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
                result: dict[str, Any] = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError("Duplicate pairing field")
                    result[key] = value
                return result

            body = json.loads(raw, object_pairs_hook=unique_fields)
            validate(schema, body)
        except (ValueError, SchemaError):
            raise PairingError("forbidden", "Malformed pairing request") from None
        if body["origin"] != state.public_origin:
            raise PairingError()
        return body  # type: ignore[no-any-return]

    def pairing_response(body: dict[str, Any], status: int = 200) -> JSONResponse:
        validate("relaydeck/gateway/v1/pairing-status", body)
        return JSONResponse(body, status_code=status, headers={"Cache-Control": "no-store"})

    @app.post("/v1/pairing/requests", status_code=201)
    async def request_pairing(request: Request) -> JSONResponse:
        try:
            body = await pairing_body(request, "relaydeck/gateway/v1/pairing-request")
            assert state.pairing is not None
            result = state.pairing.request(
                body, state.hosts.record_for_host(body["host_id"]), state.now_ms()
            )
            return pairing_response(result, 201)
        except PairingError as exc:
            return error_response(exc.code, str(exc))

    @app.post("/v1/pairing/{request_id}/status")
    async def pairing_status(request_id: str, request: Request) -> JSONResponse:
        try:
            body = await pairing_body(request, "relaydeck/gateway/v1/pairing-proof")
            assert state.pairing is not None
            result = state.pairing.poll(
                request_id, body, state.hosts.record_for_host(body["host_id"]), state.now_ms()
            )
            return pairing_response(result)
        except PairingError as exc:
            return error_response(exc.code, str(exc))

    @app.post("/v1/pairing/{request_id}/redeem")
    async def redeem_pairing(request_id: str, request: Request) -> JSONResponse:
        try:
            body = await pairing_body(request, "relaydeck/gateway/v1/pairing-proof")
            assert state.pairing is not None
            grant = state.pairing.redeem(
                request_id, body, state.hosts.record_for_host(body["host_id"]), state.now_ms()
            )
        except PairingError as exc:
            return error_response(exc.code, str(exc))
        # No await between consuming the approval and creating the session.
        access, refresh = state.sessions.create(
            grant.account,
            grant.capabilities,
            (grant.host_id,),
            state.now_ms(),
            grant.device_id,
            host_binding=grant.host_binding,
        )
        out = {"access_token": access, "refresh_token": refresh, "expires_in": 900}
        validate("relaydeck/gateway/v1/token-response", out)
        return JSONResponse(out, headers={"Cache-Control": "no-store"})

    @app.get("/v1/authorize")
    async def authorize(request: Request) -> Any:
        provider = state.identity_provider
        if provider is None:
            return error_response("host_offline", "Identity provider configuration required", False)
        try:
            if len(request.query_params.multi_items()) != len(request.query_params):
                raise AuthorizationError("Duplicate authorization parameter")
            flow_state, attempt = state.authorization.begin(
                dict(request.query_params), state.public_origin, state.now_ms()
            )
            destination = provider.authorize_url(flow_state, attempt)
        except AuthorizationError:
            return error_response("forbidden", "Invalid authorization request")
        return RedirectResponse(
            destination,
            status_code=302,
            headers={
                "Cache-Control": "no-store",
                "Referrer-Policy": "no-referrer",
            },
        )

    @app.get("/v1/identity/callback")
    async def identity_callback(request: Request) -> Any:
        provider = state.identity_provider
        if provider is None:
            return error_response("host_offline", "Identity provider configuration required", False)
        try:
            if len(request.query_params.multi_items()) != len(request.query_params) or set(
                request.query_params
            ) - {"state", "code", "error", "error_description"}:
                raise AuthorizationError("Invalid identity callback")
            attempt = state.authorization.take(
                request.query_params.get("state", ""), state.now_ms()
            )
            code = request.query_params.get("code", "")
            if request.query_params.get("error") or not code or len(code) > 2048:
                raise AuthorizationError("Identity authorization declined")
            async with state.authorization_slots:
                principal = await provider.exchange(code, attempt)
            now = state.now_ms()
            if attempt.expires_ms <= now:
                raise AuthorizationError("Identity authorization expired")
            state.pending_codes = {
                key: value
                for key, value in state.pending_codes.items()
                if value.get("expires_ms", 0) > now
            }
            if len(state.pending_codes) >= FLOW_LIMIT:
                raise AuthorizationError("Authorization code capacity reached")
            gateway_code = secrets.token_urlsafe(32)
            state.pending_codes[gateway_code] = {
                "challenge": attempt.challenge,
                "account": principal.account,
                "capabilities": principal.capabilities,
                "hosts": principal.hosts,
                "expires_ms": now + CODE_TTL_MS,
            }
            from urllib.parse import urlencode

            destination = (
                attempt.redirect_uri
                + "?"
                + urlencode(
                    {
                        "state": attempt.client_state,
                        "code": gateway_code,
                    }
                )
            )
            return RedirectResponse(
                destination,
                status_code=302,
                headers={
                    "Cache-Control": "no-store",
                    "Referrer-Policy": "no-referrer",
                },
            )
        except AuthorizationError:
            return error_response("unauthenticated", "Identity authorization failed; start again")

    @app.post("/v1/token")
    async def token_exchange(request: Request) -> JSONResponse:
        try:
            body = await request.json()
            validate("relaydeck/gateway/v1/token-exchange", body)
        except (ValueError, SchemaError):
            return error_response(
                "forbidden", "malformed token request", details={"reason": "malformed"}
            )
        if body["grant"] == "refresh_token":
            pair = state.sessions.refresh(
                body.get("refresh_token", ""), state.now_ms(), allowed=session_allowed
            )
            if pair is None:
                return error_response("unauthenticated", "refresh rejected")
            access, refresh = pair
            out = {"access_token": access, "refresh_token": refresh, "expires_in": 900}
            validate("relaydeck/gateway/v1/token-response", out)
            return JSONResponse(out)
        code = body.get("code", "")
        pending = state.pending_codes.pop(code, None)
        if pending is None:
            return error_response("unauthenticated", "code unknown or reused")
        if pending.get("expires_ms", state.now_ms() + 1) <= state.now_ms():
            return error_response("unauthenticated", "authorization code expired")
        from relaydeck.gateway.identity import pkce_verify

        if not pkce_verify(body.get("verifier", ""), pending["challenge"]):
            return error_response("unauthenticated", "PKCE mismatch")
        access, refresh = state.sessions.create(
            pending["account"],
            pending["capabilities"],
            pending["hosts"],
            state.now_ms(),
        )
        out = {"access_token": access, "refresh_token": refresh, "expires_in": 900}
        validate("relaydeck/gateway/v1/token-response", out)
        return JSONResponse(out)

    @app.post("/v1/logout")
    async def logout_session(request: Request) -> Any:
        token = _bearer(request)
        if token is None:
            return error_response("unauthenticated", "missing or expired token")
        if state.sessions.persistence is None:
            if state.sessions.lookup(token, state.now_ms()) is None:
                return error_response("unauthenticated", "missing or expired token")
            dead = state.sessions.revoke_session(token)
        else:
            local = state.sessions.begin_revocation(token)
            dead = None
            try:
                dead = await state.sessions.persistence.run_draft(
                    lambda: state.sessions.revoke_session(token)
                )
            finally:
                local = dead or state.sessions.pending_revocation(token) or local
                if local is not None:
                    state.metadata.revoke(account=local.account, device_id=local.device_id)
                    state.backend.revoke_leases_device(local.account, local.device_id)
                    await state.sockets.close_device(
                        local.account, local.device_id, "unauthenticated"
                    )
                    state.push.revoke_device(local.account, local.device_id, state.now_ms())
            if dead is None:
                return error_response("unauthenticated", "missing or expired token")
        if dead is not None:
            if state.sessions.persistence is None:
                state.metadata.revoke(account=dead.account, device_id=dead.device_id)
                state.backend.revoke_leases_device(dead.account, dead.device_id)
                await state.sockets.close_device(dead.account, dead.device_id, "unauthenticated")
            # logout removes the bound push subscription and its
            # token association (the client also DELETEs its install id,
            # which may differ from the session device id).
            if state.sessions.persistence is None:
                state.push.revoke_device(dead.account, dead.device_id, state.now_ms())
            # An optional {"device_ref": ...} logout body also revokes that push record when the
            # session account owns it. Absent, malformed, unknown or foreign refs are a silent
            # no-op.
            try:
                logout_body = await request.json()
            except ValueError:
                logout_body = None
            logout_ref = logout_body.get("device_ref") if isinstance(logout_body, dict) else None
            # Paired push subscriptions use the issued device_id revoked above;
            # an account-owned install ref must not target a different device.
            if isinstance(logout_ref, str) and logout_ref and not dead.host_binding:
                state.push.revoke_device_by_ref(dead.account, logout_ref, state.now_ms())
        return {"ok": True}

    # device subscriptions + deep links. Bodies are
    # validated inline (proposed schemas live with the contracts owner);
    # responses are plain versioned dicts plus the shared error envelope.
    @app.post("/v1/devices", status_code=201)
    async def register_device(request: Request) -> JSONResponse:
        session = current_session(request)
        if session is None:
            return error_response("unauthenticated", "missing or expired token")
        try:
            body = await request.json()
        except ValueError:
            return error_response("forbidden", "malformed device", details={"reason": "malformed"})
        keys = {"device_id", "token", "platform"}
        if (
            not isinstance(body, dict)
            or set(body) != keys
            or not all(isinstance(body[key], str) for key in keys)
        ):
            return error_response("forbidden", "malformed device", details={"reason": "malformed"})
        if not device_allowed(session, body["device_id"]):
            return error_response("not_found", "no such device")
        try:
            record = state.push.register_device(
                session.account,
                body["device_id"],
                body["token"],
                body["platform"],
                state.now_ms(),
            )
        except push.PushDenied as exc:
            return error_response(exc.code, str(exc) or "device refused")
        out = {"device_id": record.device_id, "platform": record.platform, "status": record.status}
        validate("relaydeck/gateway/v1/device-registration", out)
        return JSONResponse(status_code=201, content=out)

    @app.post("/v1/devices/{device_id}/rotate")
    async def rotate_device_token(device_id: str, request: Request) -> JSONResponse:
        session = current_session(request)
        if session is None:
            return error_response("unauthenticated", "missing or expired token")
        if (
            not device_id
            or len(device_id) > push.MAX_DEVICE_ID_CHARS
            or not device_allowed(session, device_id)
        ):
            return error_response("not_found", "no such device")
        try:
            body = await request.json()
        except ValueError:
            return error_response(
                "forbidden", "malformed rotation", details={"reason": "malformed"}
            )
        if (
            not isinstance(body, dict)
            or set(body) != {"token"}
            or not isinstance(body["token"], str)
        ):
            return error_response(
                "forbidden", "malformed rotation", details={"reason": "malformed"}
            )
        try:
            record = state.push.rotate_token(
                session.account, device_id, body["token"], state.now_ms()
            )
        except push.PushDenied as exc:
            return error_response(exc.code, str(exc) or "rotation refused")
        if record is None:
            return error_response("not_found", "no such device")
        return JSONResponse({"device_id": record.device_id, "status": record.status})

    @app.get("/v1/devices")
    async def list_devices(request: Request) -> JSONResponse:
        session = current_session(request)
        if session is None:
            return error_response("unauthenticated", "missing or expired token")
        now = state.now_ms()
        auth = state.sessions.device_metadata(session.account, now)
        subscriptions = {
            row["device_id"]: row for row in state.push.device_metadata(session.account)
        }
        if session.host_binding:
            # Filter before ordering/counts as well as before row serialization.
            auth = {key: row for key, row in auth.items() if device_allowed(session, key)}
            subscriptions = {
                key: row for key, row in subscriptions.items() if device_allowed(session, key)
            }
        identifiers = sorted(
            auth.keys() | subscriptions.keys(),
            key=lambda value: (value != session.device_id, value),
        )
        rows = []
        for device_id in identifiers[:200]:
            credentials = auth.get(device_id, {})
            subscription = subscriptions.get(device_id, {})
            active = credentials.get("access_expires_ms", 0) >= now
            status = (
                ("active" if active else "refresh_only")
                if credentials
                else ("notifications_only" if subscription.get("status") == "active" else "revoked")
            )
            row = {
                "device_id": device_id,
                "current": device_id == session.device_id,
                "platform": subscription.get("platform", "unknown"),
                "status": status,
                "auth_sessions": credentials.get("auth_sessions", 0),
                "push_status": subscription.get("status", "none"),
            }
            if credentials:
                row["access_expires_at"] = _utc_z(credentials["access_expires_ms"])
                row["refresh_expires_at"] = _utc_z(credentials["refresh_expires_ms"])
            if subscription:
                row["registered_at"] = _utc_z(subscription["registered_ms"])
            rows.append(row)
        out = {
            "current_device_id": session.device_id,
            "devices": rows,
            "truncated": len(identifiers) > 200 or len(subscriptions) > 200,
        }
        validate("relaydeck/gateway/v1/devices", out)
        return JSONResponse(out)

    @app.delete("/v1/devices/{device_id}")
    async def delete_device(device_id: str, request: Request) -> JSONResponse:
        session = current_session(request)
        if session is None:
            return error_response("unauthenticated", "missing or expired token")
        if (
            not device_id
            or len(device_id) > push.MAX_DEVICE_ID_CHARS
            or not device_allowed(session, device_id)
        ):
            return error_response("not_found", "no such device")
        # Capture the admitted target before durable storage can fail or deny
        # subsequent credential reads. Never derive teardown from a retry lookup.
        account = session.account
        try:
            revoked_sessions = state.sessions.revoke_device(account, device_id)
            revoked_push = state.push.revoke_device(account, device_id, state.now_ms())
        finally:
            # Both credential and subscription failures must sever writers.
            state.metadata.revoke(account=account, device_id=device_id)
            try:
                state.backend.revoke_leases_device(account, device_id)
            finally:
                await state.sockets.close_device(account, device_id, "unauthenticated")
        if not revoked_sessions and not revoked_push:
            return error_response("not_found", "no such device")
        out = {"ok": True}
        validate("relaydeck/gateway/v1/device-revoke", out)
        return JSONResponse(out)

    @app.post("/v1/deep-links/resolve")
    async def resolve_deep_link(request: Request) -> JSONResponse:
        """Authenticated hint/link resolution: tap/open fetches refs and
        route ids, never content. Forged/dead/expired/foreign links fail
        closed as not_found with no content."""
        session = current_session(request)
        if session is None:
            return error_response("unauthenticated", "missing or expired token")
        try:
            body = await request.json()
        except ValueError:
            return error_response("forbidden", "malformed resolve", details={"reason": "malformed"})
        if not isinstance(body, dict) or set(body) not in ({"token"}, {"notification_id"}):
            return error_response("forbidden", "malformed resolve", details={"reason": "malformed"})
        value = body.get("token", body.get("notification_id", ""))
        if not isinstance(value, str) or not value or len(value) > 256:
            return error_response("forbidden", "malformed resolve", details={"reason": "malformed"})
        if "token" in body:
            resolved = state.push.resolve_link(value, session.account, state.now_ms())
        else:
            resolved = state.push.resolve_hint(session.account, value, state.now_ms())
        if resolved is not None and session.host_binding:
            notification = state.push.get_notification(str(resolved["notification_id"]))
            if (
                notification is None
                or notification["account"] != session.account
                or not device_allowed(session, str(notification["device_id"]))
                or resolved["host_id"] not in session.hosts
            ):
                resolved = None
        if resolved is None:
            return error_response("not_found", "unknown or expired link")
        validate("relaydeck/gateway/v1/deep-link-resolution", resolved)
        return JSONResponse(resolved)

    install_enrollments(
        app,
        state,
        current_session,
        error_response=error_response,
        revoke_host_and_close=revoke_host_and_close,
        utc_z=_utc_z,
    )

    @app.websocket("/v1/connectors")
    async def connector_socket(websocket: WebSocket) -> None:
        # Bearer header preferred (query strings land in access logs);
        # the query form stays for compatibility .
        token = websocket.query_params.get("token", "")
        if not token:
            scheme, _, value = websocket.headers.get("authorization", "").partition(" ")
            if scheme.lower() == "bearer" and value:
                token = value
        record = state.hosts.lookup(token)
        if record is None:
            await websocket.close(code=1008, reason="unauthenticated")
            return
        # Single live tunnel per host (OWASP flood/DoS bound): a new
        # tunnel replaces a stale one, and arrival order on the survivor
        # is the terminal-output order.
        await state.sockets.close_host(record.host_id, "replaced")
        await websocket.accept()
        state.sockets.register_host(record.host_id, websocket, record)
        intake = SnapshotIntake()
        last_epochs = state.backend.epochs_for_host(record.host_id, record.account)

        async def send_hello() -> None:
            await state.sockets.send_text(
                websocket,
                json.dumps(
                    {
                        "kind": "hello",
                        "host_id": record.host_id,
                        "snapshot_chunks": True,
                        "epochs": last_epochs,
                    }
                ),
            )

        async def pump_outbox() -> bool:
            """Drain journaled dispatches down this tunnel. The
            write-ahead marker is journaled BEFORE each send; a marker
            miss skips the send (never re-dispatch, including
            TTL-refused markers, which settle expired), and a send
            failure ends the tunnel (bytes are never silently lost)."""
            nonlocal last_epochs
            if state.hosts.lookup(token) != record:
                await state.sockets.close_socket(websocket, "unauthenticated")
                return False
            epochs = state.backend.epochs_for_host(record.host_id, record.account)
            if epochs != last_epochs:
                try:
                    await state.sockets.send_text(
                        websocket,
                        json.dumps(
                            {
                                "kind": "terminal.epochs",
                                "host_id": record.host_id,
                                "epochs": epochs,
                            }
                        ),
                    )
                except Exception:
                    return False
                last_epochs = epochs
            pending = state.backend.drain_outbox(record.host_id)
            for message in pending:
                if message.get("principal") != record.account or not record.account:
                    LOG.error(
                        "refused cross-account connector dispatch for host %s", record.host_id
                    )
                    continue
                command_id = str(message.get("command_id", ""))
                if not command_id or not state.backend.mark_command_dispatched(
                    command_id, state.now_ms()
                ):
                    emit_command_update(state.backend.command_status(command_id))
                    continue
                emit_command_update(state.backend.command_status(command_id))
                try:
                    await state.sockets.send_text(websocket, json.dumps(message))
                except Exception:
                    return False
            return True

        async def ingest_snapshot(snapshot: dict[str, Any], now_ms: int) -> bool:
            try:
                if len(json.dumps(snapshot).encode()) > MAX_SNAPSHOT_BYTES:
                    return False
                validate("relaydeck/gateway/v1/snapshot", snapshot)
            except (ValueError, TypeError, SchemaError):
                return False
            host = snapshot.get("host", {})
            if host.get("id") != record.host_id:
                return False
            try:
                previous_epoch = state.backend.snapshot(record.host_id).get("host", {}).get("epoch")
            except BackendError:
                previous_epoch = None
            if "ok" not in state.backend.ingest_snapshot(
                record.host_id, snapshot, now_ms, state.cursor_secret
            ):
                return False
            state.notifications.snapshot(record.account, record.host_id, snapshot, now_ms)
            if previous_epoch is not None and previous_epoch != host.get("epoch"):
                reset_doc = json.dumps({"kind": "reset", "detail": "stale_epoch"})
                for lease_id in state.backend.rotate_streams(record.host_id):
                    await state.sockets.notify_lease(lease_id, reset_doc)
                    await state.sockets.close_lease(lease_id, "stale_revision")
            return True

        async def handle_tunnel_doc(doc: dict[str, Any]) -> bool:
            """One connector->gateway message. True keeps the tunnel;
            False fails it closed (OWASP: per-message authorization, not
            just handshake auth)."""
            kind = doc.get("kind")
            now_ms = state.now_ms()
            if kind in ("snapshot.begin", "snapshot.chunk", "snapshot.end"):
                try:
                    snapshot = intake.accept(doc, record.host_id, time.monotonic())
                except (ValueError, UnicodeError):
                    return False
                return snapshot is None or await ingest_snapshot(snapshot, now_ms)
            if intake.active:
                return False
            if kind == "terminal.reset_required":
                if (
                    doc.get("host_id") != record.host_id
                    or not isinstance(doc.get("terminal_id"), str)
                    or not 1 <= len(doc["terminal_id"]) <= 128
                    or not isinstance(doc.get("epoch"), str)
                    or not 1 <= len(doc["epoch"]) <= 128
                ):
                    return False
                dead = state.backend.reset_terminal(
                    record.host_id, doc["terminal_id"], doc["epoch"], record.account
                )
                for lease_id in dead:
                    await state.sockets.notify_lease(
                        lease_id,
                        json.dumps(
                            {
                                "kind": "reset",
                                "detail": "stale_epoch",
                            }
                        ),
                    )
                    await state.sockets.close_lease(lease_id, "stale_revision")
                return True
            if kind == "terminal.input_failed":
                if (
                    doc.get("host_id") != record.host_id
                    or not isinstance(doc.get("lease_id"), str)
                    or type(doc.get("generation")) is not int
                    or type(doc.get("input_seq")) is not int
                    or not 1 <= doc["input_seq"] <= 0xFFFFFFFF
                ):
                    return False
                failed_lease = state.backend.open_lease(doc["lease_id"])
                if failed_lease is None:
                    return True
                if (
                    failed_lease.host_id != record.host_id
                    or failed_lease.owner != record.account
                    or failed_lease.generation != doc["generation"]
                ):
                    return False
                await state.sockets.notify_lease(
                    failed_lease.lease_id,
                    json.dumps(
                        {
                            "kind": "gap",
                            "detail": "input_uncertain",
                            "generation": doc["generation"],
                            "input_seq": doc["input_seq"],
                        }
                    ),
                )
                return True
            if kind == "command.ack":
                if doc.get("host_id") != record.host_id or not isinstance(
                    doc.get("command_id"), str
                ):
                    return False
                command_id = str(doc["command_id"])
                if state.backend.command_owner(command_id) != (record.account, record.host_id):
                    return False
                updated = state.backend.note_command_ack(command_id, record.host_id, now_ms)
                emit_command_update(updated)
                return updated is not None
            if kind == "command.result":
                if (
                    doc.get("host_id") != record.host_id
                    or not isinstance(doc.get("command_id"), str)
                    or doc.get("status") not in ("succeeded", "failed", "rejected", "unknown")
                    or not isinstance(doc.get("result_digest", ""), str)
                    or not isinstance(doc.get("error_code", ""), str)
                    or ("result" in doc and not isinstance(doc["result"], dict))
                ):
                    return False
                if state.backend.command_owner(str(doc["command_id"])) != (
                    record.account,
                    record.host_id,
                ):
                    return False
                updated = state.backend.note_command_result(
                    str(doc["command_id"]),
                    record.host_id,
                    str(doc["status"]),
                    str(doc.get("result_digest", "")),
                    str(doc.get("error_code", "")),
                    doc.get("result"),
                    now_ms,
                )
                emit_command_update(updated)
                return updated is not None
            if kind == "terminal.output":
                data_b64 = doc.get("data_b64", "")
                if (
                    doc.get("host_id") != record.host_id
                    or not isinstance(doc.get("terminal_id"), str)
                    or not doc.get("terminal_id")
                    or not isinstance(doc.get("epoch"), str)
                    or not isinstance(data_b64, str)
                    or not data_b64
                ):
                    return False
                try:
                    data = base64.b64decode(data_b64, validate=True)
                except ValueError:
                    return False
                if len(data) > TUNNEL_MAX_OUTPUT:
                    return False
                # Semantic misses (unknown terminal, rotated epoch) drop
                # the frame but keep the host tunnel: one stale stream
                # must not kill the whole host link.
                state.backend.ingest_terminal_output(
                    record.host_id, str(doc["terminal_id"]), str(doc["epoch"]), data, now_ms
                )
                return True
            if kind == "snapshot":
                # Connector metadata sync: schema-validated,
                # host-bound, replaces. A bad snapshot fails the tunnel
                # closed (never stored, never served).
                snapshot = doc.get("snapshot")
                if doc.get("host_id") != record.host_id or not isinstance(snapshot, dict):
                    return False
                return await ingest_snapshot(snapshot, now_ms)
            if kind == "event":
                # Connector metadata sync: partial event, the
                # server stamps sequence/cursor and dedupes by event_id.
                partial = doc.get("event")
                if doc.get("host_id") != record.host_id or not isinstance(partial, dict):
                    return False
                try:
                    current_epoch = state.backend.snapshot(record.host_id)["host"]["epoch"]
                except (BackendError, KeyError):
                    return False
                if partial.get("host_epoch") != current_epoch:
                    return False
                event_type = partial.get("type")
                if not isinstance(event_type, str):
                    return False
                status_key = {"pane.upserted": "agent_status", "agent.upserted": "status"}.get(
                    event_type
                )
                payload = partial.get("payload")
                if status_key and isinstance(payload, dict) and status_key in payload:
                    value = payload[status_key]
                    if not isinstance(value, str) or value not in {
                        "idle",
                        "working",
                        "blocked",
                        "done",
                        "unknown",
                    }:
                        return False
                result = state.backend.ingest_event(
                    record.host_id, partial, now_ms, state.cursor_secret
                )
                if "ok" not in result:
                    return False
                if not result.get("duplicate"):
                    state.notifications.event(record.account, record.host_id, result["ok"], now_ms)
                return True
            return False

        try:
            await send_hello()
            if not await pump_outbox():
                return
            while True:
                try:
                    intake.check_deadline(time.monotonic())
                except ValueError:
                    await websocket.close(code=1008, reason="forbidden")
                    return
                try:
                    message = await asyncio.wait_for(websocket.receive(), timeout=TUNNEL_POLL_S)
                except TimeoutError:
                    if not await pump_outbox():
                        return
                    continue
                if message["type"] == "websocket.disconnect":
                    break
                if state.hosts.lookup(token) != record:
                    await state.sockets.close_socket(websocket, "unauthenticated")
                    return
                if "text" in message and message["text"] is not None:
                    text = message["text"]
                    # Character count, not bytes: a parse-bomb bound, not a
                    # byte bound (multibyte input may exceed it in bytes).
                    if len(text) > TUNNEL_MAX_TEXT:
                        await websocket.close(code=1008, reason="forbidden")
                        return
                    try:
                        doc = json.loads(text)
                    except ValueError:
                        await websocket.close(code=1008, reason="forbidden")
                        return
                    if not isinstance(doc, dict):
                        await websocket.close(code=1008, reason="forbidden")
                        return
                    if doc.get("kind") == "ping":
                        try:
                            await state.sockets.send_text(websocket, json.dumps({"kind": "pong"}))
                        except Exception:
                            # Revoke/close raced the ping: the tunnel is
                            # over; end it instead of crashing the handler.
                            return
                    elif doc.get("kind") == "bye":
                        break
                    elif not await handle_tunnel_doc(doc):
                        await websocket.close(code=1008, reason="forbidden")
                        return
                    if not await pump_outbox():
                        return
                else:
                    await websocket.close(code=1008, reason="forbidden")
                    return
        except (WebSocketDisconnect, TimeoutError):
            pass
        finally:
            state.sockets.unregister(websocket)
            if not state.sockets.host_count(record.host_id):
                state.notifications.drop_host(record.host_id)

    def _host_guard(session: Any, host_id: str) -> JSONResponse | None:
        if "sessions.read" not in session.capabilities:
            return error_response("unsupported_capability", "sessions.read required")
        if host_id not in session.hosts:
            return error_response("not_found", f"unknown host {host_id}")
        return None

    install_usage(app, state, current_session, _host_guard)

    @app.get("/v1/me")
    async def get_me(request: Request) -> JSONResponse:
        session = current_session(request)
        if session is None:
            return error_response("unauthenticated", "missing or expired token")
        out = {
            "account": session.account,
            "device_id": session.device_id,
            "capabilities": sorted(session.capabilities),
            "hosts": sorted(session.hosts),
            "protocol_version": GATEWAY_PROTOCOL,
        }
        validate("relaydeck/gateway/v1/me", out)
        return JSONResponse(out)

    @app.get("/v1/hosts")
    async def list_hosts(request: Request) -> JSONResponse:
        session = current_session(request)
        if session is None:
            return error_response("unauthenticated", "missing or expired token")
        if "sessions.read" not in session.capabilities:
            return error_response("unsupported_capability", "sessions.read required")
        records = {record.host_id: record for record in state.hosts.hosts()}
        labels = {key: record.label for key, record in records.items()}
        entries = []
        for host_id in sorted(session.hosts):
            try:
                snapshot = state.backend.snapshot(host_id)
            except BackendError:
                entries.append(
                    {
                        "host_id": host_id,
                        "label": labels.get(host_id, host_id),
                        "epoch": "",
                        "reachable": False,
                        "upstream": {"protocol": 0, "compatible": False},
                        **(
                            {"fingerprint": records[host_id].fingerprint}
                            if host_id in records
                            else {}
                        ),
                    }
                )
                continue
            host = snapshot.get("host", {})
            protocol = int(host.get("protocol", 0) or 0)
            entries.append(
                {
                    "host_id": host_id,
                    "label": str(host.get("label", labels.get(host_id, host_id))),
                    "epoch": str(host.get("epoch", "")),
                    "reachable": bool(host.get("reachable", True)),
                    **({"fingerprint": records[host_id].fingerprint} if host_id in records else {}),
                    **({"last_seen_at": host["last_seen_at"]} if "last_seen_at" in host else {}),
                    **({"agent_catalog": host["agent_catalog"]} if "agent_catalog" in host else {}),
                    # last_seen_at is stamped by connector heartbeats; the fake backend
                    # omits it honestly.
                    "upstream": {
                        "protocol": protocol,
                        "version": str(host.get("version", "")),
                        "compatible": protocol == EXPECTED_PROTOCOL,
                    },
                }
            )
        out = {"hosts": entries}
        validate("relaydeck/gateway/v1/hosts", out)
        return JSONResponse(out)

    @app.delete("/v1/hosts/{host_id}")
    async def delete_host(host_id: str, request: Request) -> JSONResponse:
        session = current_session(request)
        if session is None:
            return error_response("unauthenticated", "missing or expired token")
        if "hosts.enroll" not in session.capabilities:
            return error_response("unsupported_capability", "hosts.enroll required")
        record = state.hosts.record_for_host(host_id)
        if record is None or record.account != session.account or host_id not in session.hosts:
            return error_response("not_found", "no such host")
        await revoke_host_and_close(state, host_id)
        out = {"ok": True}
        validate("relaydeck/gateway/v1/host-revoke", out)
        return JSONResponse(out)

    @app.get("/v1/hosts/{host_id}/snapshot")
    async def get_snapshot(host_id: str, request: Request) -> JSONResponse:
        session = current_session(request)
        if session is None:
            return error_response("unauthenticated", "missing or expired token")
        guard = _host_guard(session, host_id)
        if guard is not None:
            return guard
        try:
            snapshot = state.backend.snapshot(host_id)
        except BackendError as exc:
            return error_response(exc.code, exc.message, exc.retryable)
        try:
            validate("relaydeck/gateway/v1/snapshot", snapshot)
        except SchemaError:
            return error_response("upstream_incompatible", "backend snapshot invalid")
        if len(json.dumps(snapshot).encode()) > MAX_SNAPSHOT_BYTES:
            return error_response("too_large", "snapshot too large")
        return JSONResponse(snapshot)

    @app.get("/v1/hosts/{host_id}/agents")
    async def list_agents(host_id: str, request: Request) -> JSONResponse:
        session = current_session(request)
        if session is None:
            return error_response("unauthenticated", "missing or expired token")
        guard = _host_guard(session, host_id)
        if guard is not None:
            return guard
        if set(request.query_params) - {"limit", "page_token", "project", "status", "q"}:
            return error_response("forbidden", "unknown inventory filter")
        try:
            limit = int(request.query_params.get("limit", "50"))
        except ValueError:
            return error_response("forbidden", "bad limit", details={"reason": "malformed"})
        limit = min(max(limit, 1), MAX_AGENTS_PER_PAGE)
        raw_token = request.query_params.get("page_token", "")
        try:
            snapshot = state.backend.snapshot(host_id)
            validate("relaydeck/gateway/v1/snapshot", snapshot)
            out = state.inventory.page(
                account=session.account,
                host_id=host_id,
                snapshot=snapshot,
                events=state.backend.events_after(host_id, 0),
                secret=state.cursor_secret,
                limit=limit,
                token=raw_token,
                project=request.query_params.get("project", ""),
                status=request.query_params.get("status", ""),
                query=request.query_params.get("q", ""),
            )
        except BackendError as exc:
            return error_response(exc.code, exc.message, exc.retryable)
        except SchemaError:
            return error_response("upstream_incompatible", "backend snapshot invalid")
        try:
            validate("relaydeck/gateway/v1/agents-page", out)
        except SchemaError:
            return error_response("upstream_incompatible", "backend agents invalid")
        return JSONResponse(out)

    @app.post("/v1/hosts/{host_id}/agents/{agent_id}/read")
    async def mark_agent_read(host_id: str, agent_id: str, request: Request) -> JSONResponse:
        session = current_session(request)
        if session is None:
            return error_response("unauthenticated", "missing or expired token")
        guard = _host_guard(session, host_id)
        if guard is not None:
            return guard
        try:
            body = await request.json()
        except (ValueError, UnicodeDecodeError):
            return error_response("forbidden", "invalid read marker")
        if (
            not isinstance(body, dict)
            or set(body) != {"host_epoch", "activity_id"}
            or (
                not isinstance(body["host_epoch"], str)
                or not 1 <= len(body["host_epoch"]) <= 128
                or not isinstance(body["activity_id"], str)
                or len(body["activity_id"]) not in (0, 64)
            )
        ):
            return error_response("forbidden", "invalid read marker")
        try:
            snapshot = state.backend.snapshot(host_id)
            validate("relaydeck/gateway/v1/snapshot", snapshot)
            out = state.inventory.mark_read(
                account=session.account,
                host_id=host_id,
                epoch=body["host_epoch"],
                agent_id=agent_id,
                activity_id=body["activity_id"],
                snapshot=snapshot,
                events=state.backend.events_after(host_id, 0),
                secret=state.cursor_secret,
            )
            validate("relaydeck/gateway/v1/agent-read", out)
        except BackendError as exc:
            return error_response(exc.code, exc.message, exc.retryable)
        except SchemaError:
            return error_response("upstream_incompatible", "inventory invalid")
        return JSONResponse(out)

    @app.get("/v1/events")
    async def get_events(request: Request) -> Any:
        session = current_session(request)
        if session is None:
            return error_response("unauthenticated", "missing or expired token")
        host_id = request.query_params.get("host_id", "")
        guard = _host_guard(session, host_id)
        if guard is not None:
            return guard
        raw_cursor = request.query_params.get("cursor", "")
        sequence = 0
        if raw_cursor:
            try:
                payload = decode_cursor(state.cursor_secret, raw_cursor)
            except CursorError:
                return error_response(
                    "stale_epoch", "cursor invalid", True, details={"reason": "cursor"}
                )
            if payload.get("host_id") != host_id:
                return error_response(
                    "stale_epoch", "cursor host mismatch", True, details={"reason": "cursor"}
                )
            boundary = payload.get("sequence", 0)
            if type(boundary) is not int or boundary < 0:
                return error_response(
                    "stale_epoch", "cursor sequence invalid", True, details={"reason": "cursor"}
                )
            sequence = boundary

        def stream_error(
            code: str,
            message: str = "Access changed",
            retryable: bool = False,
            reason: str = "",
        ) -> str:
            body = {"code": code, "message": message, "retryable": retryable}
            if reason:
                body["details"] = {"reason": reason}
            return f"event: error\ndata: {json.dumps(body)}\n\n"

        async def generate() -> Any:
            try:
                key, signal = state.metadata.register(session.account, session.device_id, host_id)
            except RuntimeError:
                yield stream_error("rate_limited", "Metadata stream capacity reached", True)
                return
            iterator = state.backend.follow(host_id, sequence, EVENTS_HEARTBEAT_S).__aiter__()
            pending: asyncio.Future[dict[str, Any] | None] | None = None
            stopped = asyncio.create_task(signal.event.wait())
            expired = asyncio.create_task(
                asyncio.sleep(max(0.001, (session.access_expires_ms - state.now_ms()) / 1000))
            )
            try:
                while True:
                    live = current_session(request)
                    if live is None or host_id not in live.hosts:
                        code = "unauthenticated" if live is None else "not_found"
                        yield stream_error(
                            code,
                            reason=state.sessions.access_failure_reason(
                                _bearer(request), state.now_ms()
                            )
                            if live is None
                            else "host_revoked",
                        )
                        break
                    pending = asyncio.ensure_future(anext(iterator))
                    done, _ = await asyncio.wait(
                        (pending, stopped, expired), return_when=asyncio.FIRST_COMPLETED
                    )
                    if stopped in done:
                        yield stream_error(signal.code, reason="revoked")
                        break
                    if expired in done:
                        yield stream_error("unauthenticated", reason="access_expired")
                        break
                    try:
                        event = pending.result()
                    except StopAsyncIteration:
                        break
                    live = current_session(request)
                    if live is None or host_id not in live.hosts:
                        continue  # Recheck before delivering any newly queued metadata.
                    if event is None:
                        yield ": ping\n\n"
                    else:
                        yield f"event: metadata\ndata: {json.dumps(event)}\n\n"
            except BackendError as exc:
                body = {"code": exc.code, "message": exc.message, "retryable": exc.retryable}
                yield f"event: error\ndata: {json.dumps(body)}\n\n"
            finally:
                state.metadata.unregister(key)
                if pending is not None:
                    pending.cancel()
                stopped.cancel()
                expired.cancel()
                await asyncio.gather(
                    *(task for task in (pending, stopped, expired) if task is not None),
                    return_exceptions=True,
                )
                try:
                    close = getattr(iterator, "aclose", None)
                    if close is not None:
                        await close()
                finally:
                    state.metadata.unregister(key)

        return MetadataResponse(
            generate(),
            media_type="text/event-stream",
            headers={AFTER_SEQUENCE_HEADER: str(sequence)},
        )

    @app.post("/v1/hosts/{host_id}/commands", status_code=202)
    async def post_command(host_id: str, request: Request) -> JSONResponse:
        session = current_session(request)
        if session is None:
            return error_response("unauthenticated", "missing or expired token")
        try:
            body = await request.json()
            validate("relaydeck/gateway/v1/command-submit", body)
        except (ValueError, SchemaError):
            return error_response("forbidden", "malformed command", details={"reason": "malformed"})
        if body["host_id"] != host_id:
            return error_response(
                "forbidden", "path host mismatch", details={"reason": "host_mismatch"}
            )
        if not state.commands.check(session.account, state.now_ms()):
            return error_response("rate_limited", "too many commands", True)
        denied = authorize_command(
            session.capabilities,
            session.hosts,
            body["kind"],
            body["host_id"],
            body["target"],
            body["payload"],
        )
        if denied:
            return error_response(denied, f"command denied: {denied}")
        try:
            snapshot = state.backend.snapshot(host_id)
        except BackendError as exc:
            return error_response(exc.code, exc.message, exc.retryable)
        if snapshot.get("host", {}).get("epoch", "") != body["host_epoch"]:
            return error_response("stale_epoch", "command epoch is stale")
        expires_at = body.get("expires_at")
        expires_ms: int | None = None
        if expires_at is not None:
            expires_ms = _parse_utc_ms(expires_at)
            now_ms = state.now_ms()
            if expires_ms is None or expires_ms <= now_ms:
                return error_response("forbidden", "command expired", details={"reason": "expired"})
            if expires_ms - now_ms > MAX_EXPIRES_FUTURE_MS:
                return error_response(
                    "forbidden", "expiry too far out", details={"reason": "expiry_bound"}
                )
        # The backend checks positive prompt revisions against metadata;
        # dispatch checks the fresh source revision again before execution.
        backend_command = dict(body)
        backend_command["principal"] = session.account
        backend_command["device_id"] = session.device_id
        if expires_ms is not None:
            backend_command["expires_ms"] = expires_ms
        try:
            record = state.backend.submit_command(backend_command)
        except BackendError as exc:
            return error_response(exc.code, exc.message, exc.retryable)
        # session.close: sever downstream sockets of the
        # locally closed attachment (backends without closes drain nothing).
        for lease_id in state.backend.drain_socket_closes():
            await state.sockets.close_lease(lease_id, "lease_expired")
        try:
            validate("relaydeck/gateway/v1/command-record", record)
        except SchemaError:
            return error_response("upstream_incompatible", "backend command invalid")
        emit_command_update(record)
        return JSONResponse(status_code=202, content=record)

    @app.get("/v1/hosts/{host_id}/commands/{command_id}")
    async def get_command(host_id: str, command_id: str, request: Request) -> JSONResponse:
        session = current_session(request)
        if session is None:
            return error_response("unauthenticated", "missing or expired token")
        guard = _host_guard(session, host_id)
        if guard is not None:
            return guard
        if state.backend.command_owner(command_id) != (session.account, host_id):
            return error_response("not_found", "no such command")
        record = state.backend.command_status(command_id)
        if record is None or record.get("host_id") != host_id:
            return error_response("not_found", "no such command")
        emit_command_update(record)
        return JSONResponse(record)

    def _attachment_json(body: Any) -> JSONResponse | dict[str, Any]:
        try:
            validate("relaydeck/gateway/v1/terminal-attach-request", body)
        except SchemaError:
            return error_response(
                "forbidden", "malformed attachment", details={"reason": "malformed"}
            )
        return {"takeover": False, "cols": DEFAULT_PTY_COLS, "rows": DEFAULT_PTY_ROWS, **body}

    @app.post("/v1/hosts/{host_id}/terminals/{terminal_id}/attachments", status_code=201)
    async def create_attachment(host_id: str, terminal_id: str, request: Request) -> JSONResponse:
        """Explicit upstream attach authorization.

        First attach authorizes ownership; a second holder without
        takeover gets 409 lease_conflict with holder disclosure and must
        take over deliberately. Agent attach always takes ownership and
        requires explicit takeover=true (disclosed by the client first).
        """
        session = current_session(request)
        if session is None:
            return error_response("unauthenticated", "missing or expired token")
        guard = _host_guard(session, host_id)
        if guard is not None:
            return guard
        if "terminal.attach" not in session.capabilities:
            return error_response("unsupported_capability", "terminal.attach required")
        if not terminal_id or len(terminal_id) > MAX_TERMINAL_ID_CHARS:
            return error_response("forbidden", "bad terminal", details={"reason": "malformed"})
        try:
            parsed = _attachment_json(await request.json())
        except ValueError:
            return error_response(
                "forbidden", "malformed attachment", details={"reason": "malformed"}
            )
        if isinstance(parsed, JSONResponse):
            return parsed
        try:
            attachment, displaced = state.backend.create_attachment(
                host_id,
                terminal_id,
                parsed["kind"],
                session.account,
                session.device_id,
                takeover=parsed["takeover"],
                owner_label=session.device_id,
                **(
                    {"expected_control_revision": parsed["expected_control_revision"]}
                    if "expected_control_revision" in parsed
                    else {}
                ),
            )
        except BackendError as exc:
            return error_response(exc.code, exc.message, exc.retryable, details=exc.details)
        # Takeover rotates the epoch: displaced viewers get the same
        # reset-doc shape as the resume path (never a forged tail), then
        # the close. The client drives reset+notice+reattach from the doc;
        # a bare close would map to host_offline instead.
        reset_doc = json.dumps({"kind": "reset", "detail": "stale_epoch"})
        for lease_id in displaced:
            await state.sockets.notify_lease(lease_id, reset_doc)
            await state.sockets.close_lease(lease_id, "stale_revision")
        await publish_epochs(host_id)
        out = {
            "attachment_id": attachment.attachment_id,
            "host_id": attachment.host_id,
            "terminal_id": attachment.terminal_id,
            "kind": attachment.kind,
            "generation": attachment.generation,
            "epoch": attachment.epoch,
            "owner": {"device_id": attachment.device_id, "label": attachment.owner_label},
            "takeover_authorized": attachment.takeover_authorized,
        }
        return JSONResponse(status_code=200 if displaced else 201, content=out)

    @app.get("/v1/hosts/{host_id}/terminals/{terminal_id}/control")
    async def get_control_holder(host_id: str, terminal_id: str, request: Request) -> JSONResponse:
        """Read-only: whether a live control lease is held on this terminal and
        whether it is the caller's device (holder disclosed same-account only).
        Lets a client take control automatically only when nobody else holds
        it; it never attaches, issues, renews or displaces anything."""
        session = current_session(request)
        if session is None:
            return error_response("unauthenticated", "missing or expired token")
        guard = _host_guard(session, host_id)
        if guard is not None:
            return guard
        if "terminal.view" not in session.capabilities:
            return error_response("unsupported_capability", "terminal.view required")
        if not terminal_id or len(terminal_id) > MAX_TERMINAL_ID_CHARS:
            return error_response("forbidden", "bad terminal", details={"reason": "malformed"})
        state_doc = state.backend.control_holder(
            host_id, terminal_id, session.account, session.device_id
        )
        return JSONResponse(state_doc)

    @app.post("/v1/terminal-attachments/{attachment_id}/leases", status_code=201)
    async def create_lease(attachment_id: str, request: Request) -> JSONResponse:
        """View an already-owned stream or acquire exclusive control.

        Viewing never authorizes upstream ownership;
        control is one live holder per attachment."""
        session = current_session(request)
        if session is None:
            return error_response("unauthenticated", "missing or expired token")
        if not attachment_id or len(attachment_id) > MAX_ATTACHMENT_ID_CHARS:
            return error_response("not_found", "no such attachment")
        try:
            body = await request.json()
        except ValueError:
            return error_response("forbidden", "malformed lease", details={"reason": "malformed"})
        try:
            validate("relaydeck/gateway/v1/terminal-lease-request", body)
        except SchemaError:
            return error_response("forbidden", "malformed lease", details={"reason": "malformed"})
        mode = body.get("mode")
        if mode == "view" and "expected_generation" in body:
            return error_response("forbidden", "malformed lease", details={"reason": "malformed"})
        attachment = state.backend.get_attachment(attachment_id)
        if attachment is None or attachment.host_id not in session.hosts:
            return error_response("not_found", "no such attachment")
        if attachment.closed:
            return error_response("lease_expired", "attachment closed")
        if mode == "view" and "terminal.view" not in session.capabilities:
            return error_response("unsupported_capability", "terminal.view required")
        if mode == "control" and "terminal.control" not in session.capabilities:
            return error_response("unsupported_capability", "terminal.control required")
        try:
            if mode == "view":
                lease = state.backend.issue_view(attachment_id, session.account, session.device_id)
            else:
                lease = state.backend.acquire_control(
                    attachment_id,
                    session.account,
                    session.device_id,
                    **(
                        {"expected_generation": body["expected_generation"]}
                        if "expected_generation" in body
                        else {}
                    ),
                )
        except BackendError as exc:
            return error_response(exc.code, exc.message, exc.retryable, details=exc.details)
        out = {
            "lease_id": lease.lease_id,
            "attachment_id": lease.attachment_id,
            "host_id": lease.host_id,
            "terminal_id": lease.terminal_id,
            "mode": lease.mode,
            "generation": lease.generation,
            "epoch": lease.epoch,
            "expires_at": _utc_z(lease.expires_ms),
        }
        await publish_epochs(attachment.host_id)
        return JSONResponse(status_code=201, content=out)

    @app.delete("/v1/terminal-attachments/{attachment_id}")
    async def delete_attachment(attachment_id: str, request: Request) -> JSONResponse:
        """Explicit remote close of an upstream attachment.

        Unlike lease release (holder-device-scoped), close is
        account-scoped: the same user may close from any own device.
        Every downstream lease dies as expired and its sockets are
        severed, mirroring the takeover-displacement path.
        """
        session = current_session(request)
        if session is None:
            return error_response("unauthenticated", "missing or expired token")
        if not attachment_id or len(attachment_id) > MAX_ATTACHMENT_ID_CHARS:
            return error_response("not_found", "no such attachment")
        attachment = state.backend.get_attachment(attachment_id)
        if attachment is None or attachment.host_id not in session.hosts:
            return error_response("not_found", "no such attachment")
        if "terminal.control" not in session.capabilities:
            return error_response("unsupported_capability", "terminal.control required")
        try:
            dead = state.backend.close_attachment(attachment_id, session.account)
        except BackendError as exc:
            return error_response(exc.code, exc.message, exc.retryable, details=exc.details)
        for lease_id in dead:
            await state.sockets.close_lease(lease_id, "lease_expired")
        await publish_epochs(attachment.host_id)
        return JSONResponse({"ok": True})

    @app.delete("/v1/terminal-leases/{lease_id}")
    async def delete_lease(lease_id: str, request: Request) -> JSONResponse:
        """Detach one lease without terminating the host process."""
        session = current_session(request)
        if session is None:
            return error_response("unauthenticated", "missing or expired token")
        if not lease_id or len(lease_id) > MAX_LEASE_ID_CHARS:
            return error_response("not_found", "no such lease")
        host_id = state.backend.lease_host(lease_id)
        if host_id is not None and host_id not in session.hosts:
            return error_response("not_found", "no such lease")
        try:
            await release_lease_and_close(state, lease_id, session.account, session.device_id)
        except BackendError as exc:
            return error_response(exc.code, exc.message, exc.retryable, details=exc.details)
        return JSONResponse({"ok": True})

    @app.post("/v1/terminal-leases/{lease_id}/renew")
    async def renew_lease(lease_id: str, request: Request) -> JSONResponse:
        """Foreground renewal: the live holder extends its own lease.
        Only the holding principal+device renews; anyone else gets
        not_found (no holder disclosure)."""
        session = current_session(request)
        if session is None:
            return error_response("unauthenticated", "missing or expired token")
        if not lease_id or len(lease_id) > MAX_LEASE_ID_CHARS:
            return error_response("not_found", "no such lease")
        try:
            body = await request.json()
        except ValueError:
            body = {}
        if not isinstance(body, dict) or set(body) - set():
            return error_response("forbidden", "malformed renew", details={"reason": "malformed"})
        host_id = state.backend.lease_host(lease_id)
        if host_id is not None and host_id not in session.hosts:
            return error_response("not_found", "no such lease")
        try:
            lease = state.backend.renew_lease(lease_id, session.account, session.device_id)
        except BackendError as exc:
            return error_response(exc.code, exc.message, exc.retryable, details=exc.details)
        return JSONResponse({"lease_id": lease.lease_id, "expires_at": _utc_z(lease.expires_ms)})

    @app.post("/v1/terminal-tickets")
    async def issue_terminal_ticket(request: Request) -> JSONResponse:
        session = current_session(request)
        if session is None:
            return error_response("unauthenticated", "missing or expired token")
        try:
            body = await request.json()
            validate("relaydeck/gateway/v1/terminal-ticket-request", body)
        except (ValueError, SchemaError):
            return error_response(
                "forbidden", "malformed ticket request", details={"reason": "malformed"}
            )
        if "terminal.view" not in session.capabilities:
            return error_response("unsupported_capability", "terminal.view required")
        lease = state.backend.open_lease(body["lease_id"])
        if lease is None or lease.host_id not in session.hosts:
            return error_response("lease_expired", "no such lease")
        if lease.owner != session.account:
            # Lease ids are guessable and host ids can rebind across
            # accounts: only the owning account mints. not_found reveals
            # nothing about the foreign lease.
            return error_response("not_found", "no such lease")
        ticket = state.tickets.issue(
            TicketGrant(
                account=session.account,
                device_id=session.device_id,
                capabilities=session.capabilities,
                hosts=session.hosts,
                lease_id=lease.lease_id,
                host_id=lease.host_id,
                generation=lease.generation,
                resume=lease.mode == "control"
                and lease.device_id == session.device_id
                and (
                    lease.resume_pending
                    or state.sockets.has_device_lease(
                        session.account, session.device_id, lease.lease_id
                    )
                ),
            ),
            state.now_ms(),
        )
        out = {"ticket": ticket, "expires_in": TICKET_TTL_MS // 1000}
        validate("relaydeck/gateway/v1/terminal-ticket", out)
        return JSONResponse(out)

    @app.websocket("/v1/terminal/{lease_id}")
    async def terminal_socket(websocket: WebSocket, lease_id: str) -> None:
        ticket = websocket.query_params.get("ticket", "")
        grant = state.tickets.redeem(ticket, lease_id, state.now_ms()) if ticket else None
        if grant is None:
            await websocket.close(code=1008, reason="unauthenticated")
            return
        if "terminal.view" not in grant.capabilities:
            await websocket.close(code=1008, reason="unsupported_capability")
            return
        lease = state.backend.open_lease(lease_id)
        if lease is None or lease.host_id not in grant.hosts or grant.account != lease.owner:
            await websocket.close(code=1008, reason="lease_expired")
            return
        if grant.generation and grant.generation != lease.generation:
            await websocket.close(code=1008, reason="lease_expired")
            return
        resume_params = "epoch" in websocket.query_params or "offset" in websocket.query_params
        resume_epoch = websocket.query_params.get("epoch", "")
        try:
            resume_offset = int(websocket.query_params.get("offset", "0"))
        except ValueError:
            await websocket.close(code=1008, reason="forbidden")
            return
        if resume_offset < 0 or len(resume_epoch) > MAX_LEASE_ID_CHARS:
            await websocket.close(code=1008, reason="forbidden")
            return
        if lease.resume_pending and not (
            resume_params
            and grant.resume
            and grant.generation == lease.generation
            and grant.device_id == lease.device_id
        ):
            await websocket.close(code=1008, reason="lease_expired")
            return
        await websocket.accept()
        trace = LeaseTrace(
            opaque("principal", grant.account, grant.device_id),
            opaque("terminal", lease.host_id, lease.terminal_id),
            lease_id,
            lease.mode,
            lease.generation,
        )
        state.sockets.register(
            grant.account, lease_id, websocket, grant.device_id, diagnostics=trace
        )
        trace.emit("socket_open", reason="ok", source="server", socket=websocket)
        fanout = state.backend.fanout_for(lease.attachment_id)
        viewer_id = str(id(websocket))
        recovery_close = False

        async def close_stream(reason: str) -> None:
            # Reset/revoke can close this socket while receive is suspended.
            # Registry removal claims the close before its first await; do not
            # send a second close or overwrite the first reset/revoke reason.
            if (
                getattr(websocket, "application_state", None) == WebSocketState.DISCONNECTED
                or getattr(websocket, "client_state", None) == WebSocketState.DISCONNECTED
            ):
                return
            await state.sockets.close_socket(websocket, reason)

        async def forward_input(chunk: terminal_wire.InputChunk) -> None:
            record = state.hosts.record_for_host(lease.host_id)
            # FakeBackend remains an explicit synthetic recorder. Production
            # never treats a local input log as delivery to the home host.
            if record is None and not state.backend.requires_connector:
                return
            if record is None:
                raise BackendError("host_offline", "connector unavailable", True)
            if record.account != lease.owner or not record.account:
                raise BackendError("not_found", "no such terminal")
            if not state.sockets.host_count(lease.host_id, record):
                raise BackendError("host_offline", "connector unavailable", True)
            doc: dict[str, Any] = {
                "kind": "terminal.resize" if chunk.kind == "resize" else "terminal.input",
                "host_id": lease.host_id,
                "terminal_id": lease.terminal_id,
                "epoch": lease.epoch,
                "lease_id": lease_id,
                "generation": chunk.generation,
                "input_seq": chunk.input_seq,
            }
            if chunk.kind == "resize":
                doc.update(cols=chunk.cols, rows=chunk.rows)
            else:
                doc.update(input_kind=chunk.kind, data_b64=base64.b64encode(chunk.data).decode())
            parse_terminal_input(doc, lease.host_id)
            if not await state.sockets.notify_host(lease.host_id, json.dumps(doc), binding=record):
                raise BackendError("host_offline", "input delivery uncertain; not replayed", True)

        async def push_pending() -> str | None:
            """Send queued live bytes. Returns None while flowing, else
            the reset detail that must close the socket: an overflowed
            (slow) viewer or a rotated epoch gets an explicit reset doc
            plus close, never silent loss presented as valid bytes."""
            if state.backend.open_lease(lease_id) is None:
                return "lease_expired"
            queue = fanout.viewer(viewer_id) if fanout is not None else None
            if queue is None:
                return None
            if queue.reset_pending or queue.overflowed:
                detail = "stale_epoch" if queue.reset_pending else "overflow"
                try:
                    await state.sockets.send_text(
                        websocket, json.dumps({"kind": "reset", "detail": detail})
                    )
                except Exception:
                    pass
                return detail
            try:
                for offset, data in queue.pop_all():
                    # A queued payload may exceed the 64 KiB wire chunk:
                    # re-chunk with continuous offsets (never fail the
                    # viewer on a large upstream write).
                    for at in range(0, len(data), terminal_wire.MAX_OUTPUT_CHUNK):
                        piece = data[at : at + terminal_wire.MAX_OUTPUT_CHUNK]
                        await state.sockets.send_bytes(
                            websocket, terminal_wire.encode_output(queue.epoch, offset + at, piece)
                        )
            except Exception:
                return "reset"
            return None

        try:
            if fanout is not None:
                # Least-privilege subscription (OWASP): this socket sees
                # only its own lease's attachment stream, and the id is
                # per-connection so co-viewers never share a queue.
                fanout.subscribe(viewer_id)
            if resume_params:
                # ring resume: exact continuation while retained,
                # else an explicit reset doc (never a forged tail).
                try:
                    stream = state.backend.read_stream(lease_id, resume_epoch, resume_offset)
                except BackendError as exc:
                    await close_stream(exc.code)
                    return
                if stream.status != "ok":
                    recovery_close = stream.status in ("stale_epoch", "stale_offset")
                    await state.sockets.send_text(
                        websocket, json.dumps({"kind": "reset", "detail": stream.status})
                    )
                    await close_stream(stream.status)
                    return
                if lease.resume_pending:
                    try:
                        state.backend.finish_reader_handoff(
                            lease_id, grant.account, grant.device_id, grant.generation
                        )
                    except BackendError as exc:
                        await close_stream(exc.code)
                        return
                if grant.resume:
                    state.tickets.discard_resume(lease_id, grant.account, grant.device_id)
                for offset, data in stream.chunks:
                    await state.sockets.send_bytes(
                        websocket, terminal_wire.encode_output(stream.epoch, offset, data)
                    )
            else:
                # Ticket-only first connect replays retained ring bytes like a first-connect resume,
                # so pre-connect publishes (the attach greeting) reach the viewer; the ring is the
                # single backlog authority. Stale reads surface as explicit reset docs, never
                # blanks.
                try:
                    stream = state.backend.read_stream(lease_id, "", 0)
                except BackendError as exc:
                    await close_stream(exc.code)
                    return
                if stream.status != "ok":
                    recovery_close = stream.status in ("stale_epoch", "stale_offset")
                    await state.sockets.send_text(
                        websocket, json.dumps({"kind": "reset", "detail": stream.status})
                    )
                    await close_stream(stream.status)
                    return
                for offset, data in stream.chunks:
                    await state.sockets.send_bytes(
                        websocket, terminal_wire.encode_output(stream.epoch, offset, data)
                    )
            while True:
                try:
                    message = await asyncio.wait_for(
                        websocket.receive(), timeout=TERMINAL_PUSH_POLL_S
                    )
                except TimeoutError:
                    push_detail = await push_pending()
                    if push_detail is not None:
                        recovery_close = push_detail in ("stale_epoch", "overflow", "reset")
                        await close_stream(push_detail)
                        return
                    continue
                if message["type"] == "websocket.disconnect":
                    break
                if "bytes" in message and message["bytes"] is not None:
                    if "terminal.control" not in grant.capabilities:
                        await close_stream("unsupported_capability")
                        return
                    try:
                        chunk = terminal_wire.decode_input(message["bytes"])
                    except ValueError:
                        await close_stream("forbidden")
                        return
                    if chunk.lease_id != lease_id:
                        await close_stream("forbidden")
                        return
                    if chunk.generation > lease.generation:
                        await close_stream("forbidden")
                        return
                    if not state.input_bytes.check(lease_id, state.now_ms(), cost=len(chunk.data)):
                        await state.sockets.send_text(
                            websocket, json.dumps({"kind": "gap", "detail": "rate_limited"})
                        )
                        continue
                    try:
                        fresh = state.backend.write_input(
                            lease_id,
                            chunk.generation,
                            chunk.input_seq,
                            chunk.kind,
                            chunk.data,
                        )
                        if fresh:
                            await forward_input(chunk)
                    except BackendError as exc:
                        await state.sockets.send_text(
                            websocket,
                            json.dumps(
                                {
                                    "kind": "gap"
                                    if exc.code
                                    in ("stale_revision", "host_offline", "rate_limited")
                                    else "bye",
                                    "detail": exc.code,
                                    **(
                                        {
                                            "generation": chunk.generation,
                                            "input_seq": chunk.input_seq,
                                        }
                                        if 1 <= chunk.generation <= 0xFFFFFFFF
                                        and 1 <= chunk.input_seq <= 0xFFFFFFFF
                                        else {}
                                    ),
                                }
                            ),
                        )
                        if exc.code not in ("stale_revision", "host_offline", "rate_limited"):
                            await close_stream(exc.code)
                            return
                elif "text" in message and message["text"] is not None:
                    # Character count, not bytes: a parse-bomb bound, not a
                    # byte bound (multibyte input may exceed it in bytes).
                    if len(message["text"]) > TUNNEL_MAX_TEXT:
                        # Tunnel parity: never parse an unbounded control
                        # string (valid control docs are ~300 bytes).
                        await close_stream("forbidden")
                        return
                    try:
                        control = json.loads(message["text"])
                        validate("relaydeck/gateway/v1/terminal-control", control)
                    except (ValueError, SchemaError):
                        await close_stream("forbidden")
                        return
                    if control["kind"] == "bye":
                        break
                push_detail = await push_pending()
                if push_detail is not None:
                    recovery_close = push_detail in ("stale_epoch", "overflow", "reset")
                    await close_stream(push_detail)
                    return
        except (WebSocketDisconnect, TimeoutError):
            trace.emit("socket_close", reason="receive_failure", source="server", socket=websocket)
        finally:
            # Close hygiene (OWASP): unsubscribe the per-connection viewer
            # on EVERY exit so a dead socket never retains its 1 MiB queue.
            trace.emit("socket_close", reason="stream_closed", source="server", socket=websocket)
            if fanout is not None:
                fanout.unsubscribe(viewer_id)
            state.sockets.unregister(websocket)
            if (
                lease.mode == "control"
                and grant.device_id == lease.device_id
                and not state.sockets.has_device_lease(grant.account, grant.device_id, lease_id)
            ):
                replacement_ticket = state.tickets.has_resume(
                    lease_id, grant.account, grant.device_id, lease.generation, state.now_ms()
                )
                handoff = (
                    recovery_close or replacement_ticket
                ) and state.backend.begin_reader_handoff(
                    lease_id, grant.account, grant.device_id, lease.generation
                )
                if not handoff:
                    with suppress(BackendError):
                        await release_lease_and_close(
                            state, lease_id, grant.account, grant.device_id
                        )

    return app
