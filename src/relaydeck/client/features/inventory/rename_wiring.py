"""Facts the rename UI needs about one agent row, derived from rows and verified inventory."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from relaydeck.client.features.inventory.rename_sheet import SheetContext
from relaydeck.client.features.inventory.selectors import AgentRow, agent_names
from relaydeck.client.theme import agent_identity
from relaydeck.domain.models import AppState, scope_key

# agent key -> (proposed name, title it replaces, attempt number)
Overrides = Mapping[str, tuple[str, str, int]]


def space_label(row: AgentRow, state: AppState | None) -> str:
    if state is None:
        return ""
    workspace = state.inventory.workspaces.get(scope_key(row.host_id, row.workspace_id))
    return workspace.label if workspace else ""


def _same_space(row: AgentRow, other: AgentRow) -> bool:
    return (
        other.key != row.key
        and other.host_id == row.host_id
        and other.workspace_id == row.workspace_id
    )


def taken_names(row: AgentRow, rows: Sequence[AgentRow], state: AppState | None) -> tuple[str, ...]:
    """Names other agents show in the same space (host and workspace)."""
    names = {other.title for other in rows if _same_space(row, other)}
    if state is not None:
        shown = agent_names(state)
        names |= {
            shown[key]
            for key, agent in state.inventory.agents.items()
            if key in shown
            and (agent.host_id, agent.workspace_id) == (row.host_id, row.workspace_id)
            and key != row.key
        }
    names.discard(row.title)
    return tuple(sorted(names))


def sheet_context(row: AgentRow, state: AppState | None, dark: bool) -> SheetContext:
    identity = agent_identity(row.agent, dark)
    return SheetContext(
        task=row.subject,
        provider=identity.label if row.agent else "",
        space=space_label(row, state),
        status=row.status.value.title(),
        bar_color=identity.color,
    )


def card_label(row: AgentRow, title: str, space: str, pinned: bool = False) -> str:
    """Spoken summary of a card: name, status, task, provider, space, time, unread, pinned."""
    identity = agent_identity(row.agent, True)
    parts = [
        title,
        row.status.value.title(),
        row.subject,
        identity.label if row.agent else "",
        space,
        row.subtitle.rsplit(" · ", 1)[-1] if row.subtitle else "",
        "Unread" if row.unread else "",
        "Pinned" if pinned else "",
    ]
    return ", ".join(part for part in parts if part)


def shown_title(row: AgentRow, overrides: Overrides) -> str:
    """The optimistic name while the page still shows the name it replaced."""
    entry = overrides.get(row.key)
    return entry[0] if entry and entry[1] == row.title else row.title
