"""Read-only bounded executable lookup in the companion's PATH. Never execute probes."""

from __future__ import annotations

import shutil
from collections.abc import Callable, Sequence
from typing import Any


def discover_agent_catalog(
    observed_ms: int,
    *,
    kinds: Sequence[str],
    which: Callable[[str], str | None] = shutil.which,
) -> dict[str, Any]:
    """Report which configured kinds are on PATH. `kinds` is the owner's allowlist
    (companion.toml `agent_kinds`); nothing is probed beyond `which`. Empty = no entries."""
    doc: dict[str, Any] = {
        "status": "ready",
        "source": "companion-path",
        "observed_ms": observed_ms,
        "entries": [],
    }
    try:
        for kind in kinds:
            available = which(kind) is not None
            doc["entries"].append(
                {
                    "kind": kind,
                    "available": available,
                    "reason": "installed" if available else "not_installed",
                }
            )
    except OSError:
        doc["status"] = "error"
        doc["entries"] = []
    return doc
