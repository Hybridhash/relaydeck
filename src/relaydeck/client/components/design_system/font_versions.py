"""Content fingerprints for the app's own bundled fonts, so their URLs can be cached for a year.

A font URL carries `?v=<first 12 hex of the file's SHA-256>`; the web server grants a long
lifetime only when the request's `v` matches the file as it is now.
"""

from __future__ import annotations

import hashlib
import re
from functools import cache
from pathlib import Path
from urllib.parse import parse_qs

FONTS_DIR = Path(__file__).resolve().parent.parent.parent / "assets" / "design_system" / "fonts"
URL_PREFIX = "/design_system/fonts/"
_NAME = re.compile(r"[A-Za-z0-9._-]+")


@cache
def fingerprint(name: str) -> str | None:
    """12 hex characters of the bundled file's SHA-256, or None when there is no such file."""
    if not _NAME.fullmatch(name) or name.startswith("."):
        return None
    path = FONTS_DIR / name
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()[:12]
    except OSError:
        return None


def versioned_url(asset_base: str, name: str) -> str:
    """`<asset_base>/fonts/<name>`, with the file's fingerprint when it exists."""
    url = f"{asset_base}/fonts/{name}"
    version = fingerprint(name)
    return f"{url}?v={version}" if version else url


def is_current(path: str, query: bytes | str) -> bool:
    """True for a bundled font request whose `v` matches the file's present fingerprint."""
    if not path.startswith(URL_PREFIX):
        return False
    text = query.decode("latin-1") if isinstance(query, bytes) else query
    versions = parse_qs(text).get("v", [])
    current = fingerprint(path[len(URL_PREFIX) :])
    return current is not None and versions == [current]
