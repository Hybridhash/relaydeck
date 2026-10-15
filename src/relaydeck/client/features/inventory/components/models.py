"""Destinations and plain row/label helpers for the inventory shell.

Pure functions over `AppState` (no Flet): terminal rows, diagnostics rows,
scope and host-context labels."""

from __future__ import annotations

from dataclasses import dataclass

from relaydeck.client.components.design_system.shells import ShellListRow
from relaydeck.client.features.inventory.selectors import DiagnosticsModel, shell_rows
from relaydeck.domain.models import AppState, scope_key

DESTINATIONS: tuple[tuple[str, str, str], ...] = (
    ("agents", "Console", "SMART_TOY"),
    ("terminals", "Terminals", "TERMINAL"),
    ("usage", "Usage", "INSIGHTS"),
    ("settings", "Settings", "SETTINGS"),
)


@dataclass(frozen=True)
class TerminalListEntry:
    host: str
    row: ShellListRow


def terminal_rows(
    state: AppState, query: str, host_id: str, live: frozenset[str], space_key: str = ""
) -> tuple[TerminalListEntry, ...]:
    """Terminals page rows: ordinary shells (agent tabs excluded), with a
    mono "host · N panes" line and this device's Live/Detached state."""
    entries: list[TerminalListEntry] = []
    tabs = {scope_key(tab.host_id, tab.id): tab for tab in state.inventory.tabs.values()}
    for row in shell_rows(state, query, host_id, space_key):
        tab = tabs.get(row.key)
        if tab is None:
            continue
        host = state.inventory.hosts.get(tab.host_id)
        host_label = (host.label if host else "") or tab.host_id
        panes = tab.pane_count or 1
        entries.append(
            TerminalListEntry(
                host=tab.host_id,
                row=ShellListRow(
                    key=row.key,
                    title=row.title,
                    meta=f"{host_label} · {panes} pane{'s' if panes != 1 else ''}",
                    live=row.key in live,
                ),
            )
        )
    return tuple(entries)


def diagnostics_rows(model: DiagnosticsModel) -> tuple[tuple[str, str], ...]:
    return (
        ("Connection", model.connection),
        ("Gateway", "reachable" if model.gateway_reachable else "unreachable"),
        ("Sync socket", model.socket_status),
        ("Host", model.host_label or "none"),
        ("Host last seen", model.host_last_seen),
        ("Host protocol", model.host_protocol),
        ("Cursor", model.cursor_preview),
        ("Correlation", model.correlation_preview),
        ("App", model.app_version),
    )


def agent_scope_label(state: AppState, host_id: str, workspace_id: str) -> str:
    """Mono header line for an agent window: "host / workspace"."""
    host = state.inventory.hosts.get(host_id)
    workspace = state.inventory.workspaces.get(scope_key(host_id, workspace_id))
    host_label = (host.label if host else "") or host_id
    workspace_label = (workspace.label or workspace.id) if workspace else ""
    return f"{host_label} / {workspace_label}" if workspace_label else host_label


def host_context(state: AppState) -> tuple[str, str, bool]:
    """Current host label, status word, and reachability from real state.

    The context host is the selected host, falling back to the sync
    connection host. The word is "Live" only when the connection is
    live on that host; an unreachable host always reads "Offline".
    """
    host_id = state.selection.host_id or state.connection.host_id
    host = state.inventory.hosts.get(host_id)
    label = (host.label if host else "") or host_id or "No host"
    reachable = bool(host and host.reachable)
    if not reachable:
        return label, "Offline", False
    if state.connection.state.value == "live" and state.connection.host_id == host_id:
        return label, "Live", True
    return label, state.connection.state.value.replace("_", " ").title(), True
