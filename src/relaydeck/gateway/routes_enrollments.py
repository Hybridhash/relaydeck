"""Connector enrollment routes: create an invitation, poll it, and redeem it with a key proof."""

from __future__ import annotations

import base64
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, cast

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from relaydeck.connector.pubkey import (
    build_qr_payload,
    fingerprint_of_raw,
    openssh_to_raw,
    raw_to_openssh,
    verify_proof,
)
from relaydeck.contracts import paths
from relaydeck.contracts.schemas import (
    Enrollment,
    EnrollmentCreate,
    EnrollmentRedeem,
    EnrollmentResult,
    EnrollmentStatus,
    SchemaError,
    validate,
)
from relaydeck.gateway.http import NO_STORE

if TYPE_CHECKING:
    from relaydeck.gateway.app import GatewayState
    from relaydeck.gateway.identity import Session


def install_enrollments(
    app: FastAPI,
    state: GatewayState,
    current_session: Callable[[Request], Session | None],
    *,
    error_response: Callable[..., JSONResponse],
    revoke_host_and_close: Callable[[GatewayState, str], Awaitable[Any]],
    utc_z: Callable[[int], str],
) -> None:
    @app.post(paths.ENROLLMENTS, status_code=201)
    async def create_enrollment(request: Request) -> JSONResponse:
        session = current_session(request)
        if session is None:
            return error_response("unauthenticated", "missing or expired token")
        if "hosts.enroll" not in session.capabilities:
            return error_response("unsupported_capability", "hosts.enroll required")
        try:
            body = await request.json()
            validate("relaydeck/gateway/v1/enrollment-create", body)
            create = cast(EnrollmentCreate, body)
        except (ValueError, SchemaError):
            return error_response(
                "forbidden", "malformed enrollment", details={"reason": "malformed"}
            )
        try:
            raw = openssh_to_raw(create["pubkey"])
        except ValueError:
            return error_response("forbidden", "bad connector pubkey", details={"reason": "pubkey"})
        canonical = raw_to_openssh(raw)
        invitation = state.enrollments.create(
            session.account,
            create["host_label"],
            canonical,
            fingerprint_of_raw(raw),
            state.now_ms(),
            create.get("ttl_seconds", 600) * 1000,
        )
        expires_at = utc_z(invitation.expires_ms)
        out: Enrollment = {
            "id": invitation.id,
            "code": invitation.code,
            "qr": build_qr_payload(state.public_origin, invitation.id, invitation.code, expires_at),
            "fingerprint": invitation.fingerprint,
            "expires_at": expires_at,
            "host_label": invitation.host_label,
        }
        validate("relaydeck/gateway/v1/enrollment", out)
        return JSONResponse(status_code=201, content=out, headers=NO_STORE)

    @app.get(paths.ENROLLMENT)
    async def enrollment_status(enrollment_id: str, request: Request) -> JSONResponse:
        session = current_session(request)
        if session is None:
            return error_response("unauthenticated", "missing or expired token")
        status = state.enrollments.status(enrollment_id, session.account, state.now_ms())
        if status is None:
            return error_response("not_found", "no such enrollment")
        out: EnrollmentStatus = {"id": enrollment_id, "status": status}
        validate("relaydeck/gateway/v1/enrollment-status", out)
        return JSONResponse(out, headers=NO_STORE)

    @app.post(paths.ENROLLMENT_REDEEM)
    async def redeem_enrollment(enrollment_id: str, request: Request) -> JSONResponse:
        try:
            body = await request.json()
            validate("relaydeck/gateway/v1/enrollment-redeem", body)
            redeem = cast(EnrollmentRedeem, body)
        except (ValueError, SchemaError):
            return error_response("forbidden", "malformed redeem", details={"reason": "malformed"})
        invitation = state.enrollments.attempt(enrollment_id, redeem["code"], state.now_ms())
        if invitation is None:
            return error_response("not_found", "no such enrollment")
        try:
            raw = openssh_to_raw(redeem["pubkey"])
        except ValueError:
            return error_response("forbidden", "bad connector pubkey", details={"reason": "pubkey"})
        if raw_to_openssh(raw) != invitation.pubkey:
            return error_response("forbidden", "key mismatch", details={"reason": "fingerprint"})
        try:
            padded = redeem["proof"] + "=" * (-len(redeem["proof"]) % 4)
            signature = base64.urlsafe_b64decode(padded)
        except ValueError:
            return error_response("forbidden", "bad proof", details={"reason": "proof"})
        if len(signature) != 64 or not verify_proof(
            invitation.pubkey, enrollment_id, redeem["host_id"], signature
        ):
            return error_response("forbidden", "bad proof", details={"reason": "proof"})
        owner = state.hosts.owner(redeem["host_id"])
        if owner is not None and owner != invitation.account:
            return error_response("not_found", "host unavailable")
        if not state.enrollments.claim(enrollment_id):
            return error_response("not_found", "no such enrollment")
        if state.hosts.record_for_host(redeem["host_id"]) is not None:
            await revoke_host_and_close(state, redeem["host_id"])
        token = state.hosts.enroll(
            redeem["host_id"],
            invitation.host_label,
            invitation.account,
            invitation.pubkey,
            invitation.fingerprint,
        )
        out: EnrollmentResult = {
            "host_id": redeem["host_id"],
            "host_token": token,
            "fingerprint": invitation.fingerprint,
        }
        validate("relaydeck/gateway/v1/enrollment-result", out)
        return JSONResponse(out, headers=NO_STORE)
