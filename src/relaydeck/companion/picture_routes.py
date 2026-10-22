"""Authenticated device uploads; picker grants reveal no stored path."""

from __future__ import annotations

import asyncio
import logging
import secrets
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from relaydeck.companion.pictures import DAY_MS, MAX_BYTES, PictureError
from relaydeck.contracts import paths
from relaydeck.contracts.schemas import SchemaError, validate

logger = logging.getLogger(__name__)


def install(
    app: FastAPI, state: Any, current_session: Any, session_allowed: Any, error: Any
) -> None:
    slots = asyncio.Semaphore(2)

    def private(response: Response) -> Response:
        response.headers["Cache-Control"] = "private, no-store"
        return response

    def authorize(request: Request) -> Any:
        session = current_session(request)
        if session is None:
            raise PictureError("unauthenticated", "Pair with your Mac again.")
        if "attachments.upload" not in session.capabilities:
            raise PictureError("unsupported_capability", "Picture upload permission is required.")
        if state.pictures is None:
            raise PictureError("unsupported_capability", "Picture uploads are unavailable on Mac.")
        return session

    def owned(request: Request, identifier: str) -> Any:
        session = authorize(request)
        record = state.pictures.get(identifier)
        if (
            record is None
            or record.account != session.account
            or record.device_id != session.device_id
            or record.host_id not in session.hosts
            or max(record.deadline, record.expires_ms) <= state.now_ms()
        ):
            raise PictureError("not_found", "Picture upload not found.")
        return record

    @app.post(paths.PICTURES, status_code=201)
    async def begin(request: Request) -> Any:
        try:
            session = authorize(request)
            body = await request.json()
            validate("relaydeck/gateway/v1/picture-request", body)
            host = body["host_id"]
            if host not in session.hosts or (
                state.pictures.host_id and host != state.pictures.host_id
            ):
                raise PictureError("not_found", "Mac not found.")
            access = request.headers.get("authorization", "").split(" ", 1)[-1]
            identifier, record = state.pictures.begin(
                session.account, session.device_id, host, access, state.now_ms()
            )
            # A grant is not presenter authentication. PUT additionally requires
            # this exact live session's bearer (or the validated web adapter).
            url = f"{paths.picture_content(identifier)}?grant={record.secret}"
            result = {"picture_id": identifier, "upload_url": url, "expires_ms": record.deadline}
            validate("relaydeck/gateway/v1/picture-grant", result)
            return private(JSONResponse(result, status_code=201))
        except (ValueError, SchemaError):
            return private(error("forbidden", "Invalid picture request.", status=400))
        except PictureError as exc:
            return private(error(exc.code, exc.message))

    @app.put(paths.PICTURE_CONTENT)
    async def upload(request: Request, identifier: str) -> Any:
        store = state.pictures
        record = store.get(identifier) if store else None
        secret = request.query_params.get("grant", "")
        authorization = request.headers.getlist("authorization")
        scheme, _, access = (authorization[0] if len(authorization) == 1 else "").partition(" ")

        def live() -> bool:
            if not record or scheme.lower() != "bearer" or not access:
                return False
            session = current_session(request)
            with store.lock:
                return bool(
                    secrets.compare_digest(access, record.access)
                    and store.get(identifier) is record
                    and session
                    and session_allowed(session)
                    and session.account == record.account
                    and session.device_id == record.device_id
                    and record.host_id in session.hosts
                    and state.hosts.allowed(session.account, record.host_id)
                    and "attachments.upload" in session.capabilities
                    and record.deadline > state.now_ms()
                )

        if not record or not secrets.compare_digest(secret, record.secret) or not live():
            session = current_session(request) if record else None
            logger.warning(
                "Picture upload refused: record=%s grant=%s bearer=%s session=%s "
                "access=%s device=%s host=%s deadline=%s",
                bool(record),
                record is not None and secrets.compare_digest(secret, record.secret),
                scheme.lower() == "bearer" and bool(access),
                bool(session),
                record is not None
                and bool(access)
                and secrets.compare_digest(access, record.access),
                session is not None
                and record is not None
                and session.device_id == record.device_id,
                session is not None and record is not None and record.host_id in session.hosts,
                record is not None and record.deadline > state.now_ms(),
            )
            return private(
                error("unauthenticated", "Picture upload expired; choose the picture again.")
            )
        if record.status != "pending":
            return private(error("forbidden", "Picture upload was already used."))
        if slots.locked():
            return private(
                error("rate_limited", "Picture uploads are busy; choose the picture again later.")
            )
        await slots.acquire()
        path = ""
        committed = False
        try:
            with store.lock:
                if not live():
                    raise PictureError("unauthenticated", "Picture upload cancelled or expired.")
                if record.status != "pending":
                    return private(error("forbidden", "Picture upload was already used."))
                record.status = "uploading"  # atomic claim, including thread cleanup
            length = request.headers.get("content-length")
            if length is not None and (int(length) < 0 or int(length) > MAX_BYTES):
                raise PictureError("too_large", "Picture must be 10 MB or smaller.")
            data = bytearray()
            async with asyncio.timeout(120):
                async for chunk in request.stream():
                    if len(data) + len(chunk) > MAX_BYTES:
                        raise PictureError("too_large", "Picture must be 10 MB or smaller.")
                    if not live():
                        raise PictureError(
                            "unauthenticated", "Picture upload cancelled or expired."
                        )
                    data.extend(chunk)
                if not live():
                    raise PictureError("unauthenticated", "Picture upload cancelled or expired.")
                # Await worker on cancellation so no orphan file escapes cleanup.
                task = asyncio.create_task(
                    asyncio.to_thread(store.save_with_preview, bytes(data), state.now_ms())
                )
                try:
                    path, preview = await asyncio.shield(task)
                except asyncio.CancelledError:
                    path, _ = await task
                    Path(path).unlink(missing_ok=True)
                    raise
            with store.lock:
                if not live():
                    raise PictureError("unauthenticated", "Picture upload cancelled or expired.")
                record.path, record.status = path, "ready"
                record.preview = preview
                record.expires_ms = state.now_ms() + DAY_MS
                committed = True
            return private(Response(status_code=204))
        except PictureError as exc:
            with store.lock:
                if store.get(identifier) is record:
                    record.status = "failed"
                    record.message = exc.message
            return private(error(exc.code, exc.message))
        except Exception as exc:
            # The type only: an exception's text can carry paths or content.
            logger.error("picture_upload_failed error=%s", type(exc).__name__)
            with store.lock:
                if store.get(identifier) is record:
                    record.status = "failed"
                    record.message = "Picture upload failed; try JPEG or PNG."
            return private(
                error("forbidden", "Picture upload failed; try JPEG or PNG.", status=400)
            )
        finally:
            slots.release()
            if path and not committed:
                Path(path).unlink(missing_ok=True)

    @app.get(paths.PICTURE)
    async def result(request: Request, identifier: str) -> Any:
        try:
            record = owned(request, identifier)
            result = state.pictures.result(identifier, record)
            validate("relaydeck/gateway/v1/picture-result", result)
            return private(JSONResponse(result))
        except PictureError as exc:
            return private(error(exc.code, exc.message))

    @app.delete(paths.PICTURE, status_code=204, response_class=Response, response_model=None)
    async def cancel(request: Request, identifier: str) -> Any:
        try:
            owned(request, identifier)
            state.pictures.cancel(identifier)
            return private(Response(status_code=204))
        except PictureError as exc:
            return private(error(exc.code, exc.message))
