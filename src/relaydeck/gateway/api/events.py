"""Events routes."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import APIRouter, Request

from relaydeck.contracts import paths
from relaydeck.contracts.cursors import AFTER_SEQUENCE_HEADER
from relaydeck.gateway.backend import BackendError
from relaydeck.gateway.bounds import EVENTS_HEARTBEAT_S, SHUTDOWN_POLL_S
from relaydeck.gateway.context import RouteContext
from relaydeck.gateway.cursors import CursorError, decode_cursor
from relaydeck.gateway.deps import SignedIn
from relaydeck.gateway.errors import error_response
from relaydeck.gateway.http import bearer_token as _bearer
from relaydeck.gateway.metadata_streams import MetadataResponse


def build_router(ctx: RouteContext) -> APIRouter:
    router = APIRouter()
    state = ctx.state
    current_session = ctx.current_session
    _host_guard = ctx.host_guard

    @router.get(paths.EVENTS)
    async def get_events(request: Request, session: SignedIn) -> Any:
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

        # Only a gateway that was serving can stop serving (tests may skip the lifespan).
        serving = state.started

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
                    while True:
                        done, _ = await asyncio.wait(
                            (pending, stopped, expired),
                            timeout=SHUTDOWN_POLL_S,
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        if done or (serving and not state.started):
                            break
                    if not done:
                        break  # The gateway is shutting down: end the stream.
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

    return router
