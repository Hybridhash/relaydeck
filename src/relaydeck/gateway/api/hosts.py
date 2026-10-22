"""Hosts routes."""

from __future__ import annotations

import json
from typing import Annotated, cast

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from relaydeck.connector.herdr import EXPECTED_PROTOCOL
from relaydeck.contracts import paths
from relaydeck.contracts.schemas import (
    GATEWAY_PROTOCOL,
    HostRevoke,
    Hosts,
    HostsHostsItem,
    Me,
    SchemaError,
    validate,
)
from relaydeck.gateway.backend import BackendError
from relaydeck.gateway.bounds import MAX_SNAPSHOT_BYTES
from relaydeck.gateway.context import RouteContext
from relaydeck.gateway.deps import HostReader, SignedIn, require_scope
from relaydeck.gateway.errors import error_response
from relaydeck.gateway.identity import Session
from relaydeck.gateway.revocation import revoke_host_and_close

ReadSession = Annotated[Session, Depends(require_scope("sessions.read"))]
EnrollSession = Annotated[Session, Depends(require_scope("hosts.enroll"))]


def build_router(ctx: RouteContext) -> APIRouter:
    router = APIRouter()
    state = ctx.state

    @router.get(paths.ME)
    async def get_me(session: SignedIn) -> JSONResponse:
        out: Me = {
            "account": session.account,
            "device_id": session.device_id,
            "capabilities": sorted(session.capabilities),
            "hosts": sorted(session.hosts),
            "protocol_version": GATEWAY_PROTOCOL,
        }
        validate("relaydeck/gateway/v1/me", out)
        return JSONResponse(out)

    @router.get(paths.HOSTS)
    async def list_hosts(session: ReadSession) -> JSONResponse:
        records = {record.host_id: record for record in state.hosts.hosts()}
        labels = {key: record.label for key, record in records.items()}
        entries: list[HostsHostsItem] = []
        for host_id in sorted(session.hosts):
            try:
                snapshot = state.backend.snapshot(host_id)
            except BackendError:
                entries.append(
                    cast(
                        HostsHostsItem,
                        {
                            "host_id": host_id,
                            "label": labels.get(host_id, host_id),
                            "epoch": "",
                            "reachable": False,
                            "upstream": {"protocol": 0, "compatible": False},
                            **(
                                {"fingerprint": records[host_id].fingerprint}
                                if host_id in records
                                else {}
                            ),
                        },
                    )
                )
                continue
            host = snapshot.get("host", {})
            protocol = int(host.get("protocol", 0) or 0)
            entries.append(
                cast(
                    HostsHostsItem,
                    {
                        "host_id": host_id,
                        "label": str(host.get("label", labels.get(host_id, host_id))),
                        "epoch": str(host.get("epoch", "")),
                        "reachable": bool(host.get("reachable", True)),
                        **(
                            {"fingerprint": records[host_id].fingerprint}
                            if host_id in records
                            else {}
                        ),
                        **(
                            {"last_seen_at": host["last_seen_at"]} if "last_seen_at" in host else {}
                        ),
                        **(
                            {"agent_catalog": host["agent_catalog"]}
                            if "agent_catalog" in host
                            else {}
                        ),
                        # Connector heartbeats stamp last_seen_at; the fake backend has none.
                        "upstream": {
                            "protocol": protocol,
                            "version": str(host.get("version", "")),
                            "compatible": protocol == EXPECTED_PROTOCOL,
                        },
                    },
                )
            )
        out: Hosts = {"hosts": entries}
        validate("relaydeck/gateway/v1/hosts", out)
        return JSONResponse(out)

    @router.delete(paths.HOST)
    async def delete_host(host_id: str, session: EnrollSession) -> JSONResponse:
        record = state.hosts.record_for_host(host_id)
        if record is None or record.account != session.account or host_id not in session.hosts:
            return error_response("not_found", "no such host")
        await revoke_host_and_close(state, host_id)
        out: HostRevoke = {"ok": True}
        validate("relaydeck/gateway/v1/host-revoke", out)
        return JSONResponse(out)

    @router.get(paths.HOST_SNAPSHOT)
    async def get_snapshot(host_id: str, session: HostReader) -> JSONResponse:
        try:
            snapshot = state.backend.snapshot(host_id)
        except BackendError as exc:
            return error_response(exc.code, exc.message, exc.retryable)
        try:
            validate("relaydeck/gateway/v1/snapshot", snapshot)
        except SchemaError:
            return error_response("upstream_incompatible", "backend snapshot invalid")
        if len(json.dumps(snapshot).encode()) > MAX_SNAPSHOT_BYTES:
            return error_response("too_large", "snapshot too large")
        return JSONResponse(snapshot)

    return router
