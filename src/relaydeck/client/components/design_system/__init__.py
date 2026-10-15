"""Reusable design-system presentation package.

Stable public surface for the application integration. Components
are controlled and declarative: immutable display props in, callbacks
out, Flet child slots for live surfaces (terminal, composer). This
package owns no HTTP/auth/PTY/durable state and duplicates no
lifecycle logic. Interfaces below are frozen; later
additions are new modules that do not mutate these signatures.
"""

from relaydeck.client.components.design_system import tokens as tokens
from relaydeck.client.components.design_system.cards import AgentCard, CollapsedAgentTile
from relaydeck.client.components.design_system.fonts import (
    ASSETS_DIR,
    DOT_TILE,
    FAMILY_INTERFACE,
    FAMILY_MONO,
    FONTS_DIR,
    WEIGHT_FILES,
    dot_tile_src,
    face,
    page_fonts,
)
from relaydeck.client.components.design_system.navigation import (
    DEST_CONSOLE,
    DEST_HOSTS,
    DEST_SETTINGS,
    DEST_TERMINALS,
    DESTINATIONS,
    ConsoleBottomBar,
    ConsoleRail,
)
from relaydeck.client.components.design_system.primitives import (
    ConsoleHeader,
    CountTile,
    DottedGround,
    HostChip,
    RoundIconButton,
    StatusPill,
    text_style,
)
from relaydeck.client.components.design_system.shell import (
    ALL_HOSTS_ID,
    ALL_HOSTS_LABEL,
    AgentListItem,
    ConsoleShell,
)

__all__ = [
    "ALL_HOSTS_ID",
    "ALL_HOSTS_LABEL",
    "ASSETS_DIR",
    "DEST_CONSOLE",
    "DEST_HOSTS",
    "DEST_SETTINGS",
    "DEST_TERMINALS",
    "DESTINATIONS",
    "DOT_TILE",
    "FAMILY_INTERFACE",
    "FAMILY_MONO",
    "FONTS_DIR",
    "WEIGHT_FILES",
    "AgentCard",
    "AgentListItem",
    "CollapsedAgentTile",
    "ConsoleBottomBar",
    "ConsoleHeader",
    "ConsoleRail",
    "ConsoleShell",
    "CountTile",
    "DottedGround",
    "HostChip",
    "RoundIconButton",
    "StatusPill",
    "dot_tile_src",
    "face",
    "page_fonts",
    "text_style",
    "tokens",
]
