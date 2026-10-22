"""Request and time helpers shared by the gateway routers."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI, Request

# Secret-bearing replies are never cached.
NO_STORE = {"Cache-Control": "no-store"}


def bearer_token(request: Request) -> str:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return ""
    return token


def utc_z(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_utc_ms(value: str) -> int | None:
    try:
        moment = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        try:
            moment = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
        except ValueError:
            return None
    return int(moment.timestamp() * 1000)


def docs_settings(debug: bool) -> dict[str, Any]:
    """FastAPI's /docs, /redoc and /openapi.json exist only while debugging."""
    return {} if debug else {"docs_url": None, "redoc_url": None, "openapi_url": None}


def remove_docs(app: FastAPI) -> None:
    """Drop the docs routes from an app built by a library that offers no switch (Flet's)."""
    hidden = {app.docs_url, app.redoc_url, app.openapi_url, app.swagger_ui_oauth2_redirect_url}
    app.router.routes = [r for r in app.router.routes if getattr(r, "path", None) not in hidden]
    app.docs_url = app.redoc_url = app.openapi_url = None
