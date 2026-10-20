"""Agent switcher rows for the phone header: pure ordering, no Flet."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from relaydeck.domain.models import scope_key

ACTIVE = ("blocked", "working")


@dataclass(frozen=True)
class SwitcherRow:
    key: str
    name: str
    space: str
    status: str
    current: bool


def switcher_rows(
    state: Any, host_id: str, current_key: str, names: Mapping[str, str]
) -> tuple[SwitcherRow, ...]:
    """Active agents first, then the rest; within each, the current space first, then name."""
    agents = state.inventory.agents
    current = agents.get(current_key)
    home = current.workspace_id if current is not None else None
    rows: list[tuple[tuple[Any, ...], SwitcherRow]] = []
    for key, agent in agents.items():
        if agent.host_id != host_id:
            continue
        workspace = state.inventory.workspaces.get(scope_key(host_id, agent.workspace_id))
        name = names.get(key, agent.terminal_id)
        status = agent.status.value
        order = (
            status not in ACTIVE,
            home is None or agent.workspace_id != home,
            name.casefold(),
            key,
        )
        row = SwitcherRow(
            key, name, workspace.label if workspace else "", status, key == current_key
        )
        rows.append((order, row))
    return tuple(row for _, row in sorted(rows, key=lambda item: item[0]))


def working_count(rows: Iterable[SwitcherRow]) -> int:
    return sum(1 for row in rows if row.status in ACTIVE)
