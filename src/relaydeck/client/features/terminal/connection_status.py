"""Connection status for the terminal dot (STATUS-DOT).

Metadata liveness alone never reports a live connection. The terminal is live only when its native
viewer is verified (`terminal_live`). An unverified terminal shows its recovery notice, or its
checking state, instead.
"""

from __future__ import annotations

from typing import Any


def connection_status(
    *,
    state_value: str,
    stale: bool,
    banner_text: str | None,
    active_entry: Any,
) -> tuple[bool, str]:
    """(connection_live, connection_text) for the dot and the banner."""
    connection_text = banner_text if banner_text is not None else state_value
    metadata_live = state_value == "live" and not stale
    connection_live = metadata_live and (active_entry is None or active_entry.session.terminal_live)
    if metadata_live and active_entry is not None and not connection_live:
        terminal_state = active_entry.session.term_state
        connection_text = (
            terminal_state.notice.text
            if terminal_state.notice is not None
            else (
                f"Terminal {terminal_state.status}"
                if terminal_state.status != "live"
                else "Checking terminal connection…"
            )
        )
    return connection_live, connection_text
