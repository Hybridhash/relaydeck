"""Console agent cards and collapsed status tiles.

`AgentCard` is the full console row; `CollapsedAgentTile` is its 48px
strip form. Both are controlled: display props in, selection callback
out. Ordering, tooltips, status and selection survive collapsing.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import flet as ft

from relaydeck.client.components.design_system import tokens as T
from relaydeck.client.components.design_system.fonts import FAMILY_INTERFACE, FAMILY_MONO
from relaydeck.client.components.design_system.primitives import (
    AgentPill,
    StatusPill,
    _action_button,
    text_style,
)


def _selected_outline(selected: bool) -> list[ft.BoxShadow] | None:
    """3px light selection ring that never changes layout dimensions."""
    if not selected:
        return None
    return [
        ft.BoxShadow(
            color=T.TEXT,
            spread_radius=T.SELECT_OUTLINE_WIDTH,
            blur_radius=0,
            offset=ft.Offset(0, 0),
        )
    ]


def AgentCard(
    agent_id: str,
    name: str,
    status: str,
    subtitle: str,
    meta_left: str,
    meta_right: str,
    selected: bool,
    on_select: Callable[[str], Any],
    agent: str = "",
    compact: bool = False,
    subject: str = "",
) -> Any:
    """Full agent card: title + pill, subject line (optional), subtitle, meta row.

    Blocked cards are solid orange with dark text; working/done/idle/
    unknown are dark cards with colored pills. Long names ellipsize;
    the tooltip keeps the full name, prefixed with the agent identity
    when the kind is known. The subtitle's leading agent word renders
    as an identity pill; a 3px identity stripe runs down the card's
    left edge inside the rounded border.
    """
    # Local import: theme.py imports this package for tokens, so a
    # module-level import would cycle (theme -> cards -> theme).
    from relaydeck.client.theme import agent_identity, agent_subtitle_rest

    blocked = status == T.STATUS_BLOCKED
    title_ink = T.BLOCKED_TEXT if blocked else T.TEXT
    line_ink = T.BLOCKED_TEXT if blocked else T.TEXT_SECONDARY
    meta_ink = T.BLOCKED_SMALL_TEXT if blocked else T.TEXT_MUTED
    identity = agent_identity(agent, True)
    rest = agent_subtitle_rest(subtitle, agent)
    meta_value = f" · {rest}" if rest and rest != subtitle else rest
    status_word = T.STATUS_LABELS.get(status, T.STATUS_LABELS[T.STATUS_UNKNOWN])
    tooltip = f"{identity.label} agent, {status_word}, {name}" if agent else name
    edge = T.BLOCKED if blocked else T.BORDER
    return _action_button(
        key=f"agent-card-{agent_id}",
        radius=T.RADIUS_CARD,
        tooltip=None if compact else tooltip,
        on_press=lambda: on_select(agent_id),
        content=ft.Container(
            padding=ft.Padding.symmetric(vertical=T.CARD_PAD_V, horizontal=T.CARD_PAD_H),
            border_radius=ft.BorderRadius.all(T.RADIUS_CARD),
            bgcolor=T.BLOCKED if blocked else T.CARD,
            border=ft.Border(
                left=ft.BorderSide(3, identity.color),
                top=ft.BorderSide(1, edge),
                right=ft.BorderSide(1, edge),
                bottom=ft.BorderSide(1, edge),
            ),
            shadow=_selected_outline(selected),
            content=ft.Column(
                spacing=3,
                controls=[
                    ft.Row(
                        spacing=T.GAP_CARD,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        controls=[
                            ft.Text(
                                name,
                                style=text_style(FAMILY_INTERFACE, T.TYPE_CARD_TITLE, title_ink),
                                no_wrap=True,
                                overflow=ft.TextOverflow.ELLIPSIS,
                                expand=True,
                            ),
                            StatusPill(status, inverted=blocked),
                        ],
                    ),
                    *(
                        [
                            ft.Text(
                                subject,
                                key=f"agent-subject-{agent_id}",
                                style=text_style(FAMILY_INTERFACE, T.TYPE_CARD_LINE, line_ink),
                                no_wrap=True,
                                overflow=ft.TextOverflow.ELLIPSIS,
                            )
                        ]
                        if subject
                        else []
                    ),
                    ft.Row(
                        spacing=6,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        controls=[
                            AgentPill(
                                identity.label,
                                identity.color,
                                key=f"agent-badge-{agent_id}",
                                ink=T.BLOCKED_TEXT if blocked else None,
                            ),
                            *(
                                [
                                    ft.Text(
                                        meta_value,
                                        style=text_style(
                                            FAMILY_INTERFACE,
                                            T.TYPE_CARD_LINE_STRONG
                                            if blocked
                                            else T.TYPE_CARD_LINE,
                                            line_ink,
                                        ),
                                        max_lines=2,
                                        overflow=ft.TextOverflow.ELLIPSIS,
                                        expand=True,
                                    )
                                ]
                                if meta_value
                                else []
                            ),
                        ],
                    ),
                    ft.Row(
                        alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                        controls=[
                            ft.Text(
                                meta_left,
                                style=text_style(FAMILY_MONO, T.TYPE_META, meta_ink),
                                no_wrap=True,
                                overflow=ft.TextOverflow.ELLIPSIS,
                                expand=True,
                            ),
                            ft.Text(
                                meta_right,
                                style=text_style(FAMILY_MONO, T.TYPE_META, meta_ink),
                                no_wrap=True,
                                overflow=ft.TextOverflow.ELLIPSIS,
                            ),
                        ],
                    ),
                ],
            ),
        ),
    )


def _tile_style(status: str) -> tuple[str, str, int]:
    """(fill, ink, border_width) for a collapsed tile.

    Blocked tiles are solid orange; working/done/idle carry a 2px
    status border with status-colored initials; unknown is muted.
    """
    if status == T.STATUS_BLOCKED:
        return T.BLOCKED, T.BLOCKED_TEXT, 0
    if status == T.STATUS_WORKING:
        return T.CARD, T.WORKING, 2
    if status == T.STATUS_DONE:
        return T.CARD, T.DONE, 2
    if status == T.STATUS_IDLE:
        return T.CARD, T.IDLE_FILL, 2
    return T.CARD, T.UNKNOWN_TEXT, 1


def CollapsedAgentTile(
    agent_id: str,
    name: str,
    status: str,
    selected: bool,
    on_select: Callable[[str], Any],
) -> Any:
    """48px collapsed tile: two-letter initials, status colors, tooltip."""
    fill, ink, border_width = _tile_style(status)
    label = T.STATUS_LABELS.get(status, T.STATUS_LABELS[T.STATUS_UNKNOWN])
    return _action_button(
        key=f"agent-tile-{agent_id}",
        width=T.MIN_TOUCH,
        height=T.MIN_TOUCH,
        radius=14,
        tooltip=f"{name}, {label.lower()}",
        on_press=lambda: on_select(agent_id),
        content=ft.Container(
            border_radius=ft.BorderRadius.all(14),
            bgcolor=fill,
            border=ft.Border.all(border_width, ink) if border_width else None,
            shadow=_selected_outline(selected),
            alignment=ft.Alignment.CENTER,
            content=ft.Text(
                T.initials(name),
                style=text_style(FAMILY_MONO, T.TYPE_TILE_INITIALS, ink),
            ),
        ),
    )
