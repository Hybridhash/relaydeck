"""Commands routes."""

from __future__ import annotations

from typing import cast

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from relaydeck.contracts import paths
from relaydeck.contracts.schemas import CommandSubmit, SchemaError, validate
from relaydeck.gateway.backend import BackendError
from relaydeck.gateway.bounds import MAX_EXPIRES_FUTURE_MS
from relaydeck.gateway.command_io import command_io
from relaydeck.gateway.context import RouteContext
from relaydeck.gateway.deps import HostReader, SignedIn
from relaydeck.gateway.errors import error_response
from relaydeck.gateway.http import parse_utc_ms as _parse_utc_ms
from relaydeck.gateway.policy import authorize_command


def build_router(ctx: RouteContext) -> APIRouter:
    router = APIRouter()
    state = ctx.state
    _host_guard = ctx.host_guard
    emit_command_update = ctx.emit_command_update

    @router.post(paths.HOST_COMMANDS, status_code=202)
    async def post_command(host_id: str, request: Request, session: SignedIn) -> JSONResponse:
        try:
            body = await request.json()
            validate("relaydeck/gateway/v1/command-submit", body)
            submit = cast(CommandSubmit, body)
        except (ValueError, SchemaError):
            return error_response("forbidden", "malformed command", details={"reason": "malformed"})
        if submit["host_id"] != host_id:
            return error_response(
                "forbidden", "path host mismatch", details={"reason": "host_mismatch"}
            )
        if not state.commands.check(session.account, state.now_ms()):
            return error_response("rate_limited", "too many commands", True)
        denied = authorize_command(
            session.capabilities,
            session.hosts,
            submit["kind"],
            submit["host_id"],
            submit["target"],
            submit["payload"],
        )
        if denied:
            return error_response(denied, f"command denied: {denied}")
        try:
            snapshot = state.backend.snapshot(host_id)
        except BackendError as exc:
            return error_response(exc.code, exc.message, exc.retryable)
        if snapshot.get("host", {}).get("epoch", "") != submit["host_epoch"]:
            return error_response("stale_epoch", "command epoch is stale")
        expires_at = submit.get("expires_at")
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
        backend_command = dict(submit)
        backend_command["principal"] = session.account
        backend_command["device_id"] = session.device_id
        if expires_ms is not None:
            backend_command["expires_ms"] = expires_ms
        try:
            record = await command_io(state.backend, "submit_command", backend_command)
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

    @router.get(paths.HOST_COMMAND)
    async def get_command(host_id: str, command_id: str, session: HostReader) -> JSONResponse:
        if await command_io(state.backend, "command_owner", command_id) != (
            session.account,
            host_id,
        ):
            return error_response("not_found", "no such command")
        record = await command_io(state.backend, "command_status", command_id)
        if record is None or record.get("host_id") != host_id:
            return error_response("not_found", "no such command")
        emit_command_update(record)
        return JSONResponse(record)

    return router
