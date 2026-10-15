"""Summary, KPI row, footnote, state cards, skeleton and the Console card.
Render-only: every value and state comes from `screen_model.py`.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import flet as ft

from relaydeck.client.components.design_system import tokens as T
from relaydeck.client.features.usage import screen_model as sm
from relaydeck.client.features.usage.components.loading import LoadingBars
from relaydeck.client.features.usage.components.primitives import (
    badge,
    card,
    divider,
    num,
    overline,
    ui,
)


def _credits_controls(summary: sm.SummaryModel, tokens: dict[str, str]) -> list[ft.Control]:
    """The separate credits line ("+ 162.2 credits (Tool A)"): credits are never in the token total
    or the cost."""
    if not summary.credits_line:
        return []
    return [
        ui(
            summary.credits_line,
            color=tokens["text_muted"],
            size=13,
            weight=600,
            max_lines=2,
            key="summary-credits",
        )
    ]


def summary_card(summary: sm.SummaryModel, *, tokens: dict[str, str]) -> ft.Container:
    """Tokens used, then the estimated priced cost with a Partial badge and its one-line
    explanation."""
    tokens_block = [
        overline("Tokens used", color=tokens["text_muted"], key="summary-tokens-label"),
        num(summary.tokens, color=tokens["text"], size=44, weight=700, key="summary-tokens"),
        ui(
            summary.context,
            color=tokens["text_muted"],
            size=13,
            weight=500,
            max_lines=2,
            key="summary-context",
        ),
        *_credits_controls(summary, tokens),
    ]
    cost_head: list[ft.Control] = [
        overline("Est. cost (priced)", color=tokens["text_muted"], key="summary-cost-label"),
        ft.Row(
            controls=[
                num(summary.cost, color=tokens["text"], size=28, weight=700, key="summary-cost"),
                badge("Partial", tokens=tokens, tone="warn", key="summary-partial")
                if summary.partial
                else ft.Container(),
            ],
            spacing=10,
            alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
        ),
    ]
    if summary.cost_caption:
        cost_head.append(
            ui(summary.cost_caption, color=tokens["text_muted"], size=13, weight=500, max_lines=2)
        )
    if summary.partial_note:
        cost_head.append(
            ui(
                summary.partial_note,
                color=tokens["text_muted"],
                size=13,
                weight=500,
                max_lines=3,
                key="summary-partial-note",
            )
        )
    return card(
        [*tokens_block, divider(tokens), *cost_head], tokens=tokens, key="usage-summary", padding=20
    )


def kpi_cards(summary: sm.SummaryModel, *, tokens: dict[str, str]) -> list[ft.Container]:
    """The three summary cards: tokens, priced cost (with the Partial note inside it), busiest
    day."""
    cost_controls: list[ft.Control] = [
        overline("Est. cost (priced)", color=tokens["text_muted"]),
        ft.Row(
            controls=[
                num(
                    summary.cost,
                    color=tokens["text"],
                    size=26,
                    weight=700,
                    key="kpi-cost",
                    max_lines=None,
                ),
                badge("Partial", tokens=tokens, tone="warn") if summary.partial else ft.Container(),
            ],
            spacing=8,
        ),
    ]
    if summary.partial and summary.partial_note:
        # The Partial explanation sits inside the cost card, under the amount.
        cost_controls.append(
            ui(
                summary.partial_note,
                color=tokens["text_muted"],
                size=13,
                weight=500,
                max_lines=None,
                key="kpi-partial-note",
            )
        )
    if summary.cost_caption:
        cost_controls.append(
            ui(summary.cost_caption, color=tokens["text_muted"], size=12, weight=500, max_lines=2)
        )
    tokens_card = card(
        [
            overline("Tokens used", color=tokens["text_muted"]),
            num(
                summary.tokens,
                color=tokens["text"],
                size=40,
                weight=700,
                key="kpi-tokens",
                max_lines=None,
            ),
            ui(summary.context, color=tokens["text_muted"], size=12, weight=500, max_lines=2),
            *_credits_controls(summary, tokens),
        ],
        tokens=tokens,
        key="usage-kpi-tokens",
        padding=22,
        expand=True,
    )
    cost = card(cost_controls, tokens=tokens, key="usage-kpi-cost", padding=22, expand=True)
    busiest = card(
        [
            overline("Busiest day", color=tokens["text_muted"]),
            num(
                summary.busiest_value,
                color=tokens["text"],
                size=26,
                weight=700,
                key="kpi-busiest",
                max_lines=None,
            ),
            ui(
                summary.busiest_caption,
                color=tokens["text_muted"],
                size=12,
                weight=500,
                max_lines=2,
            ),
        ],
        tokens=tokens,
        key="usage-kpi-busiest",
        padding=22,
        expand=True,
    )
    return [tokens_card, cost, busiest]


def kpi_row(summary: sm.SummaryModel, *, tokens: dict[str, str], wide: bool = True) -> ft.Row:
    """The summary cards on a tablet or a phone: wrapped fixed-width cards (medium), never
    clipped."""
    cards = kpi_cards(summary, tokens=tokens)
    for item in cards:
        item.expand = None
        item.width = 230
    return ft.Row(
        controls=list(cards),
        spacing=16,
        run_spacing=12,
        wrap=True,
        vertical_alignment=ft.CrossAxisAlignment.START,
    )


def caveat_line(text: str, *, tokens: dict[str, str]) -> ft.Control:
    """The token-totals caveat: one line that wraps and is never truncated."""
    return ft.Container(
        key="usage-caveat",
        padding=ft.Padding.symmetric(horizontal=4),
        content=ft.Text(
            text,
            key="usage-caveat-text",
            size=12,
            color=tokens["text_muted"],
            max_lines=None,
            overflow=ft.TextOverflow.VISIBLE,
        ),
        tooltip=text,
    )


def footnote(text: str, *, tokens: dict[str, str]) -> ft.Container:
    return ft.Container(
        key="usage-footnote",
        padding=ft.Padding.symmetric(horizontal=4, vertical=4),
        content=ui(text, color=tokens["text_muted"], size=12, weight=500, max_lines=3),
    )


def message_card(
    message: str, *, tokens: dict[str, str], key: str, tone: str = "normal"
) -> ft.Container:
    """A state that replaces the data: over capacity, unavailable, turned off, no providers."""
    color = tokens["warn"] if tone == "warn" else tokens["text"]
    return card(
        [ui(message, color=color, size=15, weight=700, max_lines=3, key=f"{key}-text")],
        tokens=tokens,
        key=key,
        padding=20,
    )


def empty_caption(text: str, *, tokens: dict[str, str], key: str) -> ft.Container:
    return card(
        [ui(text, color=tokens["text_muted"], size=14, weight=600, max_lines=2)],
        tokens=tokens,
        key=key,
        padding=16,
    )


def skeleton(*, tokens: dict[str, str], reduce_motion: bool = False) -> ft.Column:
    """The loading state: the animated bar-chart icon with "Loading usage…", then placeholder
    blocks in the layout's shape. The blocks are static (no pulse); the icon is the one animation
    and is static under Reduce Motion."""

    def block(height: float) -> ft.Container:
        return ft.Container(height=height, bgcolor=tokens["surface_raised"], border_radius=14)

    indicator = ft.Row(
        key="usage-loading",
        spacing=10,
        vertical_alignment=ft.CrossAxisAlignment.CENTER,
        controls=[
            LoadingBars(color=tokens["accent"], size=28, reduce_motion=reduce_motion),
            ui(
                "Loading usage…",
                color=tokens["text_muted"],
                size=15,
                weight=700,
                key="usage-loading-text",
            ),
        ],
    )
    return ft.Column(
        key="usage-skeleton",
        spacing=16,
        controls=[indicator, block(120), block(180), block(140), block(160)],
    )


def console_loading(*, tokens: dict[str, str], reduce_motion: bool = False) -> ft.Control:
    """The Console strip during its first fetch: "Loading…", never "unavailable"."""
    strip = ft.Container(
        key="console-usage-card",
        expand=True,
        padding=ft.Padding.symmetric(horizontal=12, vertical=7),
        border_radius=T.RADIUS_KEY,
        bgcolor=tokens["surface"],
        border=ft.Border.all(1, tokens["border"]),
        content=ft.Row(
            spacing=8,
            tight=True,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
            controls=[
                LoadingBars(
                    color=tokens["accent"],
                    size=16,
                    reduce_motion=reduce_motion,
                    key="console-loading-icon",
                ),
                ui(
                    "Loading…",
                    color=tokens["text_muted"],
                    size=13,
                    weight=700,
                    key="console-headline",
                ),
            ],
        ),
    )
    return ft.Row(spacing=0, controls=[strip])


def console_card(
    card_model: sm.ConsoleCardModel,
    *,
    tokens: dict[str, str],
    on_open: Callable[[], Any],
    wide: bool = False,
) -> ft.Control:
    """The Console's compact Today strip: full width, at most two lines.

    Line 1 is the headline ("Today $149.02 · 1.07B tokens") with a Partial badge when some tokens
    are unpriced. Line 2 is the tools' most pressing limits (`console_line2`). The
    snapshot age goes to the tooltip and the accessibility label. Unavailable and capacity say so;
    they never read as no usage."""
    first: list[ft.Control] = [
        ui(
            card_model.headline,
            color=tokens["warn"] if not card_model.available else tokens["text"],
            size=13,
            weight=700,
            max_lines=1,
            key="console-headline",
        )
    ]
    if card_model.available and card_model.partial:
        first.append(
            ft.Container(
                key="console-partial",
                padding=ft.Padding.symmetric(horizontal=6, vertical=1),
                border_radius=6,
                border=ft.Border.all(1, tokens["border"]),
                content=ui("Partial", color=tokens["text_muted"], size=10, weight=600, max_lines=1),
            )
        )
    lines: list[ft.Control] = [ft.Row(spacing=6, tight=True, controls=first)]
    second = sm.console_line2(card_model, wide=wide)
    if second:
        lines.append(
            ui(
                second,
                color=tokens["text_muted"],
                size=12,
                weight=600,
                max_lines=1,
                key="console-line2",
            )
        )
    label = "Open Usage. " + (card_model.headline + ". " if card_model.headline else "")
    if second:
        label += second + ". "
    if card_model.age:
        label += card_model.age.capitalize() + "."
    detail = " · ".join(card_model.picks) if len(card_model.picks) > 1 else ""
    tip = "Open Usage"
    if detail:
        tip += " · " + detail
    if card_model.age:
        tip += " · " + card_model.age
    strip = ft.Container(
        key="console-usage-card",
        expand=True,  # the strip fills the content column at every size
        padding=ft.Padding.symmetric(horizontal=12, vertical=7),
        border_radius=T.RADIUS_KEY,
        bgcolor=tokens["surface"],
        border=ft.Border.all(1, tokens["border"]),
        on_click=lambda _e: on_open(),
        tooltip=tip,
        content=ft.Column(
            spacing=1,
            tight=True,
            horizontal_alignment=ft.CrossAxisAlignment.START,
            controls=lines,
        ),
    )
    # A Row gives the expanded strip the column's whole width; a bare Container would wrap its text.
    return ft.Semantics(label=label, content=ft.Row(spacing=0, controls=[strip]))
