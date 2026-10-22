"""Server health: a numbers-only snapshot for the owner's Settings page."""

from __future__ import annotations

from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from relaydeck.contracts import paths
from relaydeck.contracts.schemas import ServerMetrics, validate
from relaydeck.gateway.context import RouteContext
from relaydeck.gateway.deps import require_scope
from relaydeck.gateway.http import NO_STORE
from relaydeck.gateway.identity import Session

HealthReader = Annotated[Session, Depends(require_scope("server.health"))]


def live_counts(state: Any) -> dict[str, Any]:
    """Read at request time from the stores that own the numbers, so nothing can drift."""
    sockets = state.sockets
    ownership = getattr(state.backend, "ownership", None)
    control = view = held_control = held_view = 0
    if ownership is not None:
        control, view = ownership.live_mode_counts()
        for lease_id, count in sockets.lease_socket_counts().items():
            mode = ownership.lease_mode(lease_id)
            held_control += count if mode == "control" else 0
            held_view += count if mode is not None and mode != "control" else 0
    refresh = state.usage.refresh_attempts() if state.usage is not None else []
    return {
        "open": sockets.terminal_count(),
        "controlling": held_control,
        "viewing": held_view,
        "connections": {
            "terminal_sockets": sockets.terminal_count(),
            "host_tunnels": sockets.host_tunnel_count(),
            "leases_control": control,
            "leases_view": view,
            "event_streams": len(state.metadata),
            "web_tabs": state.web_tabs() if state.web_tabs is not None else 0,
        },
        "usage_refresh": [
            {"kind": kind, "finished_ms": finished, "result": result}
            for kind, finished, result in refresh
        ],
    }


def build_router(ctx: RouteContext) -> APIRouter:
    router = APIRouter()
    state = ctx.state

    @router.get(paths.METRICS)
    async def server_metrics(session: HealthReader) -> JSONResponse:
        out = cast(ServerMetrics, state.metrics.snapshot(live_counts(state)))
        validate("relaydeck/gateway/v1/server-metrics", out)
        return JSONResponse(out, headers=NO_STORE)

    return router
