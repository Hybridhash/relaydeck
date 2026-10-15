"""Offline, explicit format registry for numeric allowance observations."""

from relaydeck.usage.contracts import LimitsReader
from relaydeck.usage.limits.json_statusline_limits import JsonStatuslineLimitsReader

LIMITS_READERS: dict[str, LimitsReader] = {
    JsonStatuslineLimitsReader.format_id: JsonStatuslineLimitsReader(),
}
