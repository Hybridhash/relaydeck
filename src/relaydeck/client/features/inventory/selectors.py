"""Pure inventory selectors.

Rows, banners, and labels derive from AppState here so components stay
presentational and every rule is unit-testable without Flet.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from relaydeck.domain.agent_order import status_rank
from relaydeck.domain.models import AgentStatus, AppState, Tab, scope_key

STATUS_RANK = {status: status_rank(status.value) for status in AgentStatus}


def _tidy(text: object) -> str:
    return " ".join(str(text or "").split())


def _label(text: object) -> str:
    """A Herdr name exactly as the owner set it (even when it only repeats the
    agent kind, e.g. a tab labelled with its kind)."""
    return _tidy(text)


def agent_names(state: AppState) -> dict[str, str]:
    """Display name per agent key (`scope_key(host, terminal_id)`).

    Rule: the agent's own name, then its Herdr tab label (always, even when it
    only repeats the agent kind), then the subject, then the agent kind. Agents that end
    up with the same name on one host are numbered in terminal-id order ("· 1",
    "· 2"), so two panes of one tab stay distinguishable."""
    tabs = state.inventory.tabs
    bases: dict[str, tuple[str, str]] = {}
    for agent in state.inventory.agents.values():
        tab = tabs.get(scope_key(agent.host_id, agent.tab_id))
        base = (
            _label(agent.name)
            or _label(tab.label if tab else "")
            or _tidy(agent.title)
            or _tidy(agent.agent).title()
            or agent.terminal_id
        )
        bases[scope_key(agent.host_id, agent.terminal_id)] = (agent.host_id, base)
    groups: Counter[tuple[str, str]] = Counter(bases.values())
    names: dict[str, str] = {}
    ordinal: Counter[tuple[str, str]] = Counter()
    for key in sorted(bases):
        host, base = bases[key]
        if groups[(host, base)] > 1:
            ordinal[(host, base)] += 1
            names[key] = f"{base} · {ordinal[(host, base)]}"
        else:
            names[key] = base
    return names


def agent_subject(state: AppState, key: str) -> str:
    """The agent's chat subject (Herdr terminal title) when it adds to the display name."""
    agent = state.inventory.agents.get(key)
    if agent is None:
        return ""
    subject = _tidy(agent.title)
    return subject if subject and subject != agent_names(state).get(key, "") else ""


@dataclass(frozen=True)
class AgentRow:
    key: str
    title: str
    subtitle: str
    status: AgentStatus
    focused: bool
    unread: bool = False
    last_activity_ms: int = 0
    host_id: str = ""
    workspace_id: str = ""
    agent: str = ""
    subject: str = ""


def metadata_rows(
    rows: Iterable[dict[str, Any]], now_ms: int, names: Mapping[str, str] | None = None
) -> tuple[AgentRow, ...]:
    """Trusted typed page metadata; zero activity is unknown, not 'never'.

    `names` (from `agent_names`) supplies the display name when the agent is
    in the loaded inventory; otherwise the page's own name/title is used."""
    result = []
    for row in rows:
        at = row.get("last_activity_ms", 0)
        activity = relative_ms(now_ms, at) if at else "Activity unknown"
        status = AgentStatus(row["status"])
        key = scope_key(row["host_id"], row["terminal_id"])
        title = (names or {}).get(key) or row.get("name") or row.get("title") or row["terminal_id"]
        subject = _tidy(row.get("title")) if _tidy(row.get("title")) != title else ""
        result.append(
            AgentRow(
                key=key,
                title=title,
                subject=subject,
                subtitle=" · ".join(
                    part
                    for part in (row.get("agent", ""), row.get("project", ""), activity)
                    if part
                ),
                status=status,
                focused=row.get("focused", False),
                unread=row.get("unread", False),
                last_activity_ms=at,
                host_id=row["host_id"],
                workspace_id=row.get("workspace_id", ""),
                agent=row.get("agent", ""),
            )
        )
    result.sort(
        key=lambda row: (
            status_rank(row.status.value),
            row.title.casefold(),
            row.key,
        )
    )
    return tuple(result)


@dataclass(frozen=True)
class ShellRow:
    key: str
    title: str
    subtitle: str


@dataclass(frozen=True)
class HostRow:
    key: str
    title: str
    subtitle: str
    reachable: bool


@dataclass(frozen=True)
class SpaceRow:
    key: str
    title: str
    count: int


