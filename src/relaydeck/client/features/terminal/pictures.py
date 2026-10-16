"""Picker upload orchestration; a picked picture alone never emits terminal input.

The picker is one source for the shared operation in `picture_operations`; the hook below owns
the picker identities, the busy gate and the notices around that operation.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import stat
import uuid
import weakref
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import flet as ft
from relaydeck_terminal.protocol import ORIGIN_KEY

from relaydeck.client.features.composer.pictures import Picture
from relaydeck.client.features.terminal.events import click, spawn
from relaydeck.client.features.terminal.paste_policy import BRACKET_END, BRACKET_START
from relaydeck.client.features.terminal.picture_operations import (
    Activation,
    PictureOperation,
    RequestClaims,
    SourceMetadata,
    UploadFailed,
    UploadSource,
    run_operation,
    start_from_source,
)
from relaydeck.contracts.schemas import validate

logger = logging.getLogger(__name__)

UPLOAD_TIMEOUT = 130.0  # gateway grant lives 120 s
RESULT_POLLS = 40
RESULT_POLL_SECONDS = 0.25
PICTURE_MESSAGE_TTL = 5.0  # status lines clear themselves; tests may shorten
FAILURE = "Picture upload failed. Try a JPEG or PNG picture."
CONNECTION_CHANGED = "Connection changed while picking; picture not inserted. Tap 📎 to try again."
_EXIT_MESSAGES = {
    "no_files": "No picture selected. Tap 📎 to try again.",
    "stale_op": "Previous picture selection expired. Tap 📎 to try again.",
    "retired_picker": "Previous picture selection expired. Tap 📎 to try again.",
    "already_started": "This picture selection was already received; nothing was retried.",
    "already_accepted": "This picture selection was already received; nothing was retried.",
    "cancelled": "Picture upload cancelled. Nothing inserted.",
    "session_changed": "Picture selection expired after changing sessions. Tap 📎 to try again.",
    "draft_changed": "The draft changed while picking. Tap 📎 to try again.",
    "disposed": "Picture selection expired after leaving the session. Tap 📎 to try again.",
    "revoked": "Access changed while picking. Pair with your Mac again before attaching pictures.",
    "boundary_changed": CONNECTION_CHANGED,
    "not_foreground": (
        "You left the terminal while picking; picture not inserted. Tap 📎 to try again."
    ),
    "paused": "The terminal paused while picking; picture not inserted. Tap 📎 to try again.",
    "mode_changed": "Input mode changed while picking; picture not inserted. Tap 📎 to try again.",
    "selection_open": "Selection opened while picking; picture not inserted. Tap 📎 to try again.",
    "no_control": "Control changed while picking; picture not inserted. Tap 📎 to try again.",
    "too_large": "Picture must be 10 MB or smaller.",
    "picture_limit": "Remove a picture before attaching another.",
    "upload_error": FAILURE,
    "not_ready": FAILURE,
    "busy": "A picture is already uploading. Wait for it to finish.",
}
_PASTE_REJECTIONS = {
    "multiple": "Paste one picture at a time.",
    "unsupported": "That picture type is not supported. Use a PNG, JPEG or WebP picture.",
    "empty": "The pasted picture is empty.",
    "busy": "A picture is already uploading. Wait for it to finish.",
    "stale": "The screen changed while pasting; picture not attached. Paste it again.",
    "too_large": "Picture must be 10 MB or smaller.",
}


def _log_result(files: Any) -> None:
    size = max((file.size for file in files or ()), default=0)
    bucket = (
        "none"
        if not files
        else "under_1mb"
        if size <= 1024 * 1024
        else "under_5mb"
        if size <= 5 * 1024 * 1024
        else "under_10mb"
        if size <= 10 * 1024 * 1024
        else "over_10mb"
    )
    logger.info("Picture pick result: file_count=%d size_bucket=%s", len(files or ()), bucket)


class _PictureFeedback:
    """Session-scoped notices; retired services hold only a weak reference."""

    def __init__(self, session: Any, setter: Callable[[str], Any]) -> None:
        self.session, self.setter, self.mounted = session, setter, True

    def show(self, session: Any, message: str) -> None:
        if self.mounted and self.session is session:
            self.setter(message)

    def stopped(self, session: Any, stage: str, reason: str) -> None:
        logger.info("Picture selection stopped: stage=%s reason=%s", stage, reason)
        if reason == "no_files":
            return  # dismissing the picker is normal; keep the log line only
        self.show(session, _EXIT_MESSAGES[reason])


def _retired_result(feedback: _PictureFeedback, session: Any) -> Any:
    target, owner = weakref.ref(feedback), weakref.ref(session)

    async def result(event: Any) -> None:
        _log_result(event.files)
        notice, original = target(), owner()
        if notice is not None and original is not None:
            notice.stopped(original, "result", "retired_picker")
        else:
            logger.info("Picture selection stopped: stage=result reason=retired_picker")

    return result


class PictureClientError(Exception):
    pass


# Exception class names safe to name in logs. Only the name is logged —
# never the message, traceback, chained text, URLs or response bodies,
# which may carry local paths, grants or response data.
_LOG_ERROR_ALLOWLIST = frozenset(
    {"PictureClientError", "TimeoutError", "CancelledError", "ConnectionError"}
)


def _log_reason(exc: BaseException) -> str:
    name = type(exc).__name__
    return name if name in _LOG_ERROR_ALLOWLIST else "other"


class PictureApi:
    def __init__(self, ops: Any) -> None:
        self.ops = ops
        self.native = bool(ops.native_picture_upload)

    async def request(self, method: str, path: str, body: Any = None) -> Any:
        response = await self.ops.client.request(
            method,
            self.ops.base_url.rstrip("/") + path,
            headers=await self.ops._auth(),
            timeout=30,
            **({"json": body} if body is not None else {}),
        )
        if response.status_code == 204:
            return None
        if response.is_error:
            code = response.json().get("code", "")
            messages = {
                "unsupported_capability": "Pair with your Mac again to enable pictures.",
                "unauthenticated": "Pair with your Mac again before attaching pictures.",
                "rate_limited": "Picture limit reached. Try again later.",
                "too_large": "Picture must be 10 MB or smaller.",
            }
            raise PictureClientError(messages.get(code, FAILURE))
        return response.json()

    async def begin(self, host: str) -> dict[str, Any]:
        doc = await self.request("POST", "/v1/pictures", {"host_id": host})
        validate("relaydeck/gateway/v1/picture-grant", doc)
        # Web resolves against page origin, never the server's loopback address.
        if self.native:
            doc = {**doc, "upload_url": self.ops.base_url.rstrip("/") + doc["upload_url"]}
        return dict(doc)

    async def upload_native(
        self,
        grant: dict[str, Any],
        file: Any,
        guard: Callable[[], bool],
        progress: Callable[[float], Any],
    ) -> None:
        # Only the packaged runtime enables local file access. Browser-supplied
        # Page.web/platform/path values cannot enable it in the web process.
        if not self.native or not guard() or not file.path or not Path(file.path).is_absolute():
            raise PictureClientError(FAILURE)
        descriptor = os.open(file.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > 10 * 1024 * 1024:
                raise PictureClientError(FAILURE)

            async def chunks() -> AsyncIterator[bytes]:
                size = 0
                while True:
                    data = await asyncio.to_thread(source.read, 64 * 1024)
                    if not guard():
                        raise PictureClientError(FAILURE)
                    if not data:
                        break
                    size += len(data)
                    if size > 10 * 1024 * 1024:
                        raise PictureClientError("Picture must be 10 MB or smaller.")
                    yield data
                    progress(size / max(1, info.st_size))

            response = await self.ops.client.put(
                grant["upload_url"],
                headers=await self.ops._auth(),
                content=chunks(),
                timeout=120,
                follow_redirects=False,
            )
            if response.status_code != 204 or not guard():
                raise PictureClientError(FAILURE)

    async def result(self, identifier: str) -> dict[str, Any]:
        doc = await self.request("GET", f"/v1/pictures/{identifier}")
        validate("relaydeck/gateway/v1/picture-result", doc)
        return dict(doc)

    async def cancel(self, identifier: str) -> None:
        await self.request("DELETE", f"/v1/pictures/{identifier}")


async def insert_picture(
    session: Any, picture: Picture, send: Any, guard: Callable[[], bool]
) -> str:
    if not guard() or not session.keys_enabled:
        return "dropped"
    data = picture.path.encode("utf-8")
    if session.bracketed_paste:
        data = BRACKET_START + data + BRACKET_END
    # commit_bytes, never interpret literal path characters as Ctrl/Alt keys.
    return str(await send(ORIGIN_KEY, (data,), direct_only=True, can_input=guard))


class _PickerSource:
    """The file picker as an upload source: it keeps the picker's file id and callbacks."""

    def __init__(
        self,
        picker: Any,
        file: Any,
        api: Any,
        timeout: Callable[[], float],
        on_error: Callable[[], None],
    ) -> None:
        self.metadata = SourceMetadata(file.size)
        self._picker, self._file, self._api = picker, file, api
        self._timeout, self._on_error = timeout, on_error
        self._finished = asyncio.Event()
        self._failed = False

    async def upload(
        self,
        grant: dict[str, Any],
        live_guard: Callable[[], bool],
        progress: Callable[[float], Any],
    ) -> None:
        if getattr(self._api, "native", False):
            await self._api.upload_native(grant, self._file, live_guard, progress)
            return

        # Flet's web upload() only starts the transfer; completion arrives as on_upload events.
        # Always signal `finished` so a stale operation never hangs.
        async def uploaded(event: Any) -> None:
            if event.error:
                self._failed = True
                self._finished.set()
                self._on_error()
            elif event.progress is not None:
                if event.progress >= 1.0:
                    self._finished.set()
                if live_guard():
                    progress(event.progress)

        picker = self._picker
        picker.on_upload = uploaded
        picker.update()
        await picker.upload(
            [
                ft.FilePickerUploadFile(
                    id=self._file.id, upload_url=grant["upload_url"], method="PUT"
                )
            ]
        )
        async with asyncio.timeout(self._timeout()):
            await self._finished.wait()
        if self._failed:
            raise UploadFailed

    def abort(self) -> None:
        self._finished.set()

    def dispose(self) -> None:
        self._picker = None


