"""Private, bounded picture storage. Never mount this directory as HTTP assets."""

from __future__ import annotations

import asyncio
import base64
import io
import os
import re
import secrets
import stat
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Any

from relaydeck.companion.private import private_dir

MAX_BYTES = 10 * 1024 * 1024
MAX_PIXELS = 20_000_000
PREVIEW_SIZE = 48
MAX_PREVIEW_CHARS = 24_000
DAY_MS = 86_400_000
GRANT_MS = 120_000
ROOT = Path.home() / "Library/Application Support/RelayDeck/attachments"


class PictureError(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code, self.message = code, message
        super().__init__(message)


def sniff(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "PNG"
    if data.startswith(b"\xff\xd8\xff"):
        return "JPEG"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "WEBP"
    if len(data) >= 16 and data[4:8] == b"ftyp":
        size = int.from_bytes(data[:4], "big")
        brands = [data[8:12], *(data[i : i + 4] for i in range(16, min(size, 128), 4))]
        if any(b in (b"heic", b"heix", b"hevc", b"hevx", b"mif1", b"msf1") for b in brands):
            return "HEIF"
    raise PictureError("forbidden", "Choose a PNG, JPEG or WebP picture.")


def sanitize(data: bytes) -> tuple[bytes, str]:
    if len(data) > MAX_BYTES:
        raise PictureError("too_large", "Picture must be 10 MB or smaller.")
    kind = sniff(data)
    if kind == "HEIF":
        raise PictureError("unsupported_capability", "HEIC is unavailable; send a JPEG picture.")
    try:
        from PIL import Image, ImageOps
    except ImportError as exc:
        raise PictureError(
            "unsupported_capability", "Picture processing is unavailable on Mac."
        ) from exc
    try:
        with Image.open(io.BytesIO(data)) as source:
            if source.width * source.height > MAX_PIXELS:
                raise PictureError("too_large", "Picture dimensions are too large.")
            if source.format != kind:
                raise ValueError("format mismatch")
            # Decode one frame, orient before dropping all metadata (including GPS).
            image = ImageOps.exif_transpose(source).convert("RGBA" if kind == "PNG" else "RGB")
            image.info.clear()
            output = io.BytesIO()
            image.save(output, format=kind)
            result = output.getvalue()
    except PictureError:
        raise
    except Exception as exc:
        raise PictureError("forbidden", "This picture could not be read; try JPEG or PNG.") from exc
    if len(result) > MAX_BYTES:
        raise PictureError("too_large", "Processed picture exceeds 10 MB.")
    return result, {"JPEG": "jpg", "PNG": "png", "WEBP": "webp"}[kind]


def thumbnail(encoded: bytes) -> str:
    """Reduce the sanitized image, flatten alpha, and omit all source metadata."""
    from PIL import Image

    with Image.open(io.BytesIO(encoded)) as source:
        small = source.convert("RGBA")
        small.thumbnail((PREVIEW_SIZE, PREVIEW_SIZE), Image.Resampling.LANCZOS)
        preview = Image.new("RGB", small.size, "white")
        preview.paste(small, mask=small.getchannel("A"))
        output = io.BytesIO()
        preview.save(output, format="JPEG", quality=75)
    result = base64.b64encode(output.getvalue()).decode("ascii")
    if len(result) > MAX_PREVIEW_CHARS:
        raise PictureError("too_large", "Picture preview is too large.")
    return result


@dataclass
class Upload:
    account: str
    device_id: str
    host_id: str
    access: str
    secret: str
    deadline: int
    status: str = "pending"
    path: str = ""
    expires_ms: int = 0
    message: str = ""
    preview: str = ""


class PictureStore:
    def __init__(self, root: Path = ROOT, *, host_id: str = "") -> None:
        self.root = root
        self.host_id = host_id
        self.uploads: dict[str, Upload] = {}
        self.rates: dict[tuple[str, str], deque[int]] = {}
        self.lock = RLock()
        self.cleanup_error = ""

    def cleanup(self, now_ms: int) -> None:
        private_dir(self.root)
        for path in self.root.iterdir():
            if not re.fullmatch(r"[a-f0-9]{48}\.(jpg|png|webp)", path.name):
                continue
            try:
                info = path.lstat()
            except FileNotFoundError:
                continue  # Cancel/expiry may remove a yielded directory entry.
            if stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid():
                if info.st_mtime * 1000 <= now_ms - DAY_MS:
                    path.unlink(missing_ok=True)
        with self.lock:
            self._expire(now_ms)
            for identifier, record in list(self.uploads.items()):
                if record.status == "ready" and not Path(record.path).is_file():
                    self.cancel(identifier)
            self.rates = {
                k: deque(t for t in v if t > now_ms - 3_600_000)
                for k, v in self.rates.items()
                if v and v[-1] > now_ms - 3_600_000
            }

    def begin(
        self, account: str, device: str, host: str, access: str, now_ms: int
    ) -> tuple[str, Upload]:
        with self.lock:
            return self._begin(account, device, host, access, now_ms)

    def _begin(
        self, account: str, device: str, host: str, access: str, now_ms: int
    ) -> tuple[str, Upload]:
        if not device:
            raise PictureError("forbidden", "A paired device is required.")
        self.rates = {k: v for k, v in self.rates.items() if v and v[-1] > now_ms - 3_600_000}
        key = (account, device)
        if key not in self.rates and len(self.rates) >= 10_000:
            raise PictureError("rate_limited", "Picture limit reached; try again later.")
        times = self.rates.setdefault(key, deque())
        while times and times[0] <= now_ms - 3_600_000:
            times.popleft()
        if len(times) >= 20:
            raise PictureError("rate_limited", "Picture limit reached; try again later.")
        self._expire(now_ms)
        if len(self.uploads) >= 1000:
            raise PictureError("rate_limited", "Picture uploads are busy; try again later.")
        times.append(now_ms)
        identifier = secrets.token_hex(16)
        record = Upload(account, device, host, access, secrets.token_urlsafe(32), now_ms + GRANT_MS)
        self.uploads[identifier] = record
        return identifier, record

    def save(self, data: bytes, now_ms: int) -> str:
        return self.save_with_preview(data, now_ms)[0]

    def save_with_preview(self, data: bytes, now_ms: int) -> tuple[str, str]:
        encoded, suffix = sanitize(data)
        preview = thumbnail(encoded)
        private_dir(self.root)
        path = self.root / f"{secrets.token_hex(24)}.{suffix}"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(encoded)
            os.utime(path, (now_ms / 1000, now_ms / 1000))
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        return str(path), preview

    def _expire(self, now_ms: int) -> None:
        with self.lock:
            for identifier, record in list(self.uploads.items()):
                if (record.expires_ms if record.status == "ready" else record.deadline) <= now_ms:
                    self.cancel(identifier)

    def get(self, identifier: str) -> Upload | None:
        with self.lock:
            return self.uploads.get(identifier)

    def cancel(self, identifier: str) -> None:
        with self.lock:
            upload = self.uploads.get(identifier)
            if upload:
                # Keep the record available for retry if deletion itself fails.
                if upload.path:
                    Path(upload.path).unlink(missing_ok=True)
                self.uploads.pop(identifier, None)
                upload.path, upload.preview = "", ""

    def result(self, identifier: str, record: Upload) -> dict[str, Any]:
        with self.lock:
            if self.uploads.get(identifier) is not record or (
                record.status == "ready" and not Path(record.path).is_file()
            ):
                self.cancel(identifier)
                raise PictureError("not_found", "Picture upload not found.")
            return {
                "picture_id": identifier,
                "status": record.status,
                "path": record.path,
                "expires_ms": record.expires_ms,
                "message": record.message,
                "preview": record.preview,
            }


async def run_picture_cleanup(
    store: PictureStore, now_ms: Callable[[], int], *, sleep: Any = asyncio.sleep
) -> None:
    """Supervise hourly cleanup without exposing file names or exception text."""
    while True:
        await sleep(3600)
        try:
            await asyncio.to_thread(store.cleanup, now_ms())
        except OSError:
            message = "Picture cleanup deferred; retry scheduled."
        except ValueError:
            # Root validation still refuses all deletion from unsafe storage.
            message = "Picture storage is unsafe; cleanup refused."
        else:
            message = ""
        with store.lock:
            store.cleanup_error = message
