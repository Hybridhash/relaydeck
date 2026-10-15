"""The application version: defined once in pyproject.toml, read from package metadata.

The packaged mobile build ships no package metadata, so its stager writes `_version.py`
from the same pyproject.toml.
"""

from __future__ import annotations

from importlib import import_module
from importlib.metadata import PackageNotFoundError, version

UNKNOWN = "0+unknown"


def _resolve() -> str:
    try:
        return version("relaydeck")
    except PackageNotFoundError:
        pass
    try:
        stamp = import_module("relaydeck._version")
    except ImportError:
        return UNKNOWN
    return str(stamp.VERSION)


__version__ = _resolve()
