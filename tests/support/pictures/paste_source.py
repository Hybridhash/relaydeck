"""A fake non-picker upload source and a minimal operation host for the shared operation."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

from relaydeck.client.features.composer.pictures import Picture
from relaydeck.client.features.terminal.picture_operations import (
    PictureOperation,
    SourceMetadata,
    UploadFailed,
)
from relaydeck.client.features.terminal.pictures import FAILURE, PictureClientError


class FakePasteSource:
    """Supplies a picture the way a clipboard paste would: a request id and a size, no file."""

    def __init__(self, size: int = 100, *, fail: bool = False) -> None:
        self.metadata = SourceMetadata(size)
        self.fail = fail
        self.release = asyncio.Event()
        self.entered = asyncio.Event()
        self.aborted = False
        self.disposed = 0
        self.uploads = 0

    async def upload(
        self,
        grant: dict[str, Any],
        live_guard: Callable[[], bool],
        progress: Callable[[float], Any],
    ) -> None:
        self.uploads += 1
        self.entered.set()
        await self.release.wait()
        if self.fail:
            raise UploadFailed
        if live_guard():
            progress(1.0)

    def abort(self) -> None:
        self.aborted = True
        self.release.set()

    def dispose(self) -> None:
        self.disposed += 1


class FakeSession:
    def __init__(self) -> None:
        self.host_id = "home-mac"
        self.keys_enabled = True
        self.bracketed_paste = False
        self.store = SimpleNamespace(pictures=[], draft="", add_picture=self._add)
        self.drafts: list[str] = []

    def _add(self, picture: Picture) -> None:
        self.store.pictures.append(picture)

    def on_draft_change(self, draft: str) -> None:
        self.drafts.append(draft)


class FakeHost:
    """Implements the operation host contract with settable fences and a recording trail."""

    logger = logging.getLogger("tests.picture_operations")

    def __init__(self, api: Any, session: FakeSession | None = None) -> None:
        self.session = session or FakeSession()
        self.api = api
        self.current: PictureOperation | None = None
        self.identity_problem: str | None = None
        self.input: str | None = None
        self.notices: list[str] = []
        self.stops: list[tuple[str, str]] = []
        self.progress: list[float | None] = []
        self.sent: list[bytes] = []
        self.finished = 0

    async def send(self, origin: Any, chunks: Any, **_kwargs: Any) -> str:
        self.sent.append(b"".join(chunks))
        return "pty"

    def is_current(self, op: PictureOperation) -> bool:
        return self.current is op

    def identity(self) -> tuple[str, str]:
        return ("home-mac", "agent-1")

    def identity_reason(self) -> str | None:
        return self.identity_problem

    def input_reason(self, op: PictureOperation) -> str | None:
        # Composer staging is draft-bound; only Direct insertion needs control.
        return None if op.workflow_at_start else self.input

    def show(self, message: str) -> None:
        self.notices.append(message)

    def stopped(self, stage: str, reason: str) -> None:
        self.stops.append((stage, reason))

    def set_progress(self, value: float | None) -> None:
        self.progress.append(value)

    def finish(self, op: PictureOperation) -> None:
        self.current = None
        self.finished += 1

    def begin(self, op: PictureOperation) -> None:
        self.current = op

    def busy(self) -> bool:
        return self.current is not None and self.current.started

    def result_polls(self) -> int:
        return 3

    def poll_seconds(self) -> float:
        return 0.0

    def failure_text(self) -> str:
        return FAILURE

    def log_reason(self, exc: BaseException) -> str:
        return type(exc).__name__

    def client_error(self) -> type[Exception]:
        return PictureClientError
