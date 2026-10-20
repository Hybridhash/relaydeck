"""The phone agent screen's floating layer: a paste review, the agent list or the tools popover."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import flet as ft

from relaydeck.client.features.terminal.paste_policy import PasteReview
from relaydeck.client.features.terminal.paste_review import PasteReviewSheet
from relaydeck.client.features.terminal.surfaces.agent_switcher import SwitcherList
from relaydeck.client.features.terminal.surfaces.common import AGENT_PASTE_FRAME_KEY, _framed
from relaydeck.client.features.terminal.surfaces.tools_popover import ToolAction, ToolsPopover
from relaydeck.client.features.terminal.switcher import SwitcherRow

OVERLAY_TOP = 32  # just under the 28pt phone header
PASTE_LAYER_KEY = "paste-review-layer"
WIDE_PASTE_TOP = 48
WIDE_PASTE_WIDTH = 520


def review_sheet(
    tokens: dict[str, str],
    review: PasteReview | None,
    on_confirm: Callable[[], Any],
    on_cancel: Callable[[], Any],
) -> Any:
    if review is None:
        return None
    sheet = PasteReviewSheet(review=review, on_confirm=on_confirm, on_cancel=on_cancel)
    return _framed(tokens, AGENT_PASTE_FRAME_KEY, sheet)


def paste_layer(sheet: Any, *, wide: bool) -> Any:
    # A floating layer, so reviewing a paste never resizes the terminal or hides under the dock.
    if wide:
        return ft.Container(
            key=PASTE_LAYER_KEY, top=WIDE_PASTE_TOP, right=12, width=WIDE_PASTE_WIDTH, content=sheet
        )
    return ft.Container(key=PASTE_LAYER_KEY, top=OVERLAY_TOP, left=4, right=4, content=sheet)


def tool_actions(
    *,
    on_paste: Callable[[], Any] | None,
    on_select: Callable[[], Any] | None,
    on_attach: Callable[[], Any] | None,
    attach_enabled: bool,
    picture_action: Any,
) -> tuple[ToolAction, ...]:
    actions: list[ToolAction] = []
    if on_paste is not None:
        actions.append(
            ToolAction("tools-paste", "Paste to terminal", ft.Icons.CONTENT_PASTE, on_paste)
        )
    if on_select is not None:
        actions.append(
            ToolAction("tools-select-text", "Select text", ft.Icons.HIGHLIGHT_ALT, on_select)
        )
    if on_attach is not None:
        actions.append(
            ToolAction(
                "tools-attach-picture",
                "Attach picture",
                ft.Icons.ATTACH_FILE,
                on_attach,
                enabled=attach_enabled,
                native=picture_action,
            )
        )
    return tuple(actions)


def phone_tools(
    *,
    on_paste: Callable[[], Any] | None,
    on_select: Callable[[], Any] | None,
    on_attach: Callable[[], Any] | None,
    attach_enabled: bool,
    picture_action: Any,
    **keys: Any,
) -> dict[str, Any]:
    actions = tool_actions(
        on_paste=on_paste,
        on_select=on_select,
        on_attach=on_attach,
        attach_enabled=attach_enabled,
        picture_action=picture_action,
    )
    return {**keys, "actions": actions}


def build_screen_overlays(
    *,
    opened: str,
    close: Callable[[], None],
    switcher_rows: tuple[SwitcherRow, ...],
    on_pick: Callable[[str], Any] | None,
    tools: dict[str, Any] | None,
    paste: Any = None,
    wide: bool = False,
) -> list[Any]:
    # The list and the popover stay mounted and only hide: a removed layer kept the
    # screen's callbacks, and with them its file pickers, alive after every opening.
    layers: list[Any] = []
    if switcher_rows and on_pick is not None:

        def pick(key: str) -> None:
            close()
            on_pick(key)

        layers.append(
            SwitcherList(
                rows=switcher_rows,
                on_pick=pick,
                on_close=close,
                top=OVERLAY_TOP,
                visible=opened == "switcher",
            )
        )
    if tools is not None:
        layers.append(
            ToolsPopover(top=OVERLAY_TOP, on_close=close, visible=opened == "tools", **tools)
        )
    if paste is not None:
        layers.append(paste_layer(paste, wide=wide))
    return layers
