"""Server health snapshot: numbers only, 15 one-minute slots, kept in memory by the companion."""

from typing import Any, TypedDict

from relaydeck.contracts.pairing import document

_INT = {"type": "integer", "minimum": 0}
_NUM = {"type": "number", "minimum": 0}
_MAYBE_MS = {"type": ["integer", "null"], "minimum": 0}
_SLOTS = 15


def _series(item: dict[str, Any]) -> dict[str, Any]:
    return {"type": "array", "minItems": _SLOTS, "maxItems": _SLOTS, "items": item}


def _object(properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": list(properties),
        "properties": properties,
    }


SERVER_METRICS_SCHEMAS = {
    "relaydeck/gateway/v1/server-metrics": document(
        "server-metrics",
        {
            "generated_ms": _INT,
            "started_ms": _INT,
            "slots_ms": _series(_INT),
            "requests": _object(
                {"series": _series(_INT), "total": _INT, "peak": _INT, "peak_at_ms": _MAYBE_MS}
            ),
            "errors": _object(
                {
                    "server_series": _series(_INT),
                    "client_series": _series(_INT),
                    "server_total": _INT,
                    "client_total": _INT,
                }
            ),
            "loop_delay": _object(
                {
                    "p95_series": _series(_NUM),
                    "p95_ms": _NUM,
                    "max_ms": _NUM,
                    "max_at_ms": _MAYBE_MS,
                }
            ),
            "terminal_sockets": _object(
                {"series": _series(_INT), "open": _INT, "controlling": _INT, "viewing": _INT}
            ),
            "routes": {
                "type": "array",
                "maxItems": 6,
                "items": _object(
                    {
                        "method": {"type": "string", "maxLength": 16},
                        "path": {"type": "string", "maxLength": 256},
                        "calls": _INT,
                        "p50_ms": _NUM,
                        "p95_ms": _NUM,
                        "errors": _INT,
                    }
                ),
            },
            "connections": _object(
                {
                    "terminal_sockets": _INT,
                    "host_tunnels": _INT,
                    "leases_control": _INT,
                    "leases_view": _INT,
                    "event_streams": _INT,
                    "web_tabs": _INT,
                }
            ),
            "sign_ins": _object({"refreshes": _INT, "rejected": _INT, "revoked": _INT}),
            "usage_refresh": {
                "type": "array",
                "maxItems": 32,
                "items": _object(
                    {
                        "kind": {"type": "string", "maxLength": 64},
                        "finished_ms": _MAYBE_MS,
                        "result": {"type": ["string", "null"], "maxLength": 64},
                    }
                ),
            },
            "process": _object({"rss_bytes": _INT, "cpu_percent": _NUM}),
        },
    )
}


class ServerMetricsRequests(TypedDict):
    series: list[int]
    total: int
    peak: int
    peak_at_ms: int | None


class ServerMetricsErrors(TypedDict):
    server_series: list[int]
    client_series: list[int]
    server_total: int
    client_total: int


class ServerMetricsLoopDelay(TypedDict):
    p95_series: list[float]
    p95_ms: float
    max_ms: float
    max_at_ms: int | None


class ServerMetricsTerminalSockets(TypedDict):
    series: list[int]
    open: int
    controlling: int
    viewing: int


class ServerMetricsRoutesItem(TypedDict):
    method: str
    path: str
    calls: int
    p50_ms: float
    p95_ms: float
    errors: int


class ServerMetricsConnections(TypedDict):
    terminal_sockets: int
    host_tunnels: int
    leases_control: int
    leases_view: int
    event_streams: int
    web_tabs: int


class ServerMetricsSignIns(TypedDict):
    refreshes: int
    rejected: int
    revoked: int


class ServerMetricsUsageRefreshItem(TypedDict):
    kind: str
    finished_ms: int | None
    result: str | None


class ServerMetricsProcess(TypedDict):
    rss_bytes: int
    cpu_percent: float


class ServerMetrics(TypedDict):
    generated_ms: int
    started_ms: int
    slots_ms: list[int]
    requests: ServerMetricsRequests
    errors: ServerMetricsErrors
    loop_delay: ServerMetricsLoopDelay
    terminal_sockets: ServerMetricsTerminalSockets
    routes: list[ServerMetricsRoutesItem]
    connections: ServerMetricsConnections
    sign_ins: ServerMetricsSignIns
    usage_refresh: list[ServerMetricsUsageRefreshItem]
    process: ServerMetricsProcess
