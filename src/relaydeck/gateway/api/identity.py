"""Sign-in routes: authorize, identity callback, token exchange and logout."""

from __future__ import annotations

import secrets
from typing import Any, cast

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse

from relaydeck.contracts import paths
from relaydeck.contracts.schemas import SchemaError, TokenExchange, TokenResponse, validate
from relaydeck.gateway.authorization import CODE_TTL_MS, FLOW_LIMIT, AuthorizationError
from relaydeck.gateway.context import RouteContext
from relaydeck.gateway.errors import error_response
from relaydeck.gateway.http import NO_STORE
from relaydeck.gateway.http import bearer_token as _bearer
from relaydeck.gateway.push_io import push_io
from relaydeck.offload import run_blocking


def build_router(ctx: RouteContext) -> APIRouter:
    router = APIRouter()
    state = ctx.state
    session_allowed = ctx.session_allowed

    @router.get(paths.AUTHORIZE)
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

    @router.get(paths.IDENTITY_CALLBACK)
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

    @router.post(paths.TOKEN)
    async def token_exchange(request: Request) -> JSONResponse:
        try:
            body = await request.json()
            validate("relaydeck/gateway/v1/token-exchange", body)
            exchange = cast(TokenExchange, body)
        except (ValueError, SchemaError):
            return error_response(
                "forbidden", "malformed token request", details={"reason": "malformed"}
            )
        if exchange["grant"] == "refresh_token":
            presented, now = exchange.get("refresh_token", ""), state.now_ms()
            if state.sessions.persistence is None:
                pair = state.sessions.refresh(presented, now, allowed=session_allowed)
            else:
                # Rotation writes the database. The store lock serialises it with revocation.
                pair = await run_blocking(
                    state.sessions.refresh, presented, now, allowed=session_allowed
                )
            if pair is None:
                state.metrics.count_sign_in("rejected")
                return error_response("unauthenticated", "refresh rejected")
            state.metrics.count_sign_in("refreshes")
            access, refresh = pair
            out: TokenResponse = {
                "access_token": access,
                "refresh_token": refresh,
                "expires_in": 900,
            }
            validate("relaydeck/gateway/v1/token-response", out)
            return JSONResponse(out, headers=NO_STORE)
        code = exchange.get("code", "")
        pending = state.pending_codes.pop(code, None)
        if pending is None:
            return error_response("unauthenticated", "code unknown or reused")
        if pending.get("expires_ms", state.now_ms() + 1) <= state.now_ms():
            return error_response("unauthenticated", "authorization code expired")
        from relaydeck.gateway.identity import pkce_verify

        if not pkce_verify(exchange.get("verifier", ""), pending["challenge"]):
            return error_response("unauthenticated", "PKCE mismatch")
        access, refresh = state.sessions.create(
            pending["account"],
            pending["capabilities"],
            pending["hosts"],
            state.now_ms(),
        )
        out = {"access_token": access, "refresh_token": refresh, "expires_in": 900}
        validate("relaydeck/gateway/v1/token-response", out)
        return JSONResponse(out, headers=NO_STORE)

    @router.post(paths.LOGOUT)
    async def logout_session(request: Request) -> Any:
        token = _bearer(request)
        if not token:
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
                    await push_io(
                        state.push, "revoke_device", local.account, local.device_id, state.now_ms()
                    )
            if dead is None:
                return error_response("unauthenticated", "missing or expired token")
        if dead is not None:
            state.metrics.count_sign_in("revoked")
            if state.sessions.persistence is None:
                state.metadata.revoke(account=dead.account, device_id=dead.device_id)
                state.backend.revoke_leases_device(dead.account, dead.device_id)
                await state.sockets.close_device(dead.account, dead.device_id, "unauthenticated")
            # logout removes the bound push subscription and its
            # token association (the client also DELETEs its install id,
            # which may differ from the session device id).
            if state.sessions.persistence is None:
                await push_io(
                    state.push, "revoke_device", dead.account, dead.device_id, state.now_ms()
                )
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
                await push_io(
                    state.push, "revoke_device_by_ref", dead.account, logout_ref, state.now_ms()
                )
        return {"ok": True}

    return router
