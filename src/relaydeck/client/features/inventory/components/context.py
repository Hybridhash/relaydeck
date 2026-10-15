"""The read-only context every `AppShell` builder receives.

`AppShell` computes its props and derived layout once per render and builds
this value; the plain builder functions in `shell_panes` and `shell_frame` read
from it so none of them needs a 40-argument signature. It carries no hooks
and changes nothing about the control tree."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from relaydeck.client.features.inventory.selectors import AgentRow, BannerModel, DiagnosticsModel
from relaydeck.client.features.settings.prefs import TerminalPrefs
from relaydeck.domain.models import AppState


@dataclass(frozen=True)
class ShellContext:
    """Props plus derived layout for one `AppShell` render."""

    account: str
    banner: BannerModel | None
    collapsed: bool
    compact_terminal: bool
    console_layout: bool
    context_header: Any
    dark: bool
    destination: str
    diagnostics: DiagnosticsModel | None
    filter_host: str
    filter_project: str
    filter_status: str
    filters_open: bool
    has_rail: bool
    hosts: Callable[..., Any]
    inventory_busy: bool
    inventory_error: str
    inventory_more: bool
    inventory_total: int | None
    is_wide: bool
    labels: dict[str, str]
    list_destination: bool
    live_detail: Any
    live_shells: frozenset[str]
    management: Any
    mode: str
    notifications_panel: Any
    on_apply_scenario: Callable[[str], Any] | None
    on_back: Callable[[], Any] | None
    on_banner_action: Callable[[], Any]
    on_destination: Callable[[str], Any]
    on_inventory_more: Callable[[], Any] | None
    on_inventory_refresh: Callable[[], Any] | None
    on_logout: Callable[[], Any] | None
    on_new_terminal: Callable[[], Any]
    on_open_shell: Callable[[], Any] | None
    on_open_terminal: Callable[[], Any]
    on_prefs: Callable[[TerminalPrefs], Any] | None
    on_select_agent: Callable[[str], Any]
    on_select_host: Callable[[str], Any]
    on_select_shell: Callable[[str], Any]
    on_start_agent: Callable[[], Any]
    paged_agents: tuple[AgentRow, ...] | None
    pane_width: float | None
    pick_host: Callable[[str], Any]
    pick_space: Callable[[str], Any]
    prefs: TerminalPrefs | None
    prefs_saved: bool
    project: Any
    projects: dict[str, Any]
    query: str
    resize: Callable[[float], None]
    retained_active: bool
    retained_views: tuple[Any, ...]
    scenarios: tuple[str, ...]
    scroll_positions: Any
    selection: str
    set_collapsed: Callable[[bool], None]
    set_filter_status: Callable[[str], Any]
    set_filters_open: Callable[[bool], Any]
    set_query: Callable[[str], Any]
    set_shell_query: Callable[[str], Any]
    shell_host: str
    shell_query: str
    shell_space: str
    sizing: list[Any]
    spaces: Callable[..., Any]
    split_gap: int
    state: AppState
    tokens: dict[str, str]
    typing_split: bool
    width: float
    # Usage: None until the transport exposes the usage read (unavailable state).
    usage_fetch: Any = None
    usage_identity: Any = None
    usage_host_id: str = ""
    on_open_usage: Callable[[], Any] | None = None
    foreground: bool = True
