"""Browser paste bridge: a layout-neutral region around real inputs.

Dart owns the clipboard event and the retained Blob; Python only receives a request id and
metadata (`offer`), decides, and asks the same control to `upload` that request to a grant URL
or to `abort` it. Image bytes never cross Flet events or the terminal channel. Text pasted
while a terminal has focus arrives as `text` for the terminal's reviewed paste; text for a text
field is inserted in Dart and never crosses as an event.

No `from __future__ import annotations`: Flet 1.0.3 reads raw event annotations (see terminal.py).
"""

import re
from dataclasses import dataclass
from typing import Any

import flet as ft
from flet.controls.control_event import ControlEvent
from flet.controls.layout_control import LayoutControl

# The only upload target the bridge will hand to the browser: the companion's picture content path.
_UPLOAD_URL = re.compile(r"/v1/pictures/[A-Za-z0-9_-]{1,64}/content\?grant=[A-Za-z0-9_-]{1,256}")
_REQUEST_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")

MIN_UPLOAD_MS = 1_000
MAX_UPLOAD_MS = 120_000


@dataclass
class PicturePasteOfferEvent(ControlEvent):
    request_id: str
    mime: str
    size: int
    # The binding the browser read at the gesture; Python rejects it when the facts moved.
    binding: str = ""


@dataclass
class PicturePasteRejectEvent(ControlEvent):
    reason: str


@dataclass(repr=False)
class PicturePasteTextEvent(ControlEvent):
    text: str
    # The binding the browser read at the gesture, as for picture offers.
    binding: str = ""

    def __repr__(self) -> str:
        # Flet logs every triggered event at DEBUG; the pasted text must never appear there.
        return f"PicturePasteTextEvent(name={self.name!r}, chars={len(self.text)})"


def valid_request_id(value: object) -> bool:
    return isinstance(value, str) and _REQUEST_ID.fullmatch(value) is not None


def valid_upload_url(value: object) -> bool:
    return isinstance(value, str) and _UPLOAD_URL.fullmatch(value) is not None


def bounded_upload_ms(expires_ms: int, now_ms: int) -> int:
    """Time the browser may spend on the upload: the grant's remaining life, within bounds."""
    return max(MIN_UPLOAD_MS, min(MAX_UPLOAD_MS, int(expires_ms) - int(now_ms)))


@ft.control("RelayDeckPicturePaste")  # type: ignore[arg-type]
class RelayDeckPicturePaste(LayoutControl):
    """Wraps the inputs whose paste may carry a picture; adds no visual or focus behavior."""

    content: ft.Control | None = None

    binding: str = ""
    """Opaque activation revision, republished on every render and echoed in each offer."""

    pictures: bool = True
    """False while Python cannot take a picture: the browser then ignores pasted files."""

    on_offer: ft.EventHandler[PicturePasteOfferEvent] | None = None
    on_reject: ft.EventHandler[PicturePasteRejectEvent] | None = None
    on_text: ft.EventHandler[PicturePasteTextEvent] | None = None

    async def upload(self, request_id: str, upload_url: str, expires_ms: int) -> str:
        """PUT the retained Blob of `request_id`; the result is a short status word."""
        if not valid_request_id(request_id) or not valid_upload_url(upload_url):
            return "bad_request"
        result: Any = await self._invoke_method(
            "upload",
            {"request_id": request_id, "upload_url": upload_url, "expires_ms": expires_ms},
            timeout=expires_ms / 1000 + 5,
        )
        return str(result)

    async def abort(self, request_id: str) -> None:
        if valid_request_id(request_id):
            await self._invoke_method("abort", {"request_id": request_id}, timeout=5)
