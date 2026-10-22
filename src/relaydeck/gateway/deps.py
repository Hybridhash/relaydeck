"""Sign-in resolution shared by the gateway routers: the `SignedIn` dependency."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import TYPE_CHECKING, Annotated

from fastapi import Depends, Request

from relaydeck.gateway.errors import GatewayError
from relaydeck.gateway.http import bearer_token
from relaydeck.gateway.identity import Session
from relaydeck.offload import run_blocking

if TYPE_CHECKING:
    from relaydeck.gateway.app import GatewayState


def durable_session_allowed(state: GatewayState, session: Session) -> bool:
    if not session.host_binding:
        return True
    if len(session.hosts) != 1:
        return False
    record = state.hosts.record_for_host(next(iter(session.hosts)))
    return bool(
        record and record.account == session.account and record.token_prefix == session.host_binding
    )


def _granted(state: GatewayState, session: Session | None) -> Session | None:
    if session is None or not durable_session_allowed(state, session):
        return None
    hosts = session.hosts
    if not session.host_binding:
        hosts |= state.hosts.owned_ids(session.account)
    return replace(
        session,
        hosts=frozenset(h for h in hosts if state.hosts.allowed(session.account, h)),
    )


def resolve_session(state: GatewayState, request: Request) -> Session | None:
    token = bearer_token(request)
    if not token:
        return None
    return _granted(state, state.sessions.lookup(token, state.now_ms()))


async def require_session(request: Request) -> Session:
    state: GatewayState = request.app.state.relaydeck
    token = bearer_token(request)
    found = None
    if token:
        # A durable lookup reads the database, so it runs off the event loop; the
        # host checks after it read loop-owned state and stay on the loop.
        if state.sessions.persistence is None:
            found = state.sessions.lookup(token, state.now_ms())
        else:
            found = await run_blocking(state.sessions.lookup, token, state.now_ms())
    session = _granted(state, found)
    if session is None:
        raise GatewayError("unauthenticated", "missing or expired token")
    return session


SignedIn = Annotated[Session, Depends(require_session)]


def scope_denial(session: Session, scope: str) -> GatewayError | None:
    if scope not in session.capabilities:
        return GatewayError("unsupported_capability", f"{scope} required")
    return None


def host_denial(session: Session, host_id: str) -> GatewayError | None:
    # One rule for the host routes: read access first, then ownership of the host.
    if (denied := scope_denial(session, "sessions.read")) is not None:
        return denied
    if host_id not in session.hosts:
        return GatewayError("not_found", f"unknown host {host_id}")
    return None


def require_scope(scope: str) -> Callable[..., Awaitable[Session]]:
    async def dependency(session: SignedIn) -> Session:
        if (denied := scope_denial(session, scope)) is not None:
            raise denied
        return session

    return dependency


async def require_host(host_id: str, session: SignedIn) -> Session:
    if (denied := host_denial(session, host_id)) is not None:
        raise denied
    return session


HostReader = Annotated[Session, Depends(require_host)]
