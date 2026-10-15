"""Shared keys, texts and frame helpers of the agent terminal detail."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from relaydeck.client.components.design_system.surfaces import SurfaceFrame
from relaydeck.client.components.design_system.terminal_window import (
    STATE_CONNECTING,
    STATE_DISCONNECTED,
    STATE_EXITED,
    STATE_IN_CONTROL,
    STATE_WATCHING,
)

if TYPE_CHECKING:
    pass


# -- declarative agent detail -------------------------------------------------

AGENT_DETAIL_KEY = "agent-detail"
AGENT_TITLE_KEY = "agent-detail-title"
AGENT_STATUS_KEY = "agent-detail-status"
AGENT_WORKFLOW_KEY = "agent-workflow-row"
AGENT_WORKFLOW_COMPOSER_KEY = "agent-workflow-composer"
AGENT_WORKFLOW_DIRECT_KEY = "agent-workflow-direct"
AGENT_KEYBOARD_KEY = "agent-keyboard-row"
AGENT_KEYBOARD_TEXT_KEY = "agent-keyboard-text"
AGENT_KEYBOARD_KEYS_KEY = "agent-keyboard-keys"
AGENT_CONFLICT_KEY = "agent-conflict"
AGENT_CONFLICT_OWNER_KEY = "agent-conflict-owner"
AGENT_TAKEOVER_KEY = "agent-takeover"
AGENT_TAKEOVER_DISCLOSURE_KEY = "agent-takeover-disclosure"
AGENT_TAKEOVER_CONFIRM_KEY = "agent-takeover-confirm"
AGENT_TAKEOVER_CANCEL_KEY = "agent-takeover-cancel"
AGENT_PASTE_ERROR_KEY = "agent-paste-error"
AGENT_HEADER_KEY = "agent-header"
AGENT_CONFLICT_FRAME_KEY = "agent-conflict-frame"
AGENT_TAKEOVER_FRAME_KEY = "agent-takeover-frame"
AGENT_WORKFLOW_GROUP_KEY = "agent-workflow-group"
AGENT_WORKFLOW_LABEL_KEY = "agent-workflow-label"
AGENT_KEYBOARD_GROUP_KEY = "agent-keyboard-group"
AGENT_KEYBOARD_LABEL_KEY = "agent-keyboard-label"
AGENT_KEYS_SCROLL_KEY = "agent-keys-scroll"
AGENT_PASTE_FRAME_KEY = "agent-paste-frame"


def _framed(tokens: dict[str, str], key: str, content: Any) -> Any:
    # Kept as a delegate: agent_screen.py imports this name and is out of scope.
    return SurfaceFrame(key, content, tokens=tokens)


AGENT_DISCONNECT_KEY = "agent-disconnect"
AGENT_RECONNECT_KEY = "agent-reconnect"
AGENT_MORE_KEY = "agent-more"
AGENT_OPEN_SHELL_ITEM_KEY = "agent-open-shell"
AGENT_PASTE_KEY = "agent-paste"
AGENT_WATCHING_HINT = "Not attached. Take over to stream this agent's terminal here."
AGENT_DIRECT_TEXT = "Direct input is on. What you type goes straight to the agent."
AGENT_DIRECT_DRAFT_TEXT = AGENT_DIRECT_TEXT + " Your composer draft is kept."
TAKEOVER_TITLE = "Take over this terminal?"
TAKEOVER_MESSAGE = (
    "You will be able to type here. The terminal may resize, and another "
    "client attached to it will be disconnected. "
    "While you control this pane it uses this device's size; it returns when you leave."
)


def agent_window_state(phase: str, disclosure: bool, conflict: bool) -> str:
    """Map the agent session onto the DESIGN.md window states."""
    if phase == "live":
        return STATE_IN_CONTROL
    if phase == "exited":
        return STATE_EXITED
    if disclosure or conflict:
        return STATE_WATCHING
    if phase == "disconnected":
        return STATE_DISCONNECTED
    if phase == "connecting":
        # Not attached and not asking: the reader is watching (read only)
        # and can ask to take over from the bottom area.
        return STATE_WATCHING
    return STATE_CONNECTING
