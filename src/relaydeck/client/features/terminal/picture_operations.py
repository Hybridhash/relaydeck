"""One picture-attachment operation, shared by every source that can supply a picture.

A source (the file picker today, a clipboard paste later) only supplies the bytes: it exposes
its metadata and an `upload(grant, live_guard, progress)` coroutine. Everything that decides
whether the picture may be staged or inserted lives here, so every source has the same fences:
the grant, the polling, the draft/session identity checks after each await, the accepted-before-
write rule and the retention of files that were not inserted.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from relaydeck.client.features.composer.pictures import Picture

MAX_PICTURE_BYTES = 10 * 1024 * 1024
MAX_STAGED_PICTURES = 8
CLAIMED_IDS = 64


class UploadFailed(Exception):
    """The source's transfer failed and the user was already told why."""


@dataclass(frozen=True, slots=True)
class SourceMetadata:
    size: int


class UploadSource(Protocol):
    metadata: SourceMetadata

    async def upload(
        self,
        grant: dict[str, Any],
        live_guard: Callable[[], bool],
        progress: Callable[[float], Any],
    ) -> None: ...

    def abort(self) -> None:
        """Stop waiting for the transfer; safe to call at any time and more than once."""

    def dispose(self) -> None:
        """Release whatever the source holds once the operation is over."""


@dataclass(slots=True)
class Activation:
    """What the user's gesture saw: the session, its draft identity, the workflow and boundary."""

    session: Any
    identity: tuple[Any, ...]
    workflow_at_start: bool  # True: Composer staging, False: Direct insertion
    direct_input_boundary: tuple[Any, ...]
    # The draft store the gesture saw; a replaced store invalidates the activation.
    draft_store: Any = None
    # One id per gesture: delivering the same activation again is a repeat, not a new request.
    request_id: str = field(default_factory=lambda: uuid.uuid4().hex)


@dataclass(eq=False)
class PictureOperation:
    request_id: str
    identity: tuple[Any, ...]
    workflow_at_start: bool
    direct_input_boundary: tuple[Any, ...]
    draft_store: Any = None
    cancelled: bool = False
    started: bool = False
    accepted: bool = False
    grant_id: str | None = None
    source: UploadSource | None = None
    # The picker identity that may complete this operation; None for other sources.
    owner: Any = None
    # The completion bound to the render that started the operation, so a late picker result
    # runs with that render's session facts and not an older one.
    complete: Callable[[PictureOperation, Any], Any] | None = None


class OperationHost(Protocol):
    """What the lifecycle needs from the surface that owns the operation."""

    session: Any
    api: Any
    logger: logging.Logger
    send: Any

    def is_current(self, op: PictureOperation) -> bool: ...
    def identity(self) -> tuple[Any, ...]: ...
    def identity_reason(self) -> str | None: ...
    def input_reason(self, op: PictureOperation) -> str | None: ...
    def show(self, message: str) -> None: ...
    def stopped(self, stage: str, reason: str) -> None: ...
    def set_progress(self, value: float | None) -> None: ...
    def finish(self, op: PictureOperation) -> None: ...
    def begin(self, op: PictureOperation) -> None: ...
    def busy(self) -> bool: ...
    def result_polls(self) -> int: ...
    def poll_seconds(self) -> float: ...
    def failure_text(self) -> str: ...
    def log_reason(self, exc: BaseException) -> str: ...
    def client_error(self) -> type[Exception]: ...


@dataclass
class RequestClaims:
    """Each request id runs once; a repeated delivery never makes a second grant or write."""

    seen: deque[str] = field(default_factory=lambda: deque(maxlen=CLAIMED_IDS))

    def claim(self, request_id: str) -> bool:
        if request_id in self.seen:
            return False
        self.seen.append(request_id)
        return True


