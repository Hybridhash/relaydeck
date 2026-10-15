"""Open-source software and fonts the app ships with, and their licences.

Pure data for the Acknowledgements entry in Settings. The full inventory with
versions and proof lives in `docs/acknowledgements.md`; keep the two in step
when a dependency changes.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Acknowledgement:
    name: str
    license: str


@dataclass(frozen=True, slots=True)
class AcknowledgementGroup:
    title: str
    entries: tuple[Acknowledgement, ...]


def _group(title: str, *rows: tuple[str, str]) -> AcknowledgementGroup:
    return AcknowledgementGroup(title, tuple(Acknowledgement(n, lic) for n, lic in rows))


ACKNOWLEDGEMENT_GROUPS: tuple[AcknowledgementGroup, ...] = (
    _group(
        "App framework",
        ("Flet", "Apache-2.0"),
        ("Flutter", "BSD-3-Clause"),
        ("flet-secure-storage", "Apache-2.0"),
    ),
    _group(
        "Terminal and notifications",
        ("xterm2", "MIT"),
        ("firebase_core", "BSD-3-Clause"),
        ("firebase_messaging", "BSD-3-Clause"),
        ("app_links", "Apache-2.0"),
        ("file_picker", "MIT"),
    ),
    _group(
        "Python packages",
        ("httpx", "BSD-3-Clause"),
        ("httpcore", "BSD-3-Clause"),
        ("h11", "MIT"),
        ("anyio", "MIT"),
        ("idna", "BSD-3-Clause"),
        ("certifi", "MPL-2.0"),
        ("websockets", "BSD-3-Clause"),
        ("cryptography", "Apache-2.0 OR BSD-3-Clause"),
        ("cffi", "MIT-0"),
        ("pycparser", "BSD-3-Clause"),
        ("msgpack", "Apache-2.0"),
        ("oauthlib", "BSD-3-Clause"),
        ("repath", "MIT"),
        ("six", "MIT"),
        ("typing-extensions", "PSF-2.0"),
    ),
    _group(
        "Mac companion",
        ("FastAPI", "MIT"),
        ("Starlette", "BSD-3-Clause"),
        ("Uvicorn", "BSD-3-Clause"),
        ("Pillow", "MIT-CMU"),
        ("qrcode", "BSD"),
        ("Pydantic", "MIT"),
    ),
    _group(
        "Fonts",
        ("Bricolage Grotesque", "OFL-1.1"),
        ("JetBrains Mono", "OFL-1.1"),
        ("Noto Sans Symbols 2", "OFL-1.1"),
    ),
)


__all__ = ["ACKNOWLEDGEMENT_GROUPS", "Acknowledgement", "AcknowledgementGroup"]
