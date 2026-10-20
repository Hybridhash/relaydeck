"""Agent detail header.

Plain builders preserve control ancestry across layouts. State and hooks belong
to the parent component; builders receive only props and callbacks.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import flet as ft

from relaydeck.client.components.design_system import StatusPill
from relaydeck.client.components.design_system.terminal_window import (
    STATE_IN_CONTROL,
    MoreMenu,
    PillButton,
    StatePill,
    TerminalHeader,
)
from relaydeck.client.features.a11y.announcer import LiveRegion
from relaydeck.client.features.terminal.input_modes import (
    WORKFLOW_AGENT_COMPOSER,
)
from relaydeck.client.features.terminal.models import TerminalViewState
from relaydeck.client.features.terminal.pictures import (
    attach_button,
)
from relaydeck.client.features.terminal.surfaces.agent_switcher import SwitcherTitle
from relaydeck.client.features.terminal.switcher import SwitcherRow, working_count

if TYPE_CHECKING:
    pass

from relaydeck.client.features.terminal.surfaces.common import (
    AGENT_DISCONNECT_KEY,
    AGENT_HEADER_KEY,
    AGENT_MORE_KEY,
    AGENT_OPEN_SHELL_ITEM_KEY,
    AGENT_STATUS_KEY,
    AGENT_TITLE_KEY,
)


def _switcher_title(title: str, switcher: Any) -> Any:
    if switcher is None or not switcher[0]:
        return None
    rows, opened, toggle = switcher
    return SwitcherTitle(
        title=title, working=working_count(rows), open=opened, on_open=lambda: toggle("switcher")
    )


def build_agent_header(
    *,
    agent_status: str,
    agent_title: str,
    attach_enabled: bool,
    connection_live: bool,
    connection_text: str,
    host_line: str,
    on_attach_picture: Any,
    on_back: Callable[[], Any] | None,
    on_connection_action: Callable[[], Any] | None,
    on_disconnect: Callable[[], Any] | None,
    on_open_shell: Callable[[], Any] | None,
    on_operations: Callable[[], Any] | None,
    phone: bool,
    picture_action: Any,
    slim: bool,
    terminal: TerminalViewState,
    window: str,
    window_size: tuple[int, int],
    workflow: str,
    switcher: tuple[tuple[SwitcherRow, ...], bool, Callable[[str], None]] | None = None,
) -> Any:
    """The agent header (full, or the slim typing-mode / phone variant)."""
    status_pill = StatusPill(agent_status.lower(), label=agent_status)
    status_pill.content.key = AGENT_STATUS_KEY
    status_pill.content.value = agent_status
    pills: list[Any] = [
        LiveRegion(
            key="agent-status-live",
            text=agent_status,
            content=ft.Row(tight=True, controls=[status_pill]),
        ),  # type: ignore[call-arg]
        StatePill(window, key="agent-window-state"),
    ]
    if phone:
        pills = [StatePill(window, key="agent-window-state", compact=True)]
    actions: list[Any] = []
    if not slim and workflow != WORKFLOW_AGENT_COMPOSER:
        actions.append(
            attach_button(on_attach_picture, enabled=attach_enabled, action=picture_action)
        )
    menu: list[tuple[str, str, Callable[[], Any]]] = []
    if window == STATE_IN_CONTROL and on_disconnect is not None:
        if slim:
            # Phone header keeps its width for the name (android mock).
            menu.append((AGENT_DISCONNECT_KEY, "Release", on_disconnect))
        else:
            actions.append(
                PillButton("Release", on_disconnect, key=AGENT_DISCONNECT_KEY, tone="outline")
            )
    if slim and on_operations is not None:
        menu.append(("terminal-operations", "Operations to review", on_operations))
    if on_open_shell is not None:
        menu.append((AGENT_OPEN_SHELL_ITEM_KEY, "Open a shell here", on_open_shell))
    if menu or slim:
        actions.append(
            MoreMenu(
                menu,
                key=AGENT_MORE_KEY,
                compact=slim,
                information=(
                    (f"{AGENT_TITLE_KEY}-host", host_line),
                    (AGENT_STATUS_KEY, agent_status),
                    ("terminal-window-size", f"{window_size[0]} x {window_size[1]}"),
                    (
                        "terminal-window-status",
                        f"{terminal.status} · {terminal.lease_mode} · "
                        f"{terminal.epoch}@{terminal.applied_offset}",
                    ),
                )
                if slim
                else (),
            )
        )
    header = TerminalHeader(
        title=agent_title,
        host_line=host_line,
        pills=pills,
        actions=actions,
        on_back=on_back,
        title_key=AGENT_TITLE_KEY,
        phone=slim,
        mobile_terminal=slim,
        connection_text=connection_text,
        connection_live=connection_live and window == STATE_IN_CONTROL,
        on_connection_action=on_connection_action,
        title_control=_switcher_title(agent_title, switcher) if phone else None,
    )
    return ft.Container(key=AGENT_HEADER_KEY, content=header)
