"""Shared handles the per-area routers receive from `create_app`."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fastapi import Request
    from fastapi.responses import JSONResponse

    from relaydeck.gateway.app import GatewayState
    from relaydeck.gateway.identity import Session


@dataclass(frozen=True)
class RouteContext:
    state: GatewayState
    current_session: Callable[[Request], Session | None]
    session_allowed: Callable[[Session], bool]
    device_allowed: Callable[[Session, str], bool]
    publish_epochs: Callable[[str], Awaitable[None]]
    emit_command_update: Callable[[dict[str, Any] | None], None]
    host_guard: Callable[[Any, str], JSONResponse | None]
