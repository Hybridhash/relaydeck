"""Inventory navigation and lists.

Presentational components over AppState: destinations, connection banner,
agent/shell/host lists, session detail placeholder, settings. All data
derives from selectors; all intent flows through callbacks.

The reference presentation (host context, bounded scrollable
lists, themed surfaces) without changing keys, callbacks, or the declared
keyboard focus order.

Q6d split the former single module into this package. `AppShell` is still the
one component; its former 900 lines became hooks and plain builder functions
that return the same controls in the same tree positions, so keys, ancestry,
the terminal slot and the retained-view parking are unchanged.

Modules:

- `models`: destinations and pure row/label helpers (no Flet).
- `lists`: banner, search field, scroll helper and the agent/shell/host lists.
- `panes`: session detail placeholder, settings and diagnostics panes.
- `chips`: host/space chips and dropdown fields.
- `state`: the `AppShell` hooks (`use_filter_state`, `use_sidebar_state`).
- `context`: `ShellContext`, the read-only value the builders receive.
- `shell_panes`: per-destination pane builders (plain functions, not components).
- `shell_frame`: split row, body stack, navigation and ground builders.
- `shell`: `AppShell` itself (props, hook calls, derived layout, builder calls).
"""

from __future__ import annotations

from relaydeck.client.components.status import StatusDot
from relaydeck.client.features.inventory.components.lists import (
    AgentsList,
    ConnectionBanner,
    HostsList,
    InventoryScroll,
    SearchField,
    ShellsList,
)
from relaydeck.client.features.inventory.components.models import (
    DESTINATIONS,
    TerminalListEntry,
    agent_scope_label,
    diagnostics_rows,
    host_context,
    terminal_rows,
)
from relaydeck.client.features.inventory.components.panes import (
    DiagnosticsPane,
    SessionDetail,
    SettingsPane,
)
from relaydeck.client.features.inventory.components.shell import AppShell

__all__ = [
    "AgentsList",
    "AppShell",
    "ConnectionBanner",
    "DESTINATIONS",
    "DiagnosticsPane",
    "HostsList",
    "InventoryScroll",
    "SearchField",
    "SessionDetail",
    "SettingsPane",
    "ShellsList",
    "StatusDot",
    "TerminalListEntry",
    "agent_scope_label",
    "diagnostics_rows",
    "host_context",
    "terminal_rows",
]