class _PictureHost:
    """The operation host for one mounted session: thin delegation to the hook's closures."""

    logger = logger

    def __init__(self, session: Any, api: Any, send: Any, **hooks: Any) -> None:
        self.session, self.api, self.send = session, api, send
        self._hooks = hooks

    def is_current(self, op: PictureOperation) -> bool:
        return bool(self._hooks["current"]() is op)

    def identity(self) -> tuple[Any, ...]:
        return tuple(self._hooks["identity"]())

    def identity_reason(self) -> str | None:
        return self._hooks["identity_reason"]()  # type: ignore[no-any-return]

    def input_reason(self, op: PictureOperation) -> str | None:
        return self._hooks["input_reason"](op.workflow_at_start, op.direct_input_boundary)  # type: ignore[no-any-return]

    def show(self, message: str) -> None:
        self._hooks["show"](message)

    def stopped(self, stage: str, reason: str) -> None:
        self._hooks["stopped"](stage, reason)

    def set_progress(self, value: float | None) -> None:
        self._hooks["set_progress"](value)

    def finish(self, op: PictureOperation) -> None:
        self._hooks["finish"](op)

    def begin(self, op: PictureOperation) -> None:
        self._hooks["begin"](op)

    def busy(self) -> bool:
        return not self._hooks["idle"]()

    def result_polls(self) -> int:
        return RESULT_POLLS

    def poll_seconds(self) -> float:
        return RESULT_POLL_SECONDS

    def failure_text(self) -> str:
        return FAILURE

    def log_reason(self, exc: BaseException) -> str:
        return _log_reason(exc)

    def client_error(self) -> type[Exception]:
        return PictureClientError


