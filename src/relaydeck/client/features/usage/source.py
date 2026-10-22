"""The gateway adapter for the usage report.

Maps the transport's usage call onto the feed's fetch, and its errors onto the fixed usage codes.
Nothing here reads a response body beyond the transport's own checks.

The fetch object also carries `refresh_limits`, the on-demand limits read
(`POST .../usage/limits/refresh`), so the view that already holds the fetch can offer a refresh
button without another dependency being wired through the shell.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from relaydeck.client.features.sync.transport import GatewayTransport, TransportError
from relaydeck.client.features.usage.api import (
    RefreshOutcome,
    UsageHttpError,
    UsageQuery,
)
from relaydeck.contracts import paths


class GatewayUsageFetch:
    """`fetch(query) -> document` over a gateway transport, plus `refresh_limits(host, kind)`."""

    def __init__(self, transport: GatewayTransport) -> None:
        self._transport = transport

    async def __call__(self, query: UsageQuery) -> Mapping[str, object]:
        transport = self._transport
        if not callable(getattr(transport, "usage", None)):
            raise UsageHttpError(0, "usage_unavailable")  # a transport without the usage read
        # The bucket is sent only for an hourly query, so a transport without it keeps working
        # daily.
        bucket = {"bucket": "hour"} if query.hour else {}
        try:
            return await transport.usage(
                query.host_id,
                days=query.days,
                start=query.start,
                end=query.end,
                space_id=query.space_id,
                cursor=query.cursor,
                **bucket,
            )
        except TransportError as error:
            raise UsageHttpError(0, error.code) from error

    async def refresh_limits(self, host_id: str, kind: str) -> RefreshOutcome:
        """Ask the companion to read this tool's limits now. The answer is one of the fixed
        outcomes; nothing from a response body is shown."""
        transport: Any = self._transport
        request = getattr(transport, "_request", None)
        base = getattr(transport, "base_url", None)
        if not callable(request) or not isinstance(base, str):
            return RefreshOutcome("error", code="usage_unavailable")
        url = base + paths.usage_limits_refresh(host_id)
        try:
            response = await request("POST", url, json={"kind": kind})
        except TransportError as error:
            return RefreshOutcome("error", code=error.code)
        except Exception:  # noqa: BLE001 - a fixed code only
            return RefreshOutcome("error", code="usage_unavailable")
        try:
            body = response.json()
        except ValueError:
            body = {}
        doc: Mapping[str, Any] = body if isinstance(body, dict) else {}
        status = response.status_code
        if status == 404:
            return RefreshOutcome("not_configured")
        if status == 429:
            wait = doc.get("retry_after_s")
            seconds = wait if isinstance(wait, int | float) and not isinstance(wait, bool) else None
            return RefreshOutcome("cooldown", retry_after_s=int(seconds) if seconds else None)
        if 200 <= status < 300:
            state = doc.get("state")
            attempt = doc.get("attempt_id")
            return RefreshOutcome(
                "in_progress" if state == "in_progress" else "started",
                attempt_id=attempt if isinstance(attempt, str) and attempt else None,
            )
        code = doc.get("error")
        return RefreshOutcome("error", code=code if isinstance(code, str) else "usage_unavailable")


def gateway_usage_fetch(transport: GatewayTransport) -> GatewayUsageFetch:
    return GatewayUsageFetch(transport)
