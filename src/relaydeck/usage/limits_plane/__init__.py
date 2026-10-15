"""Limits plane: provider protocol, scheduler, and format registry.

`PROVIDERS` maps a `[[usage.limits]]` format id to its provider singleton.
The registry is wiring, not provider state: providers themselves stay pure.
"""

from relaydeck.usage.limits_plane.contracts import (
    FetchContext,
    LimitsProvider,
    ProviderResult,
    ProviderSpec,
    RefreshDecision,
    TuiBundle,
    TuiRecipe,
    TuiWindowSpec,
    tui_recipe_from,
)
from relaydeck.usage.limits_plane.providers.jsonrpc_rate_limits_stdio import (
    FORMAT_ID as JSONRPC_RATE_LIMITS_STDIO_FORMAT_ID,
)
from relaydeck.usage.limits_plane.providers.jsonrpc_rate_limits_stdio import (
    JsonRpcRateLimitsProvider,
)
from relaydeck.usage.limits_plane.providers.tui_usage_pane import (
    FORMAT_ID as TUI_USAGE_PANE_FORMAT_ID,
)
from relaydeck.usage.limits_plane.providers.tui_usage_pane import (
    TuiUsagePaneProvider,
    recover_stale_tabs,
)
from relaydeck.usage.limits_plane.refresh_state import (
    STATE_FILENAME,
    AttemptRecord,
    RefreshState,
    SavedSession,
    socket_identity,
)
from relaydeck.usage.limits_plane.scheduler import (
    BACKOFF_BASE_MS,
    BACKOFF_CAP_MS,
    COOLDOWN_MS,
    MAX_FETCH_BYTES,
    THROTTLE_FLOOR_MS,
    LimitsScheduler,
    LimitsState,
    run_bounded_command,
)

PROVIDERS: dict[str, LimitsProvider] = {
    JSONRPC_RATE_LIMITS_STDIO_FORMAT_ID: JsonRpcRateLimitsProvider(),
    TUI_USAGE_PANE_FORMAT_ID: TuiUsagePaneProvider(),
}

__all__ = [
    "BACKOFF_BASE_MS",
    "BACKOFF_CAP_MS",
    "COOLDOWN_MS",
    "MAX_FETCH_BYTES",
    "STATE_FILENAME",
    "THROTTLE_FLOOR_MS",
    "PROVIDERS",
    "AttemptRecord",
    "FetchContext",
    "LimitsProvider",
    "LimitsScheduler",
    "LimitsState",
    "ProviderResult",
    "ProviderSpec",
    "RefreshDecision",
    "RefreshState",
    "SavedSession",
    "TuiBundle",
    "TuiRecipe",
    "TuiWindowSpec",
    "TuiUsagePaneProvider",
    "TUI_USAGE_PANE_FORMAT_ID",
    "recover_stale_tabs",
    "run_bounded_command",
    "socket_identity",
    "tui_recipe_from",
]