def picture_chips(pictures: tuple[Picture, ...], remove: Callable[[str], Any]) -> Any:
    return ft.Row(
        key="composer-picture-chips",
        scroll=ft.ScrollMode.AUTO,
        controls=[
            ft.Row(
                key=f"picture-chip-{p.picture_id}",
                controls=[
                    ft.Image(src=p.preview, width=48, height=48, fit=ft.BoxFit.COVER)
                    if p.preview
                    else ft.Icon(ft.Icons.IMAGE_OUTLINED, size=48),
                    ft.IconButton(
                        ft.Icons.CLOSE,
                        key=f"picture-remove-{p.picture_id}",
                        tooltip="Remove picture",
                        width=48,
                        height=48,
                        on_click=click(lambda identifier=p.picture_id: remove(identifier)),
                    ),
                ],
            )
            for p in pictures
        ],
    )


def attach_button(
    callback: Any,
    *,
    enabled: bool,
    key: str = "terminal-attach-picture",
    action: Any = None,
    compact: bool = False,
) -> Any:
    button = ft.IconButton(
        ft.Icons.ATTACH_FILE,
        key=key,
        tooltip=None if compact else "Attach picture",
        width=32,
        height=36,
        icon_size=18,
        disabled=not enabled,
        action=action,
        on_click=click(callback),
    )
    return ft.Semantics(label="Attach picture", button=True, content=button) if compact else button


