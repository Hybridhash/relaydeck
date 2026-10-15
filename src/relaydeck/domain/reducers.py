"""Pure state transitions.

Every function takes an AppState and returns a new one; inputs are never
mutated. Late/out-of-order data cannot overwrite newer state: snapshots
replace coherently per host, events apply in sequence with idempotent
duplicates, and gaps force an explicit resync instead of guessing.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from relaydeck.contracts.catalog import catalog_from_wire
from relaydeck.domain.models import (
    Agent,
    AgentStatus,
    AppState,
    CommandRecord,
    CommandStatus,
    Connection,
    ConnectionState,
    EventType,
    Host,
    Inventory,
    MetadataEvent,
    Pane,
    Selection,
    Snapshot,
    Tab,
    Workspace,
    scope_key,
    unscope_key,
)

MAX_WORKSPACES = 256
MAX_TABS = 1024
MAX_PANES = 4096
MAX_AGENTS = 4096
MAX_COMMANDS = 1000


def _truncate[T](items: dict[str, T], limit: int) -> tuple[dict[str, T], bool]:
    if len(items) <= limit:
        return dict(items), False
    kept = dict(list(items.items())[:limit])
    return kept, True


def _truncate_inventory(inv: Inventory) -> Inventory:
    workspaces, t1 = _truncate(inv.workspaces, MAX_WORKSPACES)
    tabs, t2 = _truncate(inv.tabs, MAX_TABS)
    panes, t3 = _truncate(inv.panes, MAX_PANES)
    agents, t4 = _truncate(inv.agents, MAX_AGENTS)
    return replace(
        inv,
        workspaces=workspaces,
        tabs=tabs,
        panes=panes,
        agents=agents,
        truncated=inv.truncated or t1 or t2 or t3 or t4,
    )


def apply_snapshot(state: AppState, snapshot: Snapshot) -> AppState:
    """Coherent per-host replacement. Clears stale/resync for that host and
    drops the selection only when its resource no longer exists. Snapshots
    for hosts outside the verified grant set are rejected whole (the
    identical state is returned): a stale or unauthorized snapshot must
    never resurrect revoked inventory."""
    if state.revoked:
        return state
    granted = state.granted_hosts
    if granted is not None and snapshot.host_id not in granted:
        return state
    hid = snapshot.host_id
    workspaces = {scope_key(hid, w.id): w for w in snapshot.workspaces if w.host_id == hid}
    tabs = {scope_key(hid, t.id): t for t in snapshot.tabs if t.host_id == hid}
    panes = {scope_key(hid, p.id): p for p in snapshot.panes if p.host_id == hid}
    agents = {scope_key(hid, a.terminal_id): a for a in snapshot.agents if a.host_id == hid}
    hosts = dict(state.inventory.hosts)
    hosts[snapshot.host_id] = Host(
        id=snapshot.host_id,
        label=hosts.get(snapshot.host_id, Host(id=snapshot.host_id, label="")).label
        or snapshot.host_id,
        epoch=snapshot.host_epoch,
        last_seen_ms=snapshot.taken_ms,
        reachable=True,
        protocol=snapshot.host_protocol,
        version=snapshot.host_version,
        capabilities=snapshot.host_capabilities,
        agent_catalog=snapshot.agent_catalog,
    )

    # Preserve other hosts' entries; replace this host's slice.
    def _keep_other[T](current: dict[str, T], host_attr: str) -> dict[str, T]:
        return {k: v for k, v in current.items() if getattr(v, host_attr, "") != snapshot.host_id}

    merged = Inventory(
        hosts=hosts,
        workspaces={**_keep_other(state.inventory.workspaces, "host_id"), **workspaces},
        tabs={**_keep_other(state.inventory.tabs, "host_id"), **tabs},
        panes={**_keep_other(state.inventory.panes, "host_id"), **panes},
        agents={**_keep_other(state.inventory.agents, "host_id"), **agents},
    )
    merged = _truncate_inventory(merged)
    connection = replace(
        state.connection,
        host_id=snapshot.host_id,
        cursor=snapshot.cursor,
        last_sequence=0,
    )
    selection = state.selection
    if selection.host_id == snapshot.host_id and selection.session_key:
        alive = selection.session_key in merged.agents or selection.session_key in merged.tabs
        if not alive:
            selection = replace(selection, session_key="", terminal_kind="")
    return replace(
        state,
        inventory=merged,
        connection=connection,
        selection=selection,
        stale=False,
        needs_resync=False,
    )


def merge_hosts(state: AppState, entries: list[dict[str, Any]]) -> AppState:
    """Merge a /v1/hosts listing into inventory (host switching).

    Adds unknown hosts as unsurfaced entries (reachability as listed,
    no capabilities assumed); refreshes label/epoch/reachability/
    protocol/version on known hosts while PRESERVING synced
    capabilities and last-seen (the list carries neither). Malformed
    entries are skipped.

    The listing is the COMPLETE authorized set: hosts absent from it
    lost their grant and are reconciled out with every inventory slice
    (workspaces/tabs/panes/agents) plus the selection when it points
    at a revoked host. The cleared selection bumps its generation, so
    in-flight fetches and input callbacks for the revoked target
    cannot land anywhere (the select_session invalidation contract).
    Connection state stays controller-owned: this reducer never marks
    global revocation for a per-host grant loss. The verified grant ids
    are recorded on the state so later snapshots/events for ungranted
    hosts are rejected instead of resurrecting revoked inventory.
    """
    if state.revoked:
        # A revoked identity is terminal for this state lineage: late
        # pre-revocation listings must not undo clearing. Fresh
        # authentication establishes a new context explicitly (a new
        # controller), never by resurrecting into this one.
        return state
    hosts = dict(state.inventory.hosts)
    granted: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        host_id = str(entry.get("host_id", ""))
        if not host_id:
            continue
        granted.add(host_id)
        upstream = entry.get("upstream", {})
        if not isinstance(upstream, dict):
            upstream = {}
        try:
            protocol = int(upstream.get("protocol", 0) or 0)
        except (TypeError, ValueError):
            protocol = 0
        current = hosts.get(host_id)
        if current is None:
            hosts[host_id] = Host(
                id=host_id,
                label=str(entry.get("label", host_id)),
                epoch=str(entry.get("epoch", "")),
                reachable=bool(entry.get("reachable", False)),
                protocol=protocol,
                version=str(upstream.get("version", "")),
                agent_catalog=catalog_from_wire(entry.get("agent_catalog")),
            )
        else:
            hosts[host_id] = replace(
                current,
                label=str(entry.get("label", current.label)),
                epoch=str(entry.get("epoch", current.epoch)),
                reachable=bool(entry.get("reachable", current.reachable)),
                protocol=protocol,
                version=str(upstream.get("version", current.version)),
                agent_catalog=(
                    catalog_from_wire(entry["agent_catalog"])
                    if "agent_catalog" in entry
                    else current.agent_catalog
                    if str(entry.get("epoch", current.epoch)) == current.epoch
                    else None
                ),
            )
    removed = set(hosts) - granted
    for host_id in removed:
        del hosts[host_id]
    if not removed:
        return replace(
            state,
            inventory=replace(state.inventory, hosts=hosts),
            granted_hosts=frozenset(granted),
        )
    inventory = state.inventory

    def _keep_granted[T](current: dict[str, T]) -> dict[str, T]:
        return {k: v for k, v in current.items() if getattr(v, "host_id", "") in granted}

    selection = state.selection
    if selection.host_id in removed:
        selection = Selection(
            host_id="",
            session_key="",
            terminal_kind="",
            generation=selection.generation + 1,
        )
    return replace(
        state,
        inventory=replace(
            state.inventory,
            hosts=hosts,
            workspaces=_keep_granted(inventory.workspaces),
            tabs=_keep_granted(inventory.tabs),
            panes=_keep_granted(inventory.panes),
            agents=_keep_granted(inventory.agents),
        ),
        selection=selection,
        granted_hosts=frozenset(granted),
    )


class EventAction(str):
    APPLIED = "applied"
    DUPLICATE = "duplicate"
    STALE_EPOCH = "stale_epoch"
    GAP = "gap"
    IGNORED_TYPE = "ignored_type"


def apply_event(state: AppState, event: MetadataEvent) -> tuple[AppState, str]:
    """Ordered update. Returns (new_state, action). Gaps and stale epochs
    never mutate inventory; duplicates are idempotent."""
    if state.revoked:
        return state, EventAction.IGNORED_TYPE
    conn = state.connection
    if event.host_id != conn.host_id:
        return state, EventAction.IGNORED_TYPE
    granted = state.granted_hosts
    if granted is not None and event.host_id not in granted:
        return state, EventAction.IGNORED_TYPE
    host = state.inventory.hosts.get(event.host_id)
    if host is not None and event.host_epoch != host.epoch:
        return replace(state, needs_resync=True), EventAction.STALE_EPOCH
    if event.sequence <= conn.last_sequence:
        return _apply_upsert(state, event, duplicate=True)
    if event.sequence > conn.last_sequence + 1:
        return replace(state, needs_resync=True), EventAction.GAP
    return _apply_upsert(state, event, duplicate=False)


def _apply_upsert(
    state: AppState, event: MetadataEvent, *, duplicate: bool
) -> tuple[AppState, str]:
    action = EventAction.DUPLICATE if duplicate else EventAction.APPLIED
    conn = state.connection
    if not duplicate:
        conn = replace(conn, last_sequence=event.sequence, cursor=event.cursor)
    kind = event.type
    if kind == EventType.RESYNC_REQUIRED:
        return replace(state, connection=conn, needs_resync=True), action
    if kind == EventType.HOST_STATUS:
        hosts = dict(state.inventory.hosts)
        current = hosts.get(event.host_id)
        reachable = bool(event.payload.get("reachable", True))
        last_seen = int(event.payload.get("last_seen_ms", 0) or 0)
        if current is None:
            hosts[event.host_id] = Host(
                id=event.host_id,
                label=str(event.payload.get("label", event.host_id)),
                epoch=event.host_epoch,
                last_seen_ms=last_seen,
                reachable=reachable,
            )
        else:
            hosts[event.host_id] = Host(
                id=current.id,
                label=str(event.payload.get("label", current.label)),
                epoch=event.host_epoch,
                last_seen_ms=last_seen or current.last_seen_ms,
                reachable=reachable,
                protocol=current.protocol,
                version=current.version,
                capabilities=current.capabilities,
            )
        inv = replace(state.inventory, hosts=hosts)
        return replace(state, connection=conn, inventory=inv), action
    if kind == EventType.RESOURCE_REMOVED:
        return _apply_removal(state, conn, event), action
    if kind == EventType.COMMAND_UPDATED:
        return _apply_command(state, conn, event), action
    if kind == EventType.CAPABILITIES_CHANGED:
        hosts = dict(state.inventory.hosts)
        current = hosts.get(event.host_id)
        if current is not None:
            caps = event.payload.get("capabilities", [])
            hosts[event.host_id] = Host(
                id=current.id,
                label=current.label,
                epoch=current.epoch,
                last_seen_ms=current.last_seen_ms,
                reachable=current.reachable,
                protocol=current.protocol,
                version=current.version,
                capabilities=frozenset(str(c) for c in caps),
            )
            inv = replace(state.inventory, hosts=hosts)
            return replace(state, connection=conn, inventory=inv), action
        return replace(state, connection=conn), action
    if kind == EventType.AGENT_UPSERTED:
        return _apply_agent(state, conn, event), action
    if kind == EventType.WORKSPACE_UPSERTED:
        return _apply_workspace(state, conn, event), action
    if kind == EventType.TAB_UPSERTED:
        return _apply_tab(state, conn, event), action
    if kind == EventType.PANE_UPSERTED:
        return _apply_pane(state, conn, event), action
    return replace(state, connection=conn), EventAction.IGNORED_TYPE


def _apply_agent(state: AppState, conn: Connection, event: MetadataEvent) -> AppState:
    agents = dict(state.inventory.agents)
    key = scope_key(event.host_id, event.resource_id)
    current = agents.get(key)
    if current is not None and event.resource_revision <= current.revision:
        return replace(state, connection=conn)
    payload = event.payload
    agents[key] = Agent(
        terminal_id=event.resource_id,
        host_id=event.host_id,
        workspace_id=str(payload.get("workspace_id", current.workspace_id if current else "")),
        tab_id=str(payload.get("tab_id", current.tab_id if current else "")),
        pane_id=str(payload.get("pane_id", current.pane_id if current else "")),
        name=str(payload.get("name", current.name if current else "")),
        agent=str(payload.get("agent", current.agent if current else "")),
        title=str(payload.get("title", current.title if current else "")),
        status=AgentStatus.coerce(
            str(payload.get("status", current.status.value if current else "unknown"))
        ),
        focused=bool(payload.get("focused", current.focused if current else False)),
        launch_pending=bool(
            payload.get("launch_pending", current.launch_pending if current else False)
        ),
        interactive_ready=bool(
            payload.get("interactive_ready", current.interactive_ready if current else False)
        ),
        state_change_seq=int(
            payload.get("state_change_seq", current.state_change_seq if current else 0)
        ),
        revision=event.resource_revision,
    )
    inv = _truncate_inventory(replace(state.inventory, agents=agents))
    return replace(state, connection=conn, inventory=inv)


def _apply_workspace(state: AppState, conn: Connection, event: MetadataEvent) -> AppState:
    workspaces = dict(state.inventory.workspaces)
    payload = event.payload
    key = scope_key(event.host_id, event.resource_id)
    current = workspaces.get(key)
    workspaces[key] = Workspace(
        id=event.resource_id,
        host_id=event.host_id,
        number=int(payload.get("number", current.number if current else 0)),
        label=str(payload.get("label", current.label if current else "")),
        focused=bool(payload.get("focused", False)),
        pane_count=int(payload.get("pane_count", current.pane_count if current else 0)),
        tab_count=int(payload.get("tab_count", current.tab_count if current else 0)),
        active_tab_id=str(payload.get("active_tab_id", current.active_tab_id if current else "")),
        agent_status=AgentStatus.coerce(
            str(
                payload.get(
                    "agent_status",
                    current.agent_status.value if current else "unknown",
                )
            )
        ),
    )
    inv = _truncate_inventory(replace(state.inventory, workspaces=workspaces))
    return replace(state, connection=conn, inventory=inv)


def _apply_tab(state: AppState, conn: Connection, event: MetadataEvent) -> AppState:
    tabs = dict(state.inventory.tabs)
    payload = event.payload
    key = scope_key(event.host_id, event.resource_id)
    current = tabs.get(key)
    tabs[key] = Tab(
        id=event.resource_id,
        host_id=event.host_id,
        workspace_id=str(payload.get("workspace_id", current.workspace_id if current else "")),
        number=int(payload.get("number", current.number if current else 0)),
        label=str(payload.get("label", current.label if current else "")),
        focused=bool(payload.get("focused", False)),
        pane_count=int(payload.get("pane_count", current.pane_count if current else 0)),
        agent_status=AgentStatus.coerce(
            str(
                payload.get(
                    "agent_status",
                    current.agent_status.value if current else "unknown",
                )
            )
        ),
    )
    inv = _truncate_inventory(replace(state.inventory, tabs=tabs))
    return replace(state, connection=conn, inventory=inv)


def _apply_pane(state: AppState, conn: Connection, event: MetadataEvent) -> AppState:
    panes = dict(state.inventory.panes)
    payload = event.payload
    key = scope_key(event.host_id, event.resource_id)
    current = panes.get(key)
    panes[key] = Pane(
        id=event.resource_id,
        host_id=event.host_id,
        workspace_id=str(payload.get("workspace_id", current.workspace_id if current else "")),
        tab_id=str(payload.get("tab_id", current.tab_id if current else "")),
        focused=bool(payload.get("focused", False)),
        agent_status=AgentStatus.coerce(
            str(
                payload.get(
                    "agent_status",
                    current.agent_status.value if current else "unknown",
                )
            )
        ),
    )
    agents = state.inventory.agents
    if "agent_status" in payload:
        # the dock's agent tabs read `inventory.agents`, so a pane status change
        # must reach the agent that lives in that pane (same host, matching pane id).
        status = AgentStatus.coerce(str(payload["agent_status"]))
        agents = {
            k: replace(v, status=status)
            if v.host_id == event.host_id and v.pane_id == event.resource_id and v.status != status
            else v
            for k, v in agents.items()
        }
    inv = _truncate_inventory(replace(state.inventory, panes=panes, agents=agents))
    return replace(state, connection=conn, inventory=inv)


def _apply_removal(state: AppState, conn: Connection, event: MetadataEvent) -> AppState:
    # Scoped removal: a bare resource id repeats across hosts, so only
    # this host's scoped entry is dropped; other hosts' twins survive.
    key = scope_key(event.host_id, event.resource_id)
    workspaces = {k: v for k, v in state.inventory.workspaces.items() if k != key}
    tabs = {k: v for k, v in state.inventory.tabs.items() if k != key}
    panes = {k: v for k, v in state.inventory.panes.items() if k != key}
    agents = {k: v for k, v in state.inventory.agents.items() if k != key}
    removed_agents = {k for k in state.inventory.agents if k not in agents}
    if key in state.inventory.tabs:
        # Herdr emits only tab_closed for a closed tab, so its panes and the
        # agents in it leave with it (same host only; a bare tab id repeats across hosts).
        panes = {
            k: v
            for k, v in panes.items()
            if not (v.host_id == event.host_id and v.tab_id == event.resource_id)
        }
        in_tab = {
            k
            for k, v in agents.items()
            if v.host_id == event.host_id and v.tab_id == event.resource_id
        }
        agents = {k: v for k, v in agents.items() if k not in in_tab}
        removed_agents |= in_tab
    inv = replace(state.inventory, workspaces=workspaces, tabs=tabs, panes=panes, agents=agents)
    selection = state.selection
    if selection.session_key == key or selection.session_key in removed_agents:
        selection = replace(selection, session_key="", terminal_kind="")
    return replace(state, connection=conn, inventory=inv, selection=selection)


def _apply_command(state: AppState, conn: Connection, event: MetadataEvent) -> AppState:
    commands = dict(state.commands)
    current = commands.get(event.resource_id)
    status_raw = str(event.payload.get("status", ""))
    try:
        status = CommandStatus(status_raw)
    except ValueError:
        return replace(state, connection=conn)
    if current is None:
        raw_target = event.payload.get("target", {})
        target = (
            {str(k): str(v) for k, v in raw_target.items()} if isinstance(raw_target, dict) else {}
        )
        commands[event.resource_id] = CommandRecord(
            command_id=event.resource_id,
            host_id=event.host_id,
            kind=str(event.payload.get("kind", "")),
            target=target,
            status=status,
            error_code=str(event.payload.get("error_code", "")),
            updated_ms=event.observed_ms,
        )
    else:
        commands[event.resource_id] = replace(
            current,
            status=status,
            error_code=str(event.payload.get("error_code", current.error_code)),
            updated_ms=event.observed_ms,
        )
    if len(commands) > MAX_COMMANDS:
        # Drop oldest settled records first; pending ones are never dropped.
        settled = [
            (c.updated_ms, cid)
            for cid, c in commands.items()
            if c.status
            in (
                CommandStatus.SUCCEEDED,
                CommandStatus.FAILED,
                CommandStatus.REJECTED,
            )
        ]
        for _, cid in sorted(settled)[: len(commands) - MAX_COMMANDS]:
            del commands[cid]
    return replace(state, connection=conn, commands=commands)


def submit_command(state: AppState, record: CommandRecord) -> tuple[AppState, str]:
    """Client-side dedupe: same id + same digest returns existing (no
    redispatch); same id + different digest is a conflict. Revoked or
    missing-host states reject."""
    if state.revoked:
        return state, "rejected_revoked"
    if record.host_id != state.connection.host_id:
        return state, "rejected_host"
    commands = dict(state.commands)
    current = commands.get(record.command_id)
    if current is not None:
        if current.payload_digest == record.payload_digest:
            return state, "duplicate"
        return state, "conflict"
    commands[record.command_id] = record
    return replace(state, commands=commands), "submitted"


def select_session(state: AppState, host_id: str, session_key: str, terminal_kind: str) -> AppState:
    """Selection change bumps the generation so in-flight fetches and input
    callbacks for the previous target cannot land on the new one. A
    revoked identity cannot select (clearing is terminal for
    the lineage). A non-empty session_key must be a well-formed scoped
    key for this host; malformed or host-mismatched keys (including
    bare upstream ids) are rejected fail-closed with the state
    unchanged, so a bogus key can never steer input at a live target.
    """
    if state.revoked:
        return state
    if session_key:
        parts = unscope_key(session_key)
        if parts is None or parts[0] != host_id:
            return state
    return replace(
        state,
        selection=Selection(
            host_id=host_id,
            session_key=session_key,
            terminal_kind=terminal_kind,
            generation=state.selection.generation + 1,
        ),
    )


def set_connection(
    state: AppState, connection_state: ConnectionState, detail: str = ""
) -> AppState:
    conn = replace(state.connection, state=connection_state, detail=detail)
    stale = state.stale
    if connection_state in (
        ConnectionState.RECONNECTING,
        ConnectionState.HOST_OFFLINE,
        ConnectionState.DISCONNECTED,
    ):
        stale = True
    if connection_state == ConnectionState.LIVE:
        stale = False
    revoked = state.revoked or connection_state == ConnectionState.ACCESS_REVOKED
    return replace(state, connection=conn, stale=stale, revoked=revoked)


def revoke(state: AppState) -> AppState:
    """Verified identity revocation: terminate, invalidate,
    and CLEAR cached sensitive content (inventory, selection, commands,
    grants). Retention is for offline/reconnect only — a
    revoked identity keeps nothing behind the sign-in banner, so a
    later identity can never inherit this one's caches. The cleared
    selection bumps its generation to invalidate in-flight work."""
    conn = replace(state.connection, state=ConnectionState.ACCESS_REVOKED)
    return replace(
        state,
        connection=conn,
        inventory=Inventory(),
        selection=Selection(generation=state.selection.generation + 1),
        commands={},
        granted_hosts=None,
        revoked=True,
        stale=True,
    )


def mark_offline(state: AppState) -> AppState:
    conn = replace(state.connection, state=ConnectionState.HOST_OFFLINE)
    return replace(state, connection=conn, stale=True)
