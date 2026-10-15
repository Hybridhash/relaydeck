"""Shared presentation exports, loaded lazily to keep theme imports acyclic."""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from relaydeck.client.components.host_context import HostContextHeader as HostContextHeader
    from relaydeck.client.components.layout import (
        COMPACT as COMPACT,
    )
    from relaydeck.client.components.layout import (
        DESKTOP as DESKTOP,
    )
    from relaydeck.client.components.layout import (
        PHONE as PHONE,
    )
    from relaydeck.client.components.layout import (
        layout_mode as layout_mode,
    )
    from relaydeck.client.components.layout import (
        list_width as list_width,
    )
    from relaydeck.client.components.layout import (
        rail_width as rail_width,
    )
    from relaydeck.client.components.rail import CompactRail as CompactRail
    from relaydeck.client.components.session_rows import (
        agent_tile as agent_tile,
    )
    from relaydeck.client.components.session_rows import (
        empty_hint as empty_hint,
    )
    from relaydeck.client.components.session_rows import (
        host_tile as host_tile,
    )
    from relaydeck.client.components.session_rows import (
        shell_tile as shell_tile,
    )
    from relaydeck.client.components.status import StatusDot as StatusDot
    from relaydeck.client.components.status import StatusWord as StatusWord

_EXPORTS = {
    "HostContextHeader": "host_context",
    "COMPACT": "layout",
    "DESKTOP": "layout",
    "PHONE": "layout",
    "layout_mode": "layout",
    "list_width": "layout",
    "rail_width": "layout",
    "CompactRail": "rail",
    "agent_tile": "session_rows",
    "empty_hint": "session_rows",
    "host_tile": "session_rows",
    "shell_tile": "session_rows",
    "StatusDot": "status",
    "StatusWord": "status",
}
__all__ = list(_EXPORTS)


def __getattr__(name: str) -> Any:
    if name not in _EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(f"{__name__}.{_EXPORTS[name]}"), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