def use_picture_attachment(
    session: Any, send: Any, *, expose_sources: bool = False
) -> dict[str, Any]:
    initial_progress: float | None = None
    progress, set_progress = ft.use_state(initial_progress)
    notice, set_notice = ft.use_state((0, ""))
    revision: Any = ft.use_ref(0)
    message = notice[1]

    def set_message(text: str) -> None:
        # Each notification gets a new revision so an identical repeat
        # re-arms the auto-clear timer instead of inheriting the old one.
        revision.current += 1
        set_notice((revision.current, text))

    _busy, set_busy = ft.use_state(False)
    attempts, set_attempts = ft.use_state(0)  # rerender so the next pick gets a fresh picker
    operation: Any = ft.use_ref(None)
    claims: Any = ft.use_ref(RequestClaims)
    live_holders: Any = ft.use_ref({})  # id(picker) -> holder for unretired pickers
    feedback: Any = ft.use_ref(lambda: _PictureFeedback(session, set_message))
    feedback.current.session, feedback.current.setter = session, set_message
    feedback.current.mounted = True
    shown: Any = ft.use_ref("")
    shown.current = message
    clear_timer: Any = ft.use_ref(None)

    def cancel_clear() -> None:
        task = clear_timer.current
        clear_timer.current = None
        if task is not None:
            task.cancel()

    async def auto_clear(captured: str, captured_revision: int) -> None:
        await asyncio.sleep(PICTURE_MESSAGE_TTL)
        if (
            revision.current == captured_revision
            and shown.current == captured
            and feedback.current.mounted
            and feedback.current.session is session
        ):
            set_message("")

    def arm_clear() -> None:
        cancel_clear()
        if message:
            clear_timer.current = spawn(
                auto_clear(message, notice[0]), label="picture-message-clear"
            )

    ft.use_effect(arm_clear, dependencies=[notice], cleanup=cancel_clear)
    store = getattr(session, "store", None)
    identity = (session.host_id, getattr(session, "agent_id", getattr(session, "terminal_id", "")))

    def retire_picker(picker: Any) -> None:
        # Retire a picker whose attempt can never complete through it. A
        # late native result stays ignored even if dispatch still
        # resolves the control. Each attempt builds a fresh picker, so
        # a retired identity is never reused.
        if picker is None:
            return
        holder = live_holders.current.pop(id(picker), None)
        if holder is None:
            return  # unknown or already retired
        # A holder without a picker is retired by definition: its native
        # result can never complete an operation again.
        holder["picker"] = None  # break the picker<->holder<->handler cycle
        # A weak diagnostic callback keeps the retired picker from holding the hook closures. Late
        # results can explain rejection only; in-flight uploads finish through
        # op["picker"].on_upload.
        picker.on_result = _retired_result(feedback.current, session)

    def sweep_services(picker: Any) -> None:
        # Drop registry entries our references no longer sustain. Runs
        # after the caller nulls its picker references; also runs
        # automatically after every session event.
        if picker is None:
            return
        try:
            picker.page._services.unregister_services()
        except Exception:
            # Registry hygiene must never break the attach flow.
            pass

    def new_picker() -> Any:
        # One FilePicker per attempt: its result only completes the operation
        # that opened it, so a late result from a dismissed or superseded
        # pick can never upload or insert under a newer attempt.
        holder: dict[str, Any] = {}

        async def result_event(event: Any) -> None:
            _log_result(event.files)
            active = holder.get("picker")
            if active is None:
                feedback.current.stopped(session, "result", "retired_picker")
                return
            op = operation.current
            if op is None or op.owner is not active:
                feedback.current.stopped(session, "result", "stale_op")
                return
            if op.accepted:
                feedback.current.stopped(session, "result", "already_accepted")
                return
            await op.complete(op, event.files)

        holder["picker"] = ft.FilePicker(on_result=result_event)
        live_holders.current[id(holder["picker"])] = holder
        return holder["picker"]

    picker: Any = ft.use_ref(new_picker)
    rotate: Any = ft.use_ref(False)
    if rotate.current:
        rotate.current = False
        picker.current = new_picker()
    if picker.current is None:
        # Session change retired the picker via cleanup; the next render
        # arms a fresh identity (unmount never renders again).
        picker.current = new_picker()
    token = session.input_token
    boundary = (token, session.interaction_generation, session.controller)
    api = session.picture_api

    def identity_reason() -> str | None:
        if feedback.current.session is not session or identity != (
            session.host_id,
            getattr(session, "agent_id", getattr(session, "terminal_id", "")),
        ):
            return "session_changed"
        if session.disposed or not feedback.current.mounted:
            return "disposed"
        if (
            getattr(session, "_auth_revoked", False)
            or session.term_state.status == "revoked"
            or getattr(session.controller, "revoke_reason", "") in ("unauthenticated", "forbidden")
        ):
            return "revoked"
        if getattr(session, "store", None) is not store:
            return "draft_changed"
        return None

    def input_reason(compose: bool, expected: Any = None) -> str | None:
        if reason := identity_reason():
            return reason
        if not session.foreground:
            return "not_foreground"
        if session.paused:
            return "paused"
        if session.term_state.selection.open:
            return "selection_open"
        if compose != (store is not None and session.sessions.modes.text_edits_draft):
            return "mode_changed"
        if not compose:
            if (boundary if expected is None else expected) != (
                session.input_token,
                session.interaction_generation,
                session.controller,
            ):
                return "boundary_changed"
            if not session.keys_enabled:
                return "no_control"
        return None

    def guard(compose: bool) -> bool:
        return input_reason(compose) is None

    async def cancel() -> None:
        op = operation.current
        if op is not None and not op.accepted:
            op.cancelled = True
            if op.source is not None:
                op.source.abort()
            set_progress(None)
            set_message("Picture upload cancelled. Nothing inserted.")
            if op.grant_id:
                try:
                    await api.cancel(op.grant_id)
                except Exception:
                    pass

    def finish(op: PictureOperation) -> None:
        finished_picker, op.owner = op.owner, None
        operation.current = None
        # The upload waiter finished (or never started): no upload is in flight for this
        # picker's service anymore.
        retire_picker(finished_picker)
        sweep_services(finished_picker)
        if feedback.current.session is session:
            set_progress(None)
            set_busy(False)

    def idle() -> bool:
        # A dismissed browser picker may never report a result. A pick that never received
        # files is abandoned and must not lock the button.
        op = operation.current
        return op is None or not op.started

    def begin(op: PictureOperation) -> None:
        if not idle():
            return
        if operation.current is not None:
            previous = operation.current
            previous.cancelled = True
            # Superseded attempts never started (idle() gate above), so no upload is in
            # flight for their picker.
            retired_picker, previous.owner = previous.owner, None
            retire_picker(retired_picker)
            sweep_services(retired_picker)
        operation.current = op
        set_message("")
        set_busy(True)

    host = _PictureHost(
        session,
        api,
        send,
        current=lambda: operation.current,
        identity=lambda: (
            session.host_id,
            getattr(session, "agent_id", getattr(session, "terminal_id", "")),
        ),
        identity_reason=identity_reason,
        input_reason=input_reason,
        show=lambda message: feedback.current.show(session, message),
        stopped=lambda stage, reason: feedback.current.stopped(session, stage, reason),
        set_progress=set_progress,
        finish=finish,
        begin=begin,
        idle=idle,
    )

    async def complete(op: PictureOperation, files: Any) -> None:
        source: UploadSource | None = None
        if files and operation.current is op and not op.started:
            source = _PickerSource(
                op.owner,
                files[0],
                api,
                lambda: UPLOAD_TIMEOUT,
                lambda: feedback.current.stopped(session, "upload", "upload_error"),
            )
        await run_operation(host, op, source, insert_picture)

    def capture_activation(request_id: str | None = None) -> Activation:
        compose = hasattr(session, "store") and session.sessions.modes.text_edits_draft
        activation = Activation(session, identity, bool(compose), boundary, draft_store=store)
        if request_id:
            activation.request_id = request_id  # the browser's id for this paste gesture
        return activation

    def paste_binding() -> str:
        """Opaque revision of every fact a picture attached now would depend on."""
        compose = hasattr(session, "store") and session.sessions.modes.text_edits_draft
        facts = (
            id(session),
            bool(compose),
            id(getattr(session, "store", None)),
            session.input_token,
            session.interaction_generation,
            id(session.controller),
            bool(session.foreground),
            bool(session.paused),
            bool(session.disposed),
        )
        return hashlib.sha256(repr(facts).encode()).hexdigest()[:24]

    def reject_paste(reason: str) -> None:
        set_message(_PASTE_REJECTIONS.get(reason, FAILURE))

    async def attach_source(source: UploadSource, activation: Activation) -> str:
        """Attach a picture from a non-picker source through the same operation."""
        if api is None:
            source.dispose()
            return "unavailable"
        return await start_from_source(host, claims.current, source, activation, insert_picture)

    def pick() -> None:
        compose = hasattr(session, "store") and session.sessions.modes.text_edits_draft
        if not idle() or api is None or not guard(compose):
            return
        if compose and len(session.store.pictures) >= 8:
            set_message("Remove a picture before attaching another.")
            return
        request_id = uuid.uuid4().hex
        claims.current.claim(request_id)
        begin(
            PictureOperation(
                request_id=request_id,
                identity=identity,
                workflow_at_start=bool(compose),
                direct_input_boundary=boundary,
                draft_store=store,
                owner=picker.current,
                complete=complete,
            )
        )
        rotate.current = True
        set_attempts(attempts + 1)

    # The supported action enters the native picker synchronously on iOS.
    # Python on_click only captures the generation/workflow for its result.
    action = ft.PickFiles(
        picker.current,
        allow_multiple=False,
        file_type=ft.FilePickerFileType.IMAGE,
        cancel_upload_on_window_blur=False,
    )

    def remove(identifier: str) -> None:
        if hasattr(session, "store") and session.foreground and not session.disposed:
            session.store.remove_picture(identifier)
            session.on_draft_change(session.store.draft)

    def cleanup() -> None:
        if feedback.current.session is session:
            feedback.current.mounted = False
        op = operation.current
        if op is not None:
            op.cancelled = True
            if op.source is not None:
                op.source.abort()
            if not op.started:
                # Never began an upload: no completion path can need it.
                # A started op keeps its picker until its waiter lands in
                # the finally block, which retires it there instead.
                op.owner = None
                operation.current = None
        current, picker.current = picker.current, None
        for holder in list(live_holders.current.values()):
            retire_picker(holder.get("picker"))
        sweep_services(current)

    ft.use_effect(lambda: None, dependencies=[session], cleanup=cleanup)
    props = {
        "on_attach_picture": pick,
        "picture_action": action,
        "attach_enabled": api is not None
        and guard(hasattr(session, "store") and session.sessions.modes.text_edits_draft)
        and idle(),
        "on_remove_picture": remove,
        "pictures": session.store.pictures if hasattr(session, "store") else (),
        "picture_progress": ft.Column(
            key="picture-upload-status",
            controls=[
                *(
                    [
                        ft.Row(
                            controls=[
                                ft.ProgressRing(value=progress, width=20, height=20),
                                ft.Text("Uploading picture…"),
                                ft.TextButton(
                                    "Cancel", key="picture-upload-cancel", on_click=click(cancel)
                                ),
                            ]
                        )
                    ]
                    if progress is not None
                    else []
                ),
                *([ft.Text(message, key="picture-upload-message")] if message else []),
            ],
        ),
    }
    if expose_sources:
        # Surfaces splat these props, so the source entry points are opt-in.
        props["capture_picture_activation"] = capture_activation
        props["attach_picture_source"] = attach_source
        props["reject_picture_paste"] = reject_paste
        props["picture_paste_available"] = api is not None
        props["picture_paste_binding"] = paste_binding()
        props["picture_paste_binding_now"] = paste_binding
    return props
