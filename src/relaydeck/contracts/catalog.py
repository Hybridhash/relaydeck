"""Constrained companion discovery; these are policy kinds, not Herdr RPC names."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

# the kinds this Mac may start come from its config (`agent_kinds`) and
# must also be on PATH; the wire contract only checks the shape of a kind name.
AGENT_KIND_PATTERN = "^[a-z][a-z0-9_-]{0,31}$"
AGENT_KIND = re.compile(AGENT_KIND_PATTERN)
MAX_AGENT_KINDS = 32
CATALOG_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["status", "source", "observed_ms", "entries"],
    "additionalProperties": False,
    "properties": {
        "status": {"enum": ["ready", "error"]},
        "source": {"enum": ["companion-path", "synthetic"]},
        "observed_ms": {"type": "integer", "minimum": 0},
        "entries": {
            "type": "array",
            "maxItems": MAX_AGENT_KINDS,
            "items": {
                "type": "object",
                "required": ["kind", "available", "reason"],
                "additionalProperties": False,
                "properties": {
                    "kind": {"type": "string", "pattern": AGENT_KIND_PATTERN},
                    "available": {"type": "boolean"},
                    "reason": {"enum": ["installed", "not_installed"]},
                },
            },
        },
    },
}


@dataclass(frozen=True)
class AgentCatalog:
    status: str
    observed_ms: int
    available: tuple[str, ...]
    source: str = "companion-path"


def catalog_from_wire(doc: Any) -> AgentCatalog | None:
    """Decode an already schema-validated snapshot/listing; absence is unverified."""
    if not isinstance(doc, dict):
        return None
    entries = doc.get("entries", [])
    available = (
        tuple(
            dict.fromkeys(
                entry["kind"]
                for entry in entries
                if entry.get("available") is True
                and isinstance(entry.get("kind"), str)
                and AGENT_KIND.fullmatch(entry["kind"]) is not None
                and entry.get("reason") == "installed"
            )
        )
        if doc.get("status") == "ready"
        else ()
    )
    return AgentCatalog(str(doc["status"]), int(doc["observed_ms"]), available, str(doc["source"]))
