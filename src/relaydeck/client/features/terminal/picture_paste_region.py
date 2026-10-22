"""Browser paste: the upload source backed by the paste region, and the region itself.

The browser keeps the clipboard Blob; this module only turns an offer (request id and metadata)
into an `UploadSource` for the shared operation and asks the region to upload to the grant URL.
Text pasted while the terminal has focus arrives as a `text` event and goes through the session's
reviewed paste; the text is never logged.
"""

from __future__ import annotations

import inspect
import time
from collections.abc import Callable
from typing import Any

from relaydeck_terminal.picture_paste import (
    RelayDeckPicturePaste,
    bounded_upload_ms,
    valid_request_id,
    valid_upload_url,
)

from relaydeck.client.features.terminal.events import spawn
from relaydeck.client.features.terminal.picture_operations import (
    SourceMetadata,
    UploadFailed,
)


class BrowserPictureSource:
    """One offered clipboard picture; the bytes stay in the browser until `upload`."""

    def __init__(
        self,
        region: Any,
        request_id: str,
        size: int,
        *,
        now_ms: Callable[[], int] = lambda: int(time.time() * 1000),
    ) -> None:
        self.metadata = SourceMetadata(size)
        self._region = region
        self._request_id = request_id
        self._now_ms = now_ms
        self._released = False

    async def upload(
        self,
        grant: dict[str, Any],
        live_guard: Callable[[], bool],
        progress: Callable[[float], Any],
    ) -> None:
        url = grant.get("upload_url")
        if not valid_upload_url(url):
            raise UploadFailed
        wait_ms = bounded_upload_ms(int(grant.get("expires_ms", 0)), self._now_ms())
        status = await self._region.upload(self._request_id, url, wait_ms)
        self._released = True
        if status != "ok" or not live_guard():
            raise UploadFailed

    def abort(self) -> None:
        self.dispose()

    def dispose(self) -> None:
        if self._released:
            return
        self._released = True
        spawn(self._region.abort(self._request_id), label="picture-paste-abort")


_SOURCE_KEYS = (
    "capture_picture_activation",
    "attach_picture_source",
    "reject_picture_paste",
    "picture_paste_available",
    "picture_paste_binding",
    "picture_paste_binding_now",
    "browser_paste_text",
)


def take_paste_props(picture_props: dict[str, Any]) -> dict[str, Any]:
    """Remove the paste entry points from the props a screen receives and return them."""
    return {key: picture_props.pop(key) for key in _SOURCE_KEYS if key in picture_props}


def wrap_with_paste_region(screen: Any, paste: dict[str, Any], *, key: str) -> Any:
    """The screen inside a paste region; pictures are offered only while they are available."""
    return picture_paste_region(
        screen,
        capture=paste["capture_picture_activation"],
        attach=paste["attach_picture_source"],
        reject=paste["reject_picture_paste"],
        binding=paste["picture_paste_binding"],
        current_binding=paste["picture_paste_binding_now"],
        key=key,
        pictures=bool(paste.get("picture_paste_available")),
        on_text=paste["browser_paste_text"],
    )


def picture_paste_region(
    content: Any,
    *,
    capture: Callable[..., Any],
    attach: Callable[[Any, Any], Any],
    reject: Callable[[str], None],
    binding: str,
    current_binding: Callable[[], str],
    key: str,
    pictures: bool = True,
    on_text: Callable[[str], Any] | None = None,
) -> Any:
    """Wrap `content` so a browser paste attaches its picture through the shared operation, or
    hands its text to the focused terminal's reviewed paste."""

    async def on_offer(event: Any) -> None:
        if not valid_request_id(event.request_id):
            return
        # The offer carries the binding the browser held at the gesture. If any fact it
        # stood for moved since (workflow, draft, session, writer, pause), the offer is
        # refused and its Blob released; it is never re-bound to the new facts.
        if getattr(event, "binding", "") != current_binding():
            reject("stale")
            spawn(event.control.abort(event.request_id), label="picture-paste-stale-abort")
            return
        source = BrowserPictureSource(event.control, event.request_id, int(event.size))
        await attach(source, capture(event.request_id))

    def on_reject(event: Any) -> None:
        reject(str(event.reason))

    async def on_paste_text(event: Any) -> None:
        text = getattr(event, "text", "")
        if on_text is None or not isinstance(text, str) or not text:
            return
        # Same rule as picture offers: if the writer, workflow or session moved since the
        # gesture, the text is refused, never re-targeted at the new facts.
        if getattr(event, "binding", "") != current_binding():
            reject("stale_text")
            return
        result = on_text(text)
        if inspect.isawaitable(result):
            await result

    return RelayDeckPicturePaste(
        key=key,
        expand=True,
        content=content,
        binding=binding,
        pictures=pictures,
        on_offer=on_offer,
        on_reject=on_reject,
        on_text=on_paste_text,
    )
