"""Gateway HTTP/WebSocket application.

All routes authenticate via bearer token, enforce capability policy,
validate bodies against `relaydeck.contracts.schemas`, and return the
shared error envelope. Terminal streaming rides a WebSocket with the
binary framing in `terminal_wire`; metadata rides SSE with HMAC cursors.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Callable
from contextlib import AsyncExitStack, asynccontextmanager, suppress
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from relaydeck.contracts import paths
from relaydeck.contracts.schemas import (
    ERROR_STATUS,
    validate,
)
from relaydeck.gateway import push
from relaydeck.gateway.authorization import (
    AuthorizationFlow,
    IdentityProvider,
)
from relaydeck.gateway.backend import Backend, BackendError
from relaydeck.gateway.bounds import (
    MAX_JSON_BODY,
)
from relaydeck.gateway.context import RouteContext
from relaydeck.gateway.deps import durable_session_allowed, host_denial, resolve_session
from relaydeck.gateway.errors import error_response, install_error_handlers
from relaydeck.gateway.hosts import HostStore, drain_revoke_requests
from relaydeck.gateway.http import docs_settings
from relaydeck.gateway.http import utc_z as _utc_z
from relaydeck.gateway.identity import (
    EnrollmentStore,
    Session,
    SessionStore,
    TicketStore,
)
from relaydeck.gateway.inventory import InventoryView
from relaydeck.gateway.loop_watch import LoopWatch
from relaydeck.gateway.metadata_streams import MetadataStreams
from relaydeck.gateway.metrics import Metrics, install_metrics
from relaydeck.gateway.notification_transitions import NotificationTransitions, Status
from relaydeck.gateway.pairing import PairingStore
from relaydeck.gateway.policy import (
    command_limiter,
    input_limiter,
)
from relaydeck.gateway.push import ProviderPort
from relaydeck.gateway.push_io import push_io
from relaydeck.gateway.request_id import install_request_id
from relaydeck.gateway.revocation import revoke_host_and_close
from relaydeck.gateway.routes import (
    include_connector_routes,
    include_host_routes,
    include_routes,
    include_terminal_routes,
    include_terminal_socket,
)
from relaydeck.gateway.routes_enrollments import install_enrollments
from relaydeck.gateway.sockets import SocketRegistry
from relaydeck.gateway.terminal_epochs import (
    publish_terminal_routes,
    reconcile_terminal_routes,
)
from relaydeck.gateway.usage import UsageService
from relaydeck.gateway.usage import install as install_usage
from relaydeck.offload import run_blocking


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

        if scope.get("method") == "PUT" and paths.PICTURE_CONTENT_RE.fullmatch(
            scope.get("path", "")
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
    debug_docs: bool = False  # serve /docs, /redoc and /openapi.json (off in production)
    started: bool = False  # set by the lifespan while the app is serving
    metrics: Metrics = field(default_factory=Metrics)
    web_tabs: Callable[[], int] | None = None  # the web app's live tab count (set by serve.py)

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

    async def emit_notification_async(self, status: Status, category: str) -> None:
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
            await run_blocking(
                self.web_push_sink,
                status.account,
                devices,
                status.event_id,
                category,
                status.host,
                status.session,
            )
        if devices:
            await push_io(
                self.push,
                "notify",
                account=status.account,
                account_hosts=frozenset({status.host}),
                host_id=status.host,
                session_id=status.session,
                event_id=status.event_id,
                category=category,
                now=self.now_ms(),
                device_ids=devices,
            )


def create_app(state: GatewayState) -> FastAPI:
    async def drain_spool() -> None:
        while True:
            for host_id in await run_blocking(drain_revoke_requests, state.revoke_spool):
                await revoke_host_and_close(state, host_id)
            await asyncio.sleep(max(0.01, state.revoke_poll_s))

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # Every background task is registered with the stack as soon as it exists, so a failure
        # while starting a later one still cancels and awaits the earlier ones.
        async with AsyncExitStack() as stack:
            stack.push_async_callback(close_identity_provider)
            if state.pictures is not None:
                from relaydeck.companion.pictures import run_picture_cleanup

                await asyncio.to_thread(state.pictures.cleanup, state.now_ms())
                picture_task = asyncio.create_task(
                    run_picture_cleanup(state.pictures, state.now_ms)
                )
                stack.push_async_callback(cancel_task, picture_task)
            if state.revoke_spool:
                stack.push_async_callback(cancel_task, asyncio.create_task(drain_spool()))
            from relaydeck.gateway.push_worker import PushWorker

            worker = (
                PushWorker(state.push, state.push_provider, authorized=state.push_delivery_allowed)
                if state.push_provider is not None
                else None
            )
            app.state.push_worker = worker
            if worker is not None:
                stack.push_async_callback(worker.stop)
                worker.start()
            notification_stop = asyncio.Event()
            notification_task = asyncio.create_task(
                state.notifications.run(
                    notification_stop, state.now_ms, state.emit_notification_async
                )
            )
            stack.push_async_callback(stop_notifications, notification_stop, notification_task)
            terminal_task = asyncio.create_task(reconcile_terminal_routes(state))
            stack.push_async_callback(cancel_task, terminal_task)

            def on_beat(lag: float) -> None:
                state.metrics.record_loop_delay(lag)
                state.metrics.sample_sockets(state.sockets.terminal_count())

            monitor = LoopWatch(on_beat=on_beat)
            monitor.start()
            stack.push_async_callback(monitor.close)
            stack.callback(set_started, False)
            state.started = True
            yield

    async def cancel_task(task: asyncio.Task[Any]) -> None:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    async def stop_notifications(stop: asyncio.Event, task: asyncio.Task[Any]) -> None:
        stop.set()
        await task

    async def close_identity_provider() -> None:
        close = getattr(state.identity_provider, "close", None)
        if close is not None:
            await close()

    def set_started(value: bool) -> None:
        state.started = value

    docs = docs_settings(state.debug_docs)
    app = FastAPI(title="RelayDeck gateway", version="1.0.0", lifespan=lifespan, **docs)
    app.state.relaydeck = state
    app.add_middleware(BodyCapMiddleware)

    install_request_id(app)
    install_metrics(app, state.metrics)
    install_error_handlers(app)

    def session_allowed(session: Session) -> bool:
        return durable_session_allowed(state, session)

    def current_session(request: Request) -> Session | None:
        return resolve_session(state, request)

    def device_allowed(session: Session, device_id: str) -> bool:
        # Paired Console grants are not account-wide device administration.
        return not session.host_binding or device_id == session.device_id

    from relaydeck.companion.picture_routes import install as install_pictures

    install_pictures(app, state, current_session, session_allowed, error_response)

    async def publish_epochs(host_id: str) -> None:
        await publish_terminal_routes(state, host_id)

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

    def _host_guard(session: Any, host_id: str) -> JSONResponse | None:
        denied = host_denial(session, host_id)
        if denied is None:
            return None
        return error_response(denied.code, denied.message)

    ctx = RouteContext(
        state=state,
        current_session=current_session,
        session_allowed=session_allowed,
        device_allowed=device_allowed,
        publish_epochs=publish_epochs,
        emit_command_update=emit_command_update,
        host_guard=_host_guard,
    )
    include_routes(app, ctx)

    install_enrollments(
        app,
        state,
        current_session,
        error_response=error_response,
        revoke_host_and_close=revoke_host_and_close,
        utc_z=_utc_z,
    )

    include_connector_routes(app, ctx)

    install_usage(app, state, current_session, _host_guard)
    include_host_routes(app, ctx)
    include_terminal_routes(app, ctx)

    include_terminal_socket(app, ctx)

    return app
