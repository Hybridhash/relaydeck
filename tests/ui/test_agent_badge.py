"""Agent badge rendering: pill text/geometry and stripe colour on cards."""

from dataclasses import replace

import flet as ft
import pytest

from relaydeck.client.components.design_system import tokens as T
from relaydeck.client.components.design_system.cards import AgentCard
from relaydeck.client.components.session_rows import agent_tile
from relaydeck.client.features.inventory.selectors import AgentRow, AgentStatus
from relaydeck.client.theme import agent_badge_ink, agent_identity
from tests.support.host_harness import render_component, run

PILL_KINDS = [
    (k, k.capitalize(), agent_identity(k, True).color) for k in ("agent-a", "agent-b", "agent-c")
]


def _card(kind: str, status: str = "working", name: str = "Space Alpha"):
    return AgentCard(
        f"k-{kind or 'none'}",
        name,
        status,
        f"{kind} · Space Alpha · 48m ago" if kind else "Space Alpha · 48m ago",
        "",
        "",
        False,
        lambda i: None,
        agent=kind,
    )


def _pill(card):
    body = card.content.content  # Container -> Column
    row = body.controls[1]
    assert isinstance(row, ft.Row)
    return row.controls[0]


@pytest.mark.parametrize(
    ("kind", "label", "color"),
    PILL_KINDS,
)
def test_card_pill_text_geometry_and_stripe(kind: str, label: str, color: str) -> None:
    card = _card(kind)
    pill = _pill(card)
    assert pill.key == f"agent-badge-k-{kind}"
    assert pill.height == 18
    assert pill.content.value == label
    assert pill.content.style is not None
    assert pill.content.style.size == 11
    assert pill.content.style.weight == ft.FontWeight.W_600
    assert pill.content.style.color == color
    assert pill.bgcolor == ft.Colors.with_opacity(0.16, color)
    border = card.content.border
    assert border.left.width == 3
    assert border.left.color == color
    assert (border.top.width, border.right.width, border.bottom.width) == (1, 1, 1)


def test_card_pill_row_keeps_muted_remainder() -> None:
    card = _card("tool-a")
    row = card.content.content.controls[1]
    assert row.controls[1].value == " · Space Alpha · 48m ago"


def test_card_tooltip_starts_with_agent_label() -> None:
    assert _card("agent-a", "done").tooltip == "Agent-a agent, Done, Space Alpha"


def test_card_without_kind_keeps_plain_tooltip() -> None:
    card = _card("")
    assert card.tooltip == "Space Alpha"
    assert _pill(card).content.value == "Shell"


def test_blocked_card_uses_readable_pill_ink() -> None:
    card = _card("agent-a", "blocked")
    pill = _pill(card)
    assert pill.content.style.color == T.BLOCKED_TEXT
    assert pill.bgcolor == ft.Colors.with_opacity(0.16, agent_identity("agent-a", True).color)
    assert card.content.border.left.color == agent_identity("agent-a", True).color


def _row(kind: str) -> AgentRow:
    return AgentRow(
        key=f"h:{kind}",
        title="Space Alpha",
        subtitle=f"{kind} · Space Alpha · 48m ago",
        status=AgentStatus.WORKING,
        focused=False,
        agent=kind,
    )


def test_tile_subtitle_prefixes_pill() -> None:
    async def scenario() -> None:
        app = await render_component(
            agent_tile, _row("agent-b"), False, lambda k: None, dark=False, phone=True
        )
        tile = app.find("agent-h:agent-b")
        assert isinstance(tile.subtitle, ft.Row)
        pill, text = tile.subtitle.controls
        assert pill.key == "agent-badge-h:agent-b"
        assert pill.content.value == "Agent-b"
        assert pill.content.style.color == agent_badge_ink("agent-b", False)  # light ink
        assert pill.bgcolor == ft.Colors.with_opacity(0.16, agent_identity("agent-b", False).color)
        assert text.value == " · Space Alpha · 48m ago"
        assert text.tooltip is None  # Compact taps retain labels without a popup.

    run(scenario())


def test_tile_keys_callbacks_status_unread_unchanged() -> None:
    async def scenario() -> None:
        seen: list[str] = []
        row = replace(_row("tool-a"), unread=True)
        app = await render_component(agent_tile, row, True, seen.append, dark=False)
        tile = app.find("agent-h:tool-a")
        assert tile.selected is True
        unread = tile.trailing.controls[1]
        assert unread.value == "Unread" and unread.key == "unread-h:tool-a" and unread.visible
        await app.set_props_and_fire("agent-h:tool-a", {}, "click")
        assert seen == ["h:tool-a"]

    run(scenario())
