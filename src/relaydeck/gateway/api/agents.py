"""Agents routes."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from relaydeck.contracts import paths
from relaydeck.contracts.schemas import SchemaError, validate
from relaydeck.gateway.backend import BackendError
from relaydeck.gateway.bounds import MAX_AGENTS_PER_PAGE
from relaydeck.gateway.context import RouteContext
from relaydeck.gateway.deps import HostReader
from relaydeck.gateway.errors import error_response


def build_router(ctx: RouteContext) -> APIRouter:
    router = APIRouter()
    state = ctx.state

    @router.get(paths.HOST_AGENTS)
    async def list_agents(host_id: str, request: Request, session: HostReader) -> JSONResponse:
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

    @router.post(paths.AGENT_READ)
    async def mark_agent_read(
        host_id: str, agent_id: str, request: Request, session: HostReader
    ) -> JSONResponse:
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

    return router