def space_rows(state: AppState, host_id: str = "", *, shells: bool = False) -> tuple[SpaceRow, ...]:
    """Workspace counts before search/status filters; IDs always include the host."""
    agent_tabs = {(a.host_id, a.tab_id) for a in state.inventory.agents.values()}
    if shells:
        counts = Counter(
            scope_key(tab.host_id, tab.workspace_id)
            for tab in state.inventory.tabs.values()
            if (tab.host_id, tab.id) not in agent_tabs
        )
    else:
        counts = Counter(
            scope_key(agent.host_id, agent.workspace_id)
            for agent in state.inventory.agents.values()
        )
    rows = (
        SpaceRow(scope_key(w.host_id, w.id), w.label or w.id, counts[scope_key(w.host_id, w.id)])
        for w in state.inventory.workspaces.values()
        if not host_id or w.host_id == host_id
    )
    return tuple(sorted(rows, key=lambda row: (row.title.casefold(), row.key)))


@dataclass(frozen=True)
class BannerModel:
    tone: str  # error | warning | info
    text: str
    action: str  # "" when no action
    delay_seconds: float = 0
    not_live_since: float | None = None


def _matches(query: str, *fields: str) -> bool:
    if not query:
        return True
    needle = query.casefold()
    return any(needle in field.casefold() for field in fields)


def agent_rows(
    state: AppState,
    query: str = "",
    host_id: str = "",
    project_id: str = "",
    status: str = "",
    *,
    project_host: str = "",
) -> tuple[AgentRow, ...]:
    """Agent rows keyed by scoped key. `host_id` narrows to one
    host (the host filter); "" keeps every host."""
    rows: list[AgentRow] = []
    names = agent_names(state)
    for agent in state.inventory.agents.values():
        if host_id and agent.host_id != host_id:
            continue
        if project_host and agent.host_id != project_host:
            continue
        if project_id and agent.workspace_id != project_id:
            continue
        if status and agent.status.value != status:
            continue
        workspace = state.inventory.workspaces.get(scope_key(agent.host_id, agent.workspace_id))
        key = scope_key(agent.host_id, agent.terminal_id)
        title = names.get(key, agent.terminal_id)
        subject = agent_subject(state, key)
        subtitle = " · ".join(
            part
            for part in (
                agent.agent,
                workspace.label if workspace else "",
                agent.status.value,
            )
            if part
        )
        if not _matches(query, title, subject, subtitle, agent.terminal_id):
            continue
        rows.append(
            AgentRow(
                key=key,
                title=title,
                subject=subject,
                subtitle=subtitle,
                status=agent.status,
                focused=agent.focused,
                host_id=agent.host_id,
                workspace_id=agent.workspace_id,
                agent=agent.agent,
            )
        )
    rows.sort(key=lambda r: (status_rank(r.status.value), r.title.casefold(), r.key))
    return tuple(rows)


def shell_rows(
    state: AppState, query: str = "", host_id: str = "", space_key: str = ""
) -> tuple[ShellRow, ...]:
    """Shell rows keyed by scoped key. Agent-owned tabs compare
    as (host_id, tab_id) pairs so an h1 agent tab never hides an h2
    shell twin. `host_id` narrows to one host; "" keeps every host."""
    agent_tabs = {(a.host_id, a.tab_id) for a in state.inventory.agents.values()}
    rows: list[ShellRow] = []
    for tab in state.inventory.tabs.values():
        if host_id and tab.host_id != host_id:
            continue
        if space_key and scope_key(tab.host_id, tab.workspace_id) != space_key:
            continue
        if (tab.host_id, tab.id) in agent_tabs:
            continue
        workspace = state.inventory.workspaces.get(scope_key(tab.host_id, tab.workspace_id))
        title = tab.label or tab.id
        subtitle = " · ".join(
            part
            for part in (
                workspace.label if workspace else "",
                f"{tab.pane_count} pane" + ("s" if tab.pane_count != 1 else ""),
            )
            if part
        )
        if not _matches(query, title, subtitle, tab.id):
            continue
        rows.append(ShellRow(key=scope_key(tab.host_id, tab.id), title=title, subtitle=subtitle))
    rows.sort(key=lambda r: r.title.casefold())
    return tuple(rows)


