"""Terminal attachment, control, lease and ticket routes."""

from __future__ import annotations

import json
from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from relaydeck.contracts import paths
from relaydeck.contracts.schemas import (
    SchemaError,
    TerminalLeaseRequest,
    TerminalTicket,
    TerminalTicketRequest,
    validate,
)
from relaydeck.gateway.backend import BackendError
from relaydeck.gateway.bounds import (
    DEFAULT_PTY_COLS,
    DEFAULT_PTY_ROWS,
    MAX_ATTACHMENT_ID_CHARS,
    MAX_LEASE_ID_CHARS,
    MAX_TERMINAL_ID_CHARS,
)
from relaydeck.gateway.context import RouteContext
from relaydeck.gateway.deps import HostReader, SignedIn, require_scope
from relaydeck.gateway.errors import error_response
from relaydeck.gateway.http import NO_STORE
from relaydeck.gateway.http import utc_z as _utc_z
from relaydeck.gateway.identity import TICKET_TTL_MS, Session, TicketGrant
from relaydeck.gateway.terminal_epochs import release_lease_and_close

ViewScope = Annotated[Session, Depends(require_scope("terminal.view"))]
AttachScope = Annotated[Session, Depends(require_scope("terminal.attach"))]


def build_router(ctx: RouteContext) -> APIRouter:
    router = APIRouter()
    state = ctx.state
    publish_epochs = ctx.publish_epochs

    def _attachment_json(body: Any) -> JSONResponse | dict[str, Any]:
        try:
            validate("relaydeck/gateway/v1/terminal-attach-request", body)
        except SchemaError:
            return error_response(
                "forbidden", "malformed attachment", details={"reason": "malformed"}
            )
        return {"takeover": False, "cols": DEFAULT_PTY_COLS, "rows": DEFAULT_PTY_ROWS, **body}

    @router.post(paths.TERMINAL_ATTACHMENTS, status_code=201)
    async def create_attachment(
        host_id: str,
        terminal_id: str,
        request: Request,
        session: HostReader,
        _scope: AttachScope,
    ) -> JSONResponse:
        """Explicit upstream attach authorization.

        First attach authorizes ownership; a second holder without
        takeover gets 409 lease_conflict with holder disclosure and must
        take over deliberately. Agent attach always takes ownership and
        requires explicit takeover=true (disclosed by the client first).
        """
        if not terminal_id or len(terminal_id) > MAX_TERMINAL_ID_CHARS:
            return error_response("forbidden", "bad terminal", details={"reason": "malformed"})
        try:
            parsed = _attachment_json(await request.json())
        except ValueError:
            return error_response(
                "forbidden", "malformed attachment", details={"reason": "malformed"}
            )
        if isinstance(parsed, JSONResponse):
            return parsed
        try:
            attachment, displaced = state.backend.create_attachment(
                host_id,
                terminal_id,
                parsed["kind"],
                session.account,
                session.device_id,
                takeover=parsed["takeover"],
                owner_label=session.device_id,
                **(
                    {"expected_control_revision": parsed["expected_control_revision"]}
                    if "expected_control_revision" in parsed
                    else {}
                ),
            )
        except BackendError as exc:
            return error_response(exc.code, exc.message, exc.retryable, details=exc.details)
        # Takeover rotates the epoch: displaced viewers get the same
        # reset-doc shape as the resume path (never a forged tail), then
        # the close. The client drives reset+notice+reattach from the doc;
        # a bare close would map to host_offline instead.
        reset_doc = json.dumps({"kind": "reset", "detail": "stale_epoch"})
        for lease_id in displaced:
            await state.sockets.notify_lease(lease_id, reset_doc)
            await state.sockets.close_lease(lease_id, "stale_revision")
        await publish_epochs(host_id)
        out = {
            "attachment_id": attachment.attachment_id,
            "host_id": attachment.host_id,
            "terminal_id": attachment.terminal_id,
            "kind": attachment.kind,
            "generation": attachment.generation,
            "epoch": attachment.epoch,
            "owner": {"device_id": attachment.device_id, "label": attachment.owner_label},
            "takeover_authorized": attachment.takeover_authorized,
        }
        return JSONResponse(status_code=200 if displaced else 201, content=out)

    @router.get(paths.TERMINAL_CONTROL)
    async def get_control_holder(
        host_id: str, terminal_id: str, session: HostReader, _scope: ViewScope
    ) -> JSONResponse:
        """Read-only: whether a live control lease is held on this terminal and
        whether it is the caller's device (holder disclosed same-account only).
        Lets a client take control automatically only when nobody else holds
        it; it never attaches, issues, renews or displaces anything."""
        if not terminal_id or len(terminal_id) > MAX_TERMINAL_ID_CHARS:
            return error_response("forbidden", "bad terminal", details={"reason": "malformed"})
        state_doc = state.backend.control_holder(
            host_id, terminal_id, session.account, session.device_id
        )
        return JSONResponse(state_doc)

    @router.post(paths.ATTACHMENT_LEASES, status_code=201)
    async def create_lease(attachment_id: str, request: Request, session: SignedIn) -> JSONResponse:
        """View an already-owned stream or acquire exclusive control.

        Viewing never authorizes upstream ownership;
        control is one live holder per attachment."""
        if not attachment_id or len(attachment_id) > MAX_ATTACHMENT_ID_CHARS:
            return error_response("not_found", "no such attachment")
        try:
            body = await request.json()
        except ValueError:
            return error_response("forbidden", "malformed lease", details={"reason": "malformed"})
        try:
            validate("relaydeck/gateway/v1/terminal-lease-request", body)
            lease_request = cast(TerminalLeaseRequest, body)
        except SchemaError:
            return error_response("forbidden", "malformed lease", details={"reason": "malformed"})
        mode = lease_request.get("mode")
        if mode == "view" and "expected_generation" in lease_request:
            return error_response("forbidden", "malformed lease", details={"reason": "malformed"})
        attachment = state.backend.get_attachment(attachment_id)
        if attachment is None or attachment.host_id not in session.hosts:
            return error_response("not_found", "no such attachment")
        if attachment.closed:
            return error_response("lease_expired", "attachment closed")
        if mode == "view" and "terminal.view" not in session.capabilities:
            return error_response("unsupported_capability", "terminal.view required")
        if mode == "control" and "terminal.control" not in session.capabilities:
            return error_response("unsupported_capability", "terminal.control required")
        try:
            if mode == "view":
                lease = state.backend.issue_view(attachment_id, session.account, session.device_id)
            else:
                lease = state.backend.acquire_control(
                    attachment_id,
                    session.account,
                    session.device_id,
                    **(
                        {"expected_generation": lease_request["expected_generation"]}
                        if "expected_generation" in lease_request
                        else {}
                    ),
                )
        except BackendError as exc:
            return error_response(exc.code, exc.message, exc.retryable, details=exc.details)
        out = {
            "lease_id": lease.lease_id,
            "attachment_id": lease.attachment_id,
            "host_id": lease.host_id,
            "terminal_id": lease.terminal_id,
            "mode": lease.mode,
            "generation": lease.generation,
            "epoch": lease.epoch,
            "expires_at": _utc_z(lease.expires_ms),
        }
        await publish_epochs(attachment.host_id)
        return JSONResponse(status_code=201, content=out)

    @router.delete(paths.ATTACHMENT)
    async def delete_attachment(attachment_id: str, session: SignedIn) -> JSONResponse:
        """Explicit remote close of an upstream attachment.

        Unlike lease release (holder-device-scoped), close is
        account-scoped: the same user may close from any own device.
        Every downstream lease dies as expired and its sockets are
        severed, mirroring the takeover-displacement path.
        """
        if not attachment_id or len(attachment_id) > MAX_ATTACHMENT_ID_CHARS:
            return error_response("not_found", "no such attachment")
        attachment = state.backend.get_attachment(attachment_id)
        if attachment is None or attachment.host_id not in session.hosts:
            return error_response("not_found", "no such attachment")
        if "terminal.control" not in session.capabilities:
            return error_response("unsupported_capability", "terminal.control required")
        try:
            dead = state.backend.close_attachment(attachment_id, session.account)
        except BackendError as exc:
            return error_response(exc.code, exc.message, exc.retryable, details=exc.details)
        for lease_id in dead:
            await state.sockets.close_lease(lease_id, "lease_expired")
        await publish_epochs(attachment.host_id)
        return JSONResponse({"ok": True})

    @router.delete(paths.LEASE)
    async def delete_lease(lease_id: str, session: SignedIn) -> JSONResponse:
        """Detach one lease without terminating the host process."""
        if not lease_id or len(lease_id) > MAX_LEASE_ID_CHARS:
            return error_response("not_found", "no such lease")
        host_id = state.backend.lease_host(lease_id)
        if host_id is not None and host_id not in session.hosts:
            return error_response("not_found", "no such lease")
        try:
            await release_lease_and_close(state, lease_id, session.account, session.device_id)
        except BackendError as exc:
            return error_response(exc.code, exc.message, exc.retryable, details=exc.details)
        return JSONResponse({"ok": True})

    @router.post(paths.LEASE_RENEW)
    async def renew_lease(lease_id: str, request: Request, session: SignedIn) -> JSONResponse:
        """Foreground renewal: the live holder extends its own lease.
        Only the holding principal+device renews; anyone else gets
        not_found (no holder disclosure)."""
        if not lease_id or len(lease_id) > MAX_LEASE_ID_CHARS:
            return error_response("not_found", "no such lease")
        try:
            body = await request.json()
        except ValueError:
            body = {}
        if not isinstance(body, dict) or set(body) - set():
            return error_response("forbidden", "malformed renew", details={"reason": "malformed"})
        host_id = state.backend.lease_host(lease_id)
        if host_id is not None and host_id not in session.hosts:
            return error_response("not_found", "no such lease")
        try:
            lease = state.backend.renew_lease(lease_id, session.account, session.device_id)
        except BackendError as exc:
            return error_response(exc.code, exc.message, exc.retryable, details=exc.details)
        return JSONResponse({"lease_id": lease.lease_id, "expires_at": _utc_z(lease.expires_ms)})

    @router.post(paths.TERMINAL_TICKETS)
    async def issue_terminal_ticket(request: Request, session: SignedIn) -> JSONResponse:
        try:
            body = await request.json()
            validate("relaydeck/gateway/v1/terminal-ticket-request", body)
            ticket_request = cast(TerminalTicketRequest, body)
        except (ValueError, SchemaError):
            return error_response(
                "forbidden", "malformed ticket request", details={"reason": "malformed"}
            )
        if "terminal.view" not in session.capabilities:
            return error_response("unsupported_capability", "terminal.view required")
        lease = state.backend.open_lease(ticket_request["lease_id"])
        if lease is None or lease.host_id not in session.hosts:
            return error_response("lease_expired", "no such lease")
        if lease.owner != session.account:
            # Lease ids are guessable and host ids can rebind across
            # accounts: only the owning account mints. not_found reveals
            # nothing about the foreign lease.
            return error_response("not_found", "no such lease")
        ticket = state.tickets.issue(
            TicketGrant(
                account=session.account,
                device_id=session.device_id,
                capabilities=session.capabilities,
                hosts=session.hosts,
                lease_id=lease.lease_id,
                host_id=lease.host_id,
                generation=lease.generation,
                resume=lease.mode == "control"
                and lease.device_id == session.device_id
                and (
                    lease.resume_pending
                    or state.sockets.has_device_lease(
                        session.account, session.device_id, lease.lease_id
                    )
                ),
            ),
            state.now_ms(),
        )
        out: TerminalTicket = {"ticket": ticket, "expires_in": TICKET_TTL_MS // 1000}
        validate("relaydeck/gateway/v1/terminal-ticket", out)
        return JSONResponse(out, headers=NO_STORE)

    return router
