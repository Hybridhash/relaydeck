"""Plain helpers shared by `Root` and its hooks (moved out of `client/app.py` in Q6b).

Content-free by design: error categories are fixed strings, logging never includes
exception data. `client/app.py` re-exports these names.
"""

from __future__ import annotations

import logging

from relaydeck.client.features.composer.observation import CommandOutcomeUnknown
from relaydeck.client.features.sync.transport import TransportError
from relaydeck.client.pending_commands import RecoveryError
from relaydeck.contracts.schemas import ERROR_CODES
from relaydeck.domain.models import AppState

# design tokens (minimal first theme, centrally configurable).

logger = logging.getLogger("relaydeck.client.app")


SUBMIT_ERRORS = {
    "unsupported_capability": "Not allowed for this device.",
    "forbidden": "Rejected by the gateway.",
    "not_found": "Unknown host.",
    "stale_epoch": "The host restarted. Try again.",
    "host_offline": "The host is offline.",
    "command_conflict": "Already submitted with different content.",
    "rate_limited": "Too many requests. Wait a moment.",
    "too_large": "Content is too large.",
}


_CREATION_ERROR_CLASSES = {
    TransportError: "TransportError",
    RecoveryError: "RecoveryError",
    CommandOutcomeUnknown: "CommandOutcomeUnknown",
    TimeoutError: "TimeoutError",
    RuntimeError: "RuntimeError",
    ValueError: "ValueError",
    OSError: "OSError",
}


_CREATION_ERROR_CODES = frozenset(ERROR_CODES) | {"command_recovery"}


def _log_creation_failure(exc: Exception) -> None:
    """Log only fixed categories; exception data and chains are untrusted."""
    category = _CREATION_ERROR_CLASSES.get(type(exc), "Exception")
    raw_code = exc.code if isinstance(exc, TransportError) else None
    normalized = raw_code.strip().lower() if type(raw_code) is str else "unknown"
    code = normalized if normalized in _CREATION_ERROR_CODES else "unknown"
    logger.warning("Creation outcome unknown: class=%s code=%s", category, code)


def new_command_id() -> str:
    import uuid

    return f"cmd-{uuid.uuid4().hex[:16]}"


def selection_host(state: AppState, destination: str, selection: str) -> str:
    """Resolve the domain host for a route selection."""
    if destination == "settings":
        return selection  # a Settings selection is a host (Settings › Hosts)
    if destination == "agents":
        agent = state.inventory.agents.get(selection)
        if agent is not None:
            return agent.host_id
    elif destination == "terminals":
        tab = state.inventory.tabs.get(selection)
        if tab is not None:
            return tab.host_id
    return state.connection.host_id


# Off-screen parking distance for hidden retained terminal surfaces.
RETAINED_PARK_OFFSET = 100_000


TYPING_BLUR_DEBOUNCE_S = 0.35
