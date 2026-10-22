"""The gateway's one error envelope: code, message, retryable and optional details."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from relaydeck.contracts.schemas import ERROR_STATUS, validate
from relaydeck.gateway.request_id import HEADER, SCOPE_KEY, current_request_id
from relaydeck.persistence_contracts import StorageUnavailable

LOG = logging.getLogger("relaydeck.gateway.errors")


def error_response(
    code: str,
    message: str,
    retryable: bool = False,
    *,
    status: int | None = None,
    details: dict[str, Any] | None = None,
) -> JSONResponse:
    body: dict[str, Any] = {"code": code, "message": message, "retryable": retryable}
    if details is not None:
        body["details"] = details
    validate("relaydeck/gateway/v1/error", body)
    return JSONResponse(status_code=status or ERROR_STATUS[code], content=body)


class GatewayError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        retryable: bool = False,
        *,
        status: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.status = status
        self.details = details


def _request_id(request: Request) -> str:
    return str(request.scope.get(SCOPE_KEY) or current_request_id())


def _route(request: Request) -> str:
    # The route template, never the raw URL (it can carry ids and query strings).
    return str(getattr(request.scope.get("route"), "path", "-"))


def _log_failure(request: Request, code: str, status: int) -> None:
    LOG.log(
        logging.ERROR if status >= 500 else logging.INFO,
        "gateway_error code=%s status=%s method=%s route=%s request_id=%s",
        code,
        status,
        request.method,
        _route(request),
        _request_id(request),
    )


async def gateway_error_handler(request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, GatewayError):
        raise exc
    response = error_response(
        exc.code, exc.message, exc.retryable, status=exc.status, details=exc.details
    )
    _log_failure(request, exc.code, response.status_code)
    return response


async def validation_error_handler(request: Request, exc: Exception) -> JSONResponse:
    # Neither the body nor the validation messages are logged or returned.
    response = error_response("forbidden", "malformed request", details={"reason": "malformed"})
    _log_failure(request, "forbidden", response.status_code)
    return response


async def storage_unavailable_handler(request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, StorageUnavailable):
        raise exc
    response = error_response(
        "unauthenticated",
        "Protected sign-in storage unavailable",
        status=503,
        details={"reason": "storage_unavailable", "storage_reason": exc.code},
    )
    _log_failure(request, "unauthenticated", response.status_code)
    return response


async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    LOG.exception(
        "unhandled_error method=%s route=%s request_id=%s",
        request.method,
        _route(request),
        _request_id(request),
    )
    response = error_response("internal", "Unexpected server error", status=500)
    # This handler runs outside the request-ID middleware, so it sets the header itself.
    response.headers[HEADER] = _request_id(request)
    return response


def install_error_handlers(app: FastAPI) -> None:
    """Every app serving gateway routes registers the same handlers."""
    app.add_exception_handler(GatewayError, gateway_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.add_exception_handler(StorageUnavailable, storage_unavailable_handler)
    app.add_exception_handler(Exception, unhandled_error_handler)
