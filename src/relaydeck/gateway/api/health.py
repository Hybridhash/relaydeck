"""Health and readiness routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from relaydeck.gateway.context import RouteContext


def build_router(ctx: RouteContext) -> APIRouter:
    router = APIRouter()
    state = ctx.state

    @router.get("/healthz")
    async def healthz(request: Request) -> dict[str, Any]:
        worker = getattr(request.app.state, "push_worker", None)
        return {
            "ok": True,
            "version": "1.0.0",
            "push": {
                "configured": state.push_provider is not None,
                "enqueue_error": state.notifications.error,
                "delivery_error": worker.error if worker else "",
            },
        }

    @router.get("/readyz")
    async def readyz() -> JSONResponse:
        connected = sum(1 for host in state.hosts.hosts() if state.sockets.host_count(host.host_id))
        ready = state.started and connected > 0
        return JSONResponse(
            {"ready": ready, "hosts_connected": connected}, status_code=200 if ready else 503
        )

    return router
