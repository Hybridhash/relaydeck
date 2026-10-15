"""Responsive console shell.

One component set at all widths; only the arrangement changes:
below 600 a bottom bar with one screen at a time, 600-1099 a rail
with one screen at a time, at/above 1100 rail + list + live detail
with a collapsible list.

Detail lifecycle honesty (correction review concern): the detail
pane takes a BUILDER, not a live object. The builder runs once per
shell build at the single placement point; keyed reconciliation
preserves state while the layout branch is unchanged, but crossing
a breakpoint or collapse boundary re-parents the built subtree and
Flet remounts it. Object identity across those branches proves
nothing about retention. The application keeps live-terminal retention in the
existing stable owner (the retained_views parent in app.py); this
shell only frames whatever the builder returns.

All console data arrives as props: ordered counts, host chips, agent
items, eyebrow text. The application maps verified models; fixtures live only in
explicit tests. Agent ordering is enforced here (Blocked -> Working
-> Done -> Idle, Unknown last, stable within a group).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import flet as ft

from relaydeck.client.components.design_system import tokens as T
from relaydeck.client.components.design_system.cards import AgentCard, CollapsedAgentTile
from relaydeck.client.components.design_system.fonts import FAMILY_INTERFACE
from relaydeck.client.components.design_system.navigation import ConsoleBottomBar, ConsoleRail
from relaydeck.client.components.design_system.primitives import (
    ConsoleHeader,
    CountTile,
    DottedGround,
    HostChip,
    RoundIconButton,
    text_style,
)

ALL_HOSTS_ID = ""
ALL_HOSTS_LABEL = "All hosts"


@dataclass(frozen=True)
class AgentListItem:
    """One console row, as the application maps it from verified inventory."""

    agent_id: str
    name: str
    status: str
    subtitle: str
    meta_left: str
    meta_right: str


def _counts_row(counts: tuple[tuple[str, int], ...]) -> Any:
    tiles: list[Any] = []
    for status, count in counts:
        tile = CountTile(status, count)
        tile.expand = True  # equal-flex thirds, set pre-mount
        tiles.append(tile)
    return ft.Row(key="console-counts", spacing=T.GAP_CARD, controls=tiles)


def _hosts_row(
    hosts: tuple[tuple[str, str], ...],
    selected_host: str,
    on_select_host: Callable[[str], Any],
) -> Any:
    return ft.Row(
        key="console-hosts",
        spacing=T.GAP_CHIP,
        scroll=ft.ScrollMode.AUTO,
        controls=[
            HostChip(ALL_HOSTS_ID, ALL_HOSTS_LABEL, selected_host == ALL_HOSTS_ID, on_select_host),
            *(HostChip(hid, label, hid == selected_host, on_select_host) for hid, label in hosts),
        ],
    )


def _cards_list(
    agents: tuple[AgentListItem, ...],
    selected_id: str,
    on_select_agent: Callable[[str], Any],
    loading: bool,
    error: str | None,
    on_retry: Callable[[], Any] | None,
    empty_hint: str,
) -> Any:
    if loading:
        return ft.Column(
            key="console-list-loading",
            expand=True,
            alignment=ft.MainAxisAlignment.CENTER,
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            controls=[
                ft.ProgressRing(color=T.PRIMARY_FILL),
                ft.Text(
                    "Loading agents…",
                    style=text_style(FAMILY_INTERFACE, T.TYPE_CARD_LINE, T.TEXT_MUTED),
                ),
            ],
        )
    if error:
        controls: list[Any] = [
            ft.Text(error, style=text_style(FAMILY_INTERFACE, T.TYPE_CARD_LINE, T.TEXT_SECONDARY))
        ]
        if on_retry is not None:
            controls.append(
                ft.Container(
                    key="console-list-retry",
                    height=T.MIN_TOUCH,
                    padding=ft.Padding.symmetric(horizontal=22),
                    border_radius=ft.BorderRadius.all(12),
                    bgcolor=T.PRIMARY_FILL,
                    alignment=ft.Alignment.CENTER,
                    on_click=lambda e: on_retry(),
                    content=ft.Text(
                        "Retry", style=text_style(FAMILY_INTERFACE, T.TYPE_BUTTON, T.ON_PRIMARY)
                    ),
                )
            )
        return ft.Column(
            key="console-list-error",
            expand=True,
            spacing=T.GAP_CARD,
            alignment=ft.MainAxisAlignment.CENTER,
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            controls=controls,
        )
    ordered = T.sort_keys(list(agents), lambda a: a.status)
    if not ordered:
        return ft.Column(
            key="console-list-empty",
            expand=True,
            alignment=ft.MainAxisAlignment.CENTER,
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            controls=[
                ft.Text(
                    empty_hint, style=text_style(FAMILY_INTERFACE, T.TYPE_CARD_LINE, T.TEXT_MUTED)
                )
            ],
        )
    return ft.Column(
        key="console-cards",
        expand=True,
        spacing=T.GAP_CARD,
        scroll=ft.ScrollMode.AUTO,
        controls=[
            AgentCard(
                agent_id=item.agent_id,
                name=item.name,
                status=item.status,
                subtitle=item.subtitle,
                meta_left=item.meta_left,
                meta_right=item.meta_right,
                selected=item.agent_id == selected_id,
                on_select=on_select_agent,
            )
            for item in ordered
        ],
    )


def _list_pane(
    eyebrow: str,
    title: str,
    counts: tuple[tuple[str, int], ...],
    hosts: tuple[tuple[str, str], ...],
    selected_host: str,
    on_select_host: Callable[[str], Any],
    agents: tuple[AgentListItem, ...],
    selected_id: str,
    on_select_agent: Callable[[str], Any],
    on_search: Callable[[], Any],
    on_new_agent: Callable[[], Any],
    on_toggle_collapse: Callable[[], Any] | None,
    loading: bool,
    error: str | None,
    on_retry: Callable[[], Any] | None,
    empty_hint: str,
) -> Any:
    return ft.Column(
        key="console-list",
        expand=True,
        spacing=T.GAP_SECTION,
        scroll=ft.ScrollMode.HIDDEN,
        controls=[
            ConsoleHeader(eyebrow, title, on_search, on_new_agent, on_toggle_collapse, False),
            _counts_row(counts),
            _hosts_row(hosts, selected_host, on_select_host),
            _cards_list(agents, selected_id, on_select_agent, loading, error, on_retry, empty_hint),
        ],
    )


def _strip_pane(
    agents: tuple[AgentListItem, ...],
    selected_id: str,
    on_select_agent: Callable[[str], Any],
    on_toggle_collapse: Callable[[], Any],
    on_new_agent: Callable[[], Any],
) -> Any:
    ordered = T.sort_keys(list(agents), lambda a: a.status)
    return ft.Container(
        key="console-strip",
        width=T.COLLAPSED_WIDTH,
        padding=ft.Padding.symmetric(vertical=16, horizontal=12),
        border_radius=ft.BorderRadius.all(T.RADIUS_PANE),
        bgcolor=T.PANEL,
        border=ft.Border.all(1, T.BORDER),
        content=ft.Column(
            spacing=T.GAP_SECTION,
            scroll=ft.ScrollMode.AUTO,
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            controls=[
                RoundIconButton(
                    ft.Icons.KEYBOARD_DOUBLE_ARROW_RIGHT,
                    "Expand agent list",
                    on_toggle_collapse,
                    key="console-expand-toggle",
                ),
                *(
                    CollapsedAgentTile(
                        agent_id=item.agent_id,
                        name=item.name,
                        status=item.status,
                        selected=item.agent_id == selected_id,
                        on_select=on_select_agent,
                    )
                    for item in ordered
                ),
                RoundIconButton(
                    ft.Icons.ADD,
                    "New agent",
                    on_new_agent,
                    tone="lime",
                    key="console-new-agent-strip",
                ),
            ],
        ),
    )


@ft.component
def ConsoleShell(
    width: float,
    destination: str,
    on_select_destination: Callable[[str], Any],
    eyebrow: str,
    title: str,
    counts: tuple[tuple[str, int], ...],
    hosts: tuple[tuple[str, str], ...],
    selected_host: str,
    on_select_host: Callable[[str], Any],
    agents: tuple[AgentListItem, ...],
    selected_id: str,
    on_select_agent: Callable[[str], Any],
    detail_builder: Callable[[], Any],
    show_detail: bool,
    collapsed: bool,
    on_toggle_collapse: Callable[[], Any],
    on_search: Callable[[], Any],
    on_new_agent: Callable[[], Any],
    loading: bool = False,
    error: str | None = None,
    on_retry: Callable[[], Any] | None = None,
    empty_hint: str = "No agents match these filters.",
    list_width: float = T.LIST_WIDTH,
    asset_base: str = "design_system",
) -> Any:
    """Full console: navigation + filterable agent list + detail slot."""
    list_pane = _list_pane(
        eyebrow,
        title,
        counts,
        hosts,
        selected_host,
        on_select_host,
        agents,
        selected_id,
        on_select_agent,
        on_search,
        on_new_agent,
        on_toggle_collapse if width >= T.BREAKPOINT_SPLIT else None,
        loading,
        error,
        on_retry,
        empty_hint,
    )
    if width < T.BREAKPOINT_SPLIT:
        content = detail_builder() if show_detail else list_pane
        if width < T.BREAKPOINT_RAIL:
            # Phone: bottom bar; SafeArea supplies the real top inset.
            body: Any = ft.Column(
                spacing=0,
                controls=[
                    ft.SafeArea(
                        content=ft.Container(padding=ft.Padding.all(T.PAD_PHONE), content=content),
                        expand=True,
                    ),
                    ConsoleBottomBar(destination, on_select_destination),
                ],
            )
        else:
            body = ft.Row(
                spacing=0,
                controls=[
                    ConsoleRail(destination, on_select_destination),
                    ft.Container(
                        expand=True,
                        padding=ft.Padding.all(T.PAD_WEB),
                        content=content,
                    ),
                ],
            )
        return DottedGround(body, asset_base)
    # Wide: rail + list/strip + live detail pane.
    if collapsed:
        side: Any = _strip_pane(
            agents, selected_id, on_select_agent, on_toggle_collapse, on_new_agent
        )
    else:
        side = ft.Container(width=list_width, content=list_pane)
    return DottedGround(
        ft.Row(
            spacing=0,
            controls=[
                ConsoleRail(destination, on_select_destination),
                ft.Container(
                    expand=True,
                    padding=ft.Padding.all(T.PAD_WEB),
                    content=ft.Row(
                        spacing=T.PANE_GAP,
                        controls=[
                            side,
                            ft.Container(
                                key="console-detail",
                                expand=True,
                                padding=ft.Padding.all(T.PANE_PAD),
                                border_radius=ft.BorderRadius.all(T.RADIUS_PANE),
                                bgcolor=T.PANEL,
                                border=ft.Border.all(1, T.BORDER),
                                content=detail_builder(),
                            ),
                        ],
                    ),
                ),
            ],
        ),
        asset_base,
    )
