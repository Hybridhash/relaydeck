"""Device pairing routes."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from relaydeck.contracts import paths
from relaydeck.contracts.schemas import SchemaError, TokenResponse, validate
from relaydeck.gateway.context import RouteContext
from relaydeck.gateway.errors import error_response
from relaydeck.gateway.pairing import PairingError
from relaydeck.offload import run_blocking


def build_router(ctx: RouteContext) -> APIRouter:
    router = APIRouter()
    state = ctx.state

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

    @router.post(paths.PAIRING_REQUESTS, status_code=201)
    async def request_pairing(request: Request) -> JSONResponse:
        try:
            body = await pairing_body(request, "relaydeck/gateway/v1/pairing-request")
            assert state.pairing is not None
            result = await run_blocking(
                state.pairing.request,
                body,
                state.hosts.record_for_host(body["host_id"]),
                state.now_ms(),
            )
            return pairing_response(result, 201)
        except PairingError as exc:
            return error_response(exc.code, str(exc))

    @router.post(paths.PAIRING_STATUS)
    async def pairing_status(request_id: str, request: Request) -> JSONResponse:
        try:
            body = await pairing_body(request, "relaydeck/gateway/v1/pairing-proof")
            assert state.pairing is not None
            result = await run_blocking(
                state.pairing.poll,
                request_id,
                body,
                state.hosts.record_for_host(body["host_id"]),
                state.now_ms(),
            )
            return pairing_response(result)
        except PairingError as exc:
            return error_response(exc.code, str(exc))

    @router.post(paths.PAIRING_REDEEM)
    async def redeem_pairing(request_id: str, request: Request) -> JSONResponse:
        try:
            body = await pairing_body(request, "relaydeck/gateway/v1/pairing-proof")
            assert state.pairing is not None
            grant = await run_blocking(
                state.pairing.redeem,
                request_id,
                body,
                state.hosts.record_for_host(body["host_id"]),
                state.now_ms(),
            )
        except PairingError as exc:
            return error_response(exc.code, str(exc))
        # Cancellation burns the consumed approval; it never retries or issues a credential.
        # There is no further await before session creation; recheck the host after the worker.
        host = state.hosts.record_for_host(grant.host_id)
        if not host or host.account != grant.account or host.token_prefix != grant.host_binding:
            return error_response("not_found", "Pairing unavailable; start again")
        access, refresh = state.sessions.create(
            grant.account,
            grant.capabilities,
            (grant.host_id,),
            state.now_ms(),
            grant.device_id,
            host_binding=grant.host_binding,
        )
        out: TokenResponse = {"access_token": access, "refresh_token": refresh, "expires_in": 900}
        validate("relaydeck/gateway/v1/token-response", out)
        return JSONResponse(out, headers={"Cache-Control": "no-store"})

    return router