async def run_operation(
    host: OperationHost,
    op: PictureOperation,
    source: UploadSource | None,
    insert: Callable[[Any, Picture, Any, Callable[[], bool]], Any],
) -> None:
    """Grant, upload, poll, then stage (Composer) or insert (Direct) one picture.

    `source` is None when the user chose nothing; that is a normal exit, not an error.
    """
    session = host.session
    if not host.is_current(op):
        host.stopped("start", "stale_op")
        return
    if op.started:
        host.stopped("start", "already_started")
        return
    op.started = True
    op.source = source
    compose = op.workflow_at_start
    store = getattr(session, "store", None)

    def upload_reason() -> str | None:
        if not host.is_current(op):
            return "stale_op"
        if op.cancelled:
            return "cancelled"
        if getattr(session, "store", None) is not op.draft_store:
            return "draft_changed"
        return host.identity_reason()

    def live() -> bool:
        # Upload and staging are session/draft-bound, not writer-bound: a temporary suspension
        # can invalidate every writer while the same draft stays. Only insertion needs control.
        return upload_reason() is None

    def stopped(stage: str) -> bool:
        reason = upload_reason()
        if reason:
            host.stopped(stage, reason)
        return reason is not None

    api = host.api
    try:
        if source is None:
            host.stopped("start", "no_files")
            return
        if stopped("start"):
            return
        if source.metadata.size > MAX_PICTURE_BYTES:
            host.stopped("start", "too_large")
            return
        grant = await api.begin(session.host_id)
        op.grant_id = grant["picture_id"]
        if stopped("grant"):
            return
        host.set_progress(0.0)
        try:
            await source.upload(grant, live, host.set_progress)
        except UploadFailed:
            host.stopped("upload", "upload_error")
            return
        if stopped("upload"):
            return
        doc = await api.result(op.grant_id)
        # The gateway finishes decoding just after the bytes arrive.
        for _ in range(host.result_polls()):
            if doc["status"] not in ("pending", "uploading") or not live():
                break
            await asyncio.sleep(host.poll_seconds())
            doc = await api.result(op.grant_id)
        if stopped("poll"):
            return
        if doc["status"] != "ready":
            host.stopped("poll", "not_ready")
            host.show(doc.get("message") or host.failure_text())
            return
        picture = Picture.from_wire(doc)
        if compose:
            # A captured Composer operation owns a draft store.
            assert store is not None  # noqa: S101
            if len(store.pictures) >= MAX_STAGED_PICTURES:
                host.stopped("stage", "picture_limit")
                return
            store.add_picture(picture)
            session.on_draft_change(store.draft)
            op.accepted = True
        else:
            # Keep a successfully uploaded file under its private retention policy when control
            # changed; never retry the insertion.
            if reason := host.input_reason(op):
                op.accepted = True
                host.stopped("insert", reason)
                return
            # From here a write may have begun. A late cancel must never remove a file whose
            # path could already be in the terminal.
            op.accepted = True
            host.set_progress(None)
            outcome = await insert(
                session,
                picture,
                host.send,
                lambda: live() and host.input_reason(op) is None,
            )
            host.show(
                "Picture path inserted. Press Enter when ready."
                if outcome == "pty"
                else "Picture path was not confirmed. Check the terminal; it was not retried."
            )
    except host.client_error() as exc:
        host.logger.warning("Picture upload refused: reason=%s", host.log_reason(exc))
        host.show(str(exc))
    except Exception as exc:
        host.logger.warning("Picture upload failed: reason=%s", host.log_reason(exc))
        host.show(host.failure_text())
    except asyncio.CancelledError:
        host.stopped("complete", "cancelled")
        raise
    finally:
        if op.grant_id and not op.accepted:
            try:
                await api.cancel(op.grant_id)
            except Exception:  # noqa: S110 - cancel after a failed upload is best-effort
                pass
        if host.is_current(op):
            host.finish(op)


async def start_from_source(
    host: OperationHost,
    claims: RequestClaims,
    source: UploadSource,
    activation: Activation,
    insert: Callable[[Any, Picture, Any, Callable[[], bool]], Any],
) -> str:
    """Run a source that arrives without the picker: same fences, same busy gate.

    Returns "started" once the operation ran, otherwise the reason it was refused. A picture is
    never queued: a refused source is disposed and the user attaches again.
    """
    request_id = activation.request_id
    if host.session is not activation.session or host.identity() != activation.identity:
        reason: str | None = "session_changed"
    elif getattr(host.session, "store", None) is not activation.draft_store:
        reason = "draft_changed"
    else:
        reason = host.identity_reason()
    if reason is not None:
        host.stopped("start", reason)
        source.dispose()
        return reason
    probe = PictureOperation(
        request_id,
        activation.identity,
        activation.workflow_at_start,
        activation.direct_input_boundary,
        draft_store=activation.draft_store,
    )
    if blocked := host.input_reason(probe):
        host.stopped("start", blocked)
        source.dispose()
        return blocked
    if host.busy():
        host.stopped("start", "busy")
        source.dispose()
        return "busy"
    if not claims.claim(request_id):
        host.stopped("start", "already_started")
        source.dispose()
        return "already_started"
    store = getattr(host.session, "store", None)
    if probe.workflow_at_start and store is not None and len(store.pictures) >= MAX_STAGED_PICTURES:
        host.stopped("start", "picture_limit")
        source.dispose()
        return "picture_limit"
    host.begin(probe)
    try:
        await run_operation(host, probe, source, insert)
    finally:
        source.dispose()
    return "started"