def workspace_shells(state: AppState, host_id: str, workspace_id: str) -> tuple[Tab, ...]:
    """Pinned AgentDetailView: ordinary shells in this host/workspace only."""
    agent_tabs = {(agent.host_id, agent.tab_id) for agent in state.inventory.agents.values()}
    return tuple(
        sorted(
            (
                tab
                for tab in state.inventory.tabs.values()
                if tab.host_id == host_id
                and tab.workspace_id == workspace_id
                and (tab.host_id, tab.id) not in agent_tabs
            ),
            key=lambda tab: ((tab.label or tab.id).casefold(), tab.id),
        )
    )


def host_rows(state: AppState) -> tuple[HostRow, ...]:
    rows: list[HostRow] = []
    for host in state.inventory.hosts.values():
        agents = sum(1 for a in state.inventory.agents.values() if a.host_id == host.id)
        tabs = sum(1 for t in state.inventory.tabs.values() if t.host_id == host.id)
        subtitle = (
            f"{agents} agent"
            + ("s" if agents != 1 else "")
            + f" · {tabs} tab"
            + ("s" if tabs != 1 else "")
            + (" · offline" if not host.reachable else "")
        )
        rows.append(
            HostRow(
                key=host.id,
                title=host.label or host.id,
                subtitle=subtitle,
                reachable=host.reachable,
            )
        )
    rows.sort(key=lambda r: (not r.reachable, r.title.casefold()))
    return tuple(rows)


def connection_banner(state: AppState) -> BannerModel | None:
    if state.revoked:
        return BannerModel("error", "Access revoked. Sign in again to continue.", "Sign in")
    conn = state.connection.state.value
    if conn == "host_offline":
        return BannerModel("warning", "Home host is offline. Showing last known state.", "Retry")
    if conn == "reconnecting":
        reason = state.connection.detail or "connection_lost"
        attempt = getattr(state.connection, "retry_attempt", 0) or 1
        return BannerModel(
            "info",
            f"Reconnecting ({reason}, attempt {attempt})…",
            "",
            delay_seconds=2,
            not_live_since=getattr(state.connection, "not_live_since", None),
        )
    if conn in ("connecting", "synchronizing", "authenticating"):
        # A routine host switch resyncs in well under a second; flashing a
        # banner that pushes the page down and back reads as a reload.
        # Only the first connection (nothing to show yet) gets a notice.
        if state.inventory.hosts:
            return None
        return BannerModel("info", "Connecting…", "")
    if state.connection.detail == "gap_stalled":
        return BannerModel(
            "warning", "Metadata sync stopped after repeated gaps. Showing stale state.", "Retry"
        )
    if state.needs_resync:
        return BannerModel("warning", "State is out of sync. Refresh to resynchronize.", "Refresh")
    if state.stale:
        return BannerModel("warning", "Showing stale state.", "Retry")
    if state.inventory.truncated:
        return BannerModel("info", "Some sessions are hidden by inventory limits.", "")
    return None


@dataclass(frozen=True)
class DiagnosticsModel:
    connection: str
    gateway_reachable: bool
    socket_status: str
    host_label: str
    host_last_seen: str
    host_protocol: str
    cursor_preview: str
    correlation_preview: str
    app_version: str


def diagnostics(
    state: AppState,
    *,
    now_ms: int,
    gateway_reachable: bool,
    socket_status: str,
    app_version: str,
    correlation_id: str = "",
) -> DiagnosticsModel:
    host = state.inventory.hosts.get(state.connection.host_id)
    cursor = state.connection.cursor
    return DiagnosticsModel(
        connection=state.connection.state.value,
        gateway_reachable=gateway_reachable,
        socket_status=socket_status,
        host_label=host.label if host else state.connection.host_id,
        host_last_seen=relative_ms(now_ms, host.last_seen_ms) if host else "never",
        host_protocol=f"protocol {host.protocol} / {host.version}" if host else "unknown",
        cursor_preview=f"{cursor[:16]}…" if len(cursor) > 16 else (cursor or "none"),
        correlation_preview=f"…{correlation_id[-4:]}" if correlation_id else "none",
        app_version=app_version,
    )


def relative_ms(now_ms: int, then_ms: int) -> str:
    if then_ms <= 0:
        return "never"
    delta = max(0, now_ms - then_ms) // 1000
    if delta < 60:
        return "just now"
    if delta < 3600:
        return f"{delta // 60}m ago"
    if delta < 86400:
        return f"{delta // 3600}h ago"
    return f"{delta // 86400}d ago"
