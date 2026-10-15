"""Ownership and connection panels.

Plain builders preserve control ancestry across layouts. State and hooks belong
to the parent component; builders receive only props and callbacks.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import flet as ft

from relaydeck.client.components.design_system.terminal_window import (
    STATE_CONNECTING,
    STATE_DISCONNECTED,
    STATE_EXITED,
    STATE_WATCHING,
    NoticePanel,
    PillButton,
    TakeoverPanel,
)
from relaydeck.client.features.composer.components import ComposerBox
from relaydeck.client.features.terminal.models import TerminalViewState
from relaydeck.client.features.terminal.paste_policy import PasteReview
from relaydeck.client.features.terminal.pictures import (
    attach_button,
    picture_chips,
)

if TYPE_CHECKING:
    from relaydeck.client.features.composer.domain import StagedMarker

from relaydeck.client.features.terminal.surfaces.common import (
    AGENT_CONFLICT_FRAME_KEY,
    AGENT_CONFLICT_KEY,
    AGENT_CONFLICT_OWNER_KEY,
    AGENT_RECONNECT_KEY,
    AGENT_TAKEOVER_CANCEL_KEY,
    AGENT_TAKEOVER_CONFIRM_KEY,
    AGENT_TAKEOVER_DISCLOSURE_KEY,
    AGENT_TAKEOVER_FRAME_KEY,
    AGENT_TAKEOVER_KEY,
    TAKEOVER_MESSAGE,
    TAKEOVER_TITLE,
)


def build_state_panels(
    *,
    bottom: list[Any],
    confirm_takeover: bool,
    conflict_owner: str | None,
    notice: str,
    on_cancel_takeover: Callable[[], Any],
    on_confirm_takeover: Callable[[], Any],
    on_reconnect: Callable[[], Any] | None,
    on_takeover: Callable[[], Any],
    phone: bool,
    takeover_disclosure: str | None,
    tokens: dict[str, str],
    window: str,
) -> Any:
    """Take-over / conflict / disconnected / exited / connecting panels; appends to `bottom`."""
    if window == STATE_WATCHING:
        if conflict_owner is not None and not phone:
            bottom.append(
                ft.Container(
                    key=AGENT_CONFLICT_FRAME_KEY,
                    content=ft.Column(
                        key=AGENT_CONFLICT_KEY,
                        controls=[
                            ft.Text(
                                f"Terminal owned by {conflict_owner}",
                                key=AGENT_CONFLICT_OWNER_KEY,
                                color=tokens["muted"],
                            )
                        ],
                    ),
                )
            )
        if takeover_disclosure is not None:
            # Agent attach always takes ownership: the armed disclosure is
            # the confirm step (one button when the confirm setting is off).
            bottom.append(
                ft.Container(
                    key=AGENT_TAKEOVER_FRAME_KEY,
                    content=ft.Column(
                        key=AGENT_TAKEOVER_DISCLOSURE_KEY,
                        controls=[
                            TakeoverPanel(
                                phone=phone,
                                title=TAKEOVER_TITLE,
                                message=(
                                    f"{conflict_owner} owns this terminal. {TAKEOVER_MESSAGE}"
                                    if conflict_owner
                                    else TAKEOVER_MESSAGE
                                ),
                                on_take_over=on_confirm_takeover,
                                on_keep_watching=on_cancel_takeover if confirm_takeover else None,
                                confirm=confirm_takeover,
                                take_over_key=AGENT_TAKEOVER_CONFIRM_KEY,
                                keep_watching_key=AGENT_TAKEOVER_CANCEL_KEY,
                            )
                        ],
                    ),
                )
            )
        else:
            bottom.append(
                TakeoverPanel(
                    phone=phone,
                    title=TAKEOVER_TITLE,
                    message=(
                        f"{conflict_owner} has this terminal. {TAKEOVER_MESSAGE}"
                        if conflict_owner is not None
                        else TAKEOVER_MESSAGE
                    ),
                    on_take_over=on_takeover,
                    on_keep_watching=None,
                    confirm=conflict_owner is not None,
                    take_over_key=AGENT_TAKEOVER_KEY,
                )
            )
    elif window == STATE_DISCONNECTED:
        bottom.append(
            NoticePanel(
                phone=phone,
                key="agent-disconnected",
                title="Disconnected",
                message=notice or "The agent keeps running on the host.",
                actions=[
                    PillButton(
                        "Reconnect",
                        on_reconnect or on_takeover,
                        key=AGENT_RECONNECT_KEY,
                        tone="primary",
                    )
                ],
            )
        )
    elif window == STATE_EXITED:
        bottom.append(
            NoticePanel(
                phone=phone,
                key="agent-exited",
                title="Agent terminal exited",
                message=notice,
                actions=[],
            )
        )
    elif window == STATE_CONNECTING:
        bottom.append(
            ft.Text("Connecting…", key="agent-connecting", color=tokens["muted"], size=13)
        )


def build_watching_composer(
    *,
    agent_status: str,
    attach_enabled: bool,
    blocked_review: PasteReview | None,
    blockers: tuple[str, ...],
    can_send: bool,
    composer_focusable: bool,
    dark: bool,
    draft: str,
    error: str | None,
    input_mode: str,
    markers: tuple[StagedMarker, ...],
    on_attach_picture: Any,
    on_cancel_blocked: Callable[[], Any],
    on_clear_error: Callable[[], Any],
    on_confirm_blocked: Callable[[], Any],
    on_draft_change: Callable[[str], Any],
    on_remove_picture: Any,
    on_send: Callable[[], Any],
    picture_action: Any,
    pictures: tuple[Any, ...],
    submission: str,
    terminal: TerminalViewState,
) -> Any:
    """The composer a watching reader may still use (prompts do not need the PTY lease)."""
    return ft.Container(
        key="agent-input-dock",
        disabled=terminal.selection.open or not composer_focusable,
        content=ComposerBox(
            draft=draft,
            markers=markers,
            can_send=can_send,
            blockers=blockers,
            submission=submission,
            error=error,
            review=blocked_review,
            input_mode=input_mode,
            on_draft_change=on_draft_change,
            on_send=on_send,
            on_confirm_blocked=on_confirm_blocked,
            on_cancel_blocked=on_cancel_blocked,
            on_clear_error=on_clear_error,
            dark=dark,
            blocked=agent_status.lower() == "blocked",
            attachment=attach_button(
                on_attach_picture,
                enabled=attach_enabled,
                key="composer-attach-picture",
                action=picture_action,
            ),
            chips=picture_chips(pictures, on_remove_picture) if pictures else None,
        ),
    )
