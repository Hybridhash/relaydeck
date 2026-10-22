"""Device subscription and deep-link routes."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from relaydeck.contracts import paths
from relaydeck.contracts.schemas import (
    DeviceRegistration,
    DeviceRevoke,
    Devices,
    DevicesDevicesItem,
    validate,
)
from relaydeck.gateway import push
from relaydeck.gateway.context import RouteContext
from relaydeck.gateway.deps import SignedIn
from relaydeck.gateway.errors import error_response
from relaydeck.gateway.http import utc_z as _utc_z
from relaydeck.gateway.push_io import push_io
from relaydeck.offload import run_blocking


def build_router(ctx: RouteContext) -> APIRouter:
    router = APIRouter()
    state = ctx.state
    device_allowed = ctx.device_allowed

    # Device subscriptions and deep links: bodies are validated inline; replies are versioned
    # dicts plus the shared error envelope.
    @router.post(paths.DEVICES, status_code=201)
    async def register_device(request: Request, session: SignedIn) -> JSONResponse:
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
            record = await push_io(
                state.push,
                "register_device",
                session.account,
                body["device_id"],
                body["token"],
                body["platform"],
                state.now_ms(),
            )
        except push.PushDenied as exc:
            return error_response(exc.code, str(exc) or "device refused")
        out: DeviceRegistration = {
            "device_id": record.device_id,
            "platform": record.platform,
            "status": record.status,
        }
        validate("relaydeck/gateway/v1/device-registration", out)
        return JSONResponse(status_code=201, content=out)

    @router.post(paths.DEVICE_ROTATE)
    async def rotate_device_token(
        device_id: str, request: Request, session: SignedIn
    ) -> JSONResponse:
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
            record = await push_io(
                state.push,
                "rotate_token",
                session.account,
                device_id,
                body["token"],
                state.now_ms(),
            )
        except push.PushDenied as exc:
            return error_response(exc.code, str(exc) or "rotation refused")
        if record is None:
            return error_response("not_found", "no such device")
        return JSONResponse({"device_id": record.device_id, "status": record.status})

    @router.get(paths.DEVICES)
    async def list_devices(session: SignedIn) -> JSONResponse:
        now = state.now_ms()
        auth = state.sessions.device_metadata(session.account, now)
        subscriptions = {
            row["device_id"]: row
            for row in await push_io(state.push, "device_metadata", session.account)
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
        rows: list[DevicesDevicesItem] = []
        for device_id in identifiers[:200]:
            credentials = auth.get(device_id, {})
            subscription = subscriptions.get(device_id, {})
            active = credentials.get("access_expires_ms", 0) >= now
            status: Literal["active", "refresh_only", "notifications_only", "revoked"] = (
                ("active" if active else "refresh_only")
                if credentials
                else ("notifications_only" if subscription.get("status") == "active" else "revoked")
            )
            row: DevicesDevicesItem = {
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
        out: Devices = {
            "current_device_id": session.device_id,
            "devices": rows,
            "truncated": len(identifiers) > 200 or len(subscriptions) > 200,
        }
        validate("relaydeck/gateway/v1/devices", out)
        return JSONResponse(out)

    @router.delete(paths.DEVICE)
    async def delete_device(device_id: str, session: SignedIn) -> JSONResponse:
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
            if state.sessions.persistence is None:
                revoked_sessions = state.sessions.revoke_device(account, device_id)
            else:
                # Durable revocation writes the database; the store lock keeps it thread-safe.
                revoked_sessions = await run_blocking(
                    state.sessions.revoke_device, account, device_id
                )
            state.metrics.count_sign_in("revoked", revoked_sessions)
            revoked_push = await push_io(
                state.push, "revoke_device", account, device_id, state.now_ms()
            )
        finally:
            # Both credential and subscription failures must sever writers.
            state.metadata.revoke(account=account, device_id=device_id)
            try:
                state.backend.revoke_leases_device(account, device_id)
            finally:
                await state.sockets.close_device(account, device_id, "unauthenticated")
        if not revoked_sessions and not revoked_push:
            return error_response("not_found", "no such device")
        out: DeviceRevoke = {"ok": True}
        validate("relaydeck/gateway/v1/device-revoke", out)
        return JSONResponse(out)

    @router.post(paths.DEEP_LINK_RESOLVE)
    async def resolve_deep_link(request: Request, session: SignedIn) -> JSONResponse:
        """Authenticated hint/link resolution: tap/open fetches refs and
        route ids, never content. Forged/dead/expired/foreign links fail
        closed as not_found with no content."""
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
            resolved = await push_io(
                state.push, "resolve_link", value, session.account, state.now_ms()
            )
        else:
            resolved = await push_io(
                state.push, "resolve_hint", session.account, value, state.now_ms()
            )
        if resolved is not None and session.host_binding:
            notification = await push_io(
                state.push, "get_notification", str(resolved["notification_id"])
            )
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

    return router
