"""ClipboardPort backed by the Flet core Clipboard service."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import cast

import flet as ft

from relaydeck.client.adapters.ports import ClipboardPort
from relaydeck.client.diagnostics import failure_category

logger = logging.getLogger(__name__)

# Some mobile browsers (Firefox) leave a clipboard read pending until a
# browser-owned Paste prompt is tapped. Fall back to long-press Paste quickly
# instead of waiting for Flet's 10 s invoke timeout.
CLIPBOARD_READ_TIMEOUT = 3.0


class ClipboardReadStatus(StrEnum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class ClipboardRead:
    status: ClipboardReadStatus
    text: str | None = None
    # True when the platform read failed and ``text`` is the last text copied
    # inside RelayDeck in this page session. Callers must force paste review.
    from_app: bool = False


async def read_clipboard(port: ClipboardPort | None) -> ClipboardRead:
    """Read once; permission/platform failures are an explicit fallback result.

    A successful read is returned as-is, even when empty. Only when the read
    fails does the in-app copy (if any) stand in, labelled ``from_app``."""
    if port is None:
        return ClipboardRead(ClipboardReadStatus.UNAVAILABLE)
    try:
        return ClipboardRead(ClipboardReadStatus.AVAILABLE, await port.get_text())
    except (RuntimeError, TimeoutError) as exc:
        # Content-free: category only (a platform message can embed clipboard text).
        logger.warning("Clipboard read unavailable: category=%s", failure_category(exc))
        copied = getattr(port, "copied_text", None)
        cached = copied() if callable(copied) else None
        if cached:
            return ClipboardRead(ClipboardReadStatus.AVAILABLE, cached, from_app=True)
        return ClipboardRead(ClipboardReadStatus.UNAVAILABLE)


class FletClipboard(ClipboardPort):
    """One per page session. Remembers the last text RelayDeck copied, in
    memory only, so Paste can offer it (with review) where the browser blocks
    or stalls clipboard reads (iOS, Firefox)."""

    def __init__(self, service: ft.Clipboard) -> None:
        self._service = service
        self._copied: str | None = None

    def copied_text(self) -> str | None:
        return self._copied

    async def get_text(self) -> str | None:
        # Timeout at Flet's invocation boundary so the pending call is removed.
        return cast(
            str | None,
            await self._service._invoke_method("get", timeout=CLIPBOARD_READ_TIMEOUT),
        )

    async def set_text(self, value: str) -> None:
        self._copied = None
        await self._service.set(value)
        self._copied = value
