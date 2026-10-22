"""Register the per-area routers, in the order their routes were registered before."""

from __future__ import annotations

from fastapi import FastAPI

from relaydeck.gateway.api import (
    agents,
    commands,
    devices,
    events,
    health,
    hosts,
    identity,
    metrics,
    pairing,
    sockets,
    terminals,
)
from relaydeck.gateway.context import RouteContext


def include_routes(app: FastAPI, ctx: RouteContext) -> None:
    app.include_router(health.build_router(ctx))
    app.include_router(pairing.build_router(ctx))
    app.include_router(identity.build_router(ctx))
    app.include_router(devices.build_router(ctx))
    app.include_router(metrics.build_router(ctx))


def include_host_routes(app: FastAPI, ctx: RouteContext) -> None:
    app.include_router(hosts.build_router(ctx))
    app.include_router(agents.build_router(ctx))
    app.include_router(events.build_router(ctx))
    app.include_router(commands.build_router(ctx))


def include_terminal_routes(app: FastAPI, ctx: RouteContext) -> None:
    app.include_router(terminals.build_router(ctx))


def include_connector_routes(app: FastAPI, ctx: RouteContext) -> None:
    app.include_router(sockets.build_connector_router(ctx))


def include_terminal_socket(app: FastAPI, ctx: RouteContext) -> None:
    app.include_router(sockets.build_terminal_socket_router(ctx))
