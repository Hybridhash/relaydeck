"""Standalone shell terminal workflow.

Flet-free members are re-exported here; `screen` (Flet components) is
imported explicitly so headless tests never need the UI engine.
"""

from relaydeck.client.features.shell.ops import (
    AttachmentInfo,
    HttpxShellGatewayOps,
    LeaseInfo,
    ScriptedShellGatewayOps,
    ShellGatewayOps,
    ShellOpError,
    owner_display_from_details,
)
from relaydeck.client.features.shell.session import (
    CLOSE_MESSAGE,
    CLOSE_TITLE,
    DISCONNECT_MESSAGE,
    EXIT_MESSAGE,
    FRESH_ATTACH_NOTICE,
    SELF_TAKEOVER_NOTICE,
    SHELL_CONFLICT,
    SHELL_CONNECTING,
    SHELL_DISCONNECTED,
    SHELL_EXITED,
    SHELL_LIVE,
    ShellSession,
    ShellViewState,
)

__all__ = [
    "CLOSE_MESSAGE",
    "CLOSE_TITLE",
    "DISCONNECT_MESSAGE",
    "EXIT_MESSAGE",
    "FRESH_ATTACH_NOTICE",
    "SELF_TAKEOVER_NOTICE",
    "SHELL_CONFLICT",
    "SHELL_CONNECTING",
    "SHELL_DISCONNECTED",
    "SHELL_EXITED",
    "SHELL_LIVE",
    "AttachmentInfo",
    "HttpxShellGatewayOps",
    "LeaseInfo",
    "ScriptedShellGatewayOps",
    "ShellGatewayOps",
    "ShellOpError",
    "ShellSession",
    "ShellViewState",
    "owner_display_from_details",
]
