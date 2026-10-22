"""Every `/v1/` gateway route needs a sign-in dependency unless it is named here, so a new
unauthenticated route can never land silently."""

from __future__ import annotations

from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute

from relaydeck.gateway.app import GatewayState, create_app
from relaydeck.gateway.backend import FakeBackend
from relaydeck.gateway.deps import require_session

Route = tuple[str, str]

PUBLIC: set[Route] = {
    ("POST", "/v1/pairing/requests"),
    ("POST", "/v1/pairing/{request_id}/status"),
    ("POST", "/v1/pairing/{request_id}/redeem"),
    ("GET", "/v1/authorize"),
    ("GET", "/v1/identity/callback"),
    ("POST", "/v1/token"),
    ("POST", "/v1/enrollments/{enrollment_id}/redeem"),
}
# Routes that authenticate by hand inside the handler, with the reason.
MANUAL: dict[Route, str] = {
    ("POST", "/v1/logout"): "reads the bearer token itself to revoke exactly that session",
    ("POST", "/v1/enrollments"): "own sign-in and capability check in routes_enrollments",
    ("GET", "/v1/enrollments/{enrollment_id}"): "own sign-in check in routes_enrollments",
    ("GET", "/v1/hosts/{host_id}/usage"): "own sign-in and host guard in usage",
    ("POST", "/v1/hosts/{host_id}/usage/limits/refresh"): "own sign-in and host guard in usage",
    ("POST", "/v1/pictures"): "own sign-in and attachments.upload check in picture_routes",
    ("PUT", "/v1/pictures/{identifier}/content"): "own sign-in check in picture_routes",
    ("GET", "/v1/pictures/{identifier}"): "own sign-in check in picture_routes",
    ("DELETE", "/v1/pictures/{identifier}"): "own sign-in check in picture_routes",
}


def _requires_sign_in(dependant: Dependant) -> bool:
    return dependant.call is require_session or any(
        _requires_sign_in(child) for child in dependant.dependencies
    )


def test_every_gateway_route_requires_sign_in_or_is_listed() -> None:
    state = GatewayState(
        backend=FakeBackend(now_ms=lambda: 1_000_000),
        cursor_secret=b"x" * 32,
        now_ms=lambda: 1_000_000,
    )
    exempt = PUBLIC | set(MANUAL)
    unprotected = {
        (method, route.path)
        for route in create_app(state).routes
        if isinstance(route, APIRoute) and route.path.startswith("/v1/")
        for method in sorted(route.methods)
        if not _requires_sign_in(route.dependant) and (method, route.path) not in exempt
    }
    assert not unprotected, f"routes without a sign-in dependency: {sorted(unprotected)}"
