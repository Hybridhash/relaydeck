"""Server health numbers as the panel shows them: thresholds, formatting, sparkline maths and the
poll loop. No Flet, so every rule is testable on its own."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

POLL_INTERVAL_S = 5.0
# Error rate is server errors (5xx) over requests; loop delay is the p95 over 15 minutes.
AMBER_ERROR_PCT = 1.0
RED_ERROR_PCT = 5.0
AMBER_LOOP_MS = 50.0
RED_LOOP_MS = 250.0

TONE_OK = "ok"
TONE_WARN = "warn"
TONE_DANGER = "danger"
TONE_MUTED = "muted"

SPARK_PAD = 2.0
DEFAULT_SPARK_WIDTH = 120.0


@dataclass(frozen=True)
class Status:
    tone: str
    label: str


@dataclass(frozen=True)
class Tile:
    key: str
    label: str
    value: str
    unit: str
    note: str
    series: tuple[float, ...]
    caption: str  # what the sparkline shows, for the screen reader


@dataclass(frozen=True)
class RouteRow:
    method: str
    path: str
    calls: str
    p50: str
    p95: str
    errors: str


@dataclass(frozen=True)
class RefreshRow:
    kind: str
    when: str
    label: str
    tone: str


@dataclass(frozen=True)
class HealthView:
    status: Status
    uptime: str
    generated_ms: int
    tiles: tuple[Tile, ...]
    routes: tuple[RouteRow, ...]
    connections: tuple[tuple[str, str], ...]
    sign_ins: tuple[tuple[str, str], ...]
    refreshes: tuple[RefreshRow, ...]
    footer: str


def error_rate_pct(snapshot: dict[str, Any]) -> float:
    total = snapshot["requests"]["total"]
    return snapshot["errors"]["server_total"] / total * 100 if total else 0.0


def status_for(error_pct: float, loop_p95_ms: float) -> Status:
    if error_pct >= RED_ERROR_PCT or loop_p95_ms >= RED_LOOP_MS:
        return Status(TONE_DANGER, "Unhealthy")
    if error_pct >= AMBER_ERROR_PCT or loop_p95_ms >= AMBER_LOOP_MS:
        return Status(TONE_WARN, "Degraded")
    return Status(TONE_OK, "Healthy")


def requests_per_minute(snapshot: dict[str, Any]) -> int:
    """The busier of the last two minutes: the newest slot is still filling."""
    series = snapshot["requests"]["series"]
    return max(series[-2:]) if series else 0


def clock_hhmm(ms: int | None, zone: Any = None) -> str:
    if ms is None:
        return ""
    return datetime.fromtimestamp(ms / 1000, zone).strftime("%H:%M")


def format_uptime(started_ms: int, now_ms: int) -> str:
    minutes = max(now_ms - started_ms, 0) // 60_000
    days, rest = divmod(minutes, 1440)
    hours, mins = divmod(rest, 60)
    if days:
        return f"{days} d {hours} h"
    if hours:
        return f"{hours} h {mins} min"
    return f"{mins} min"


def format_age(seconds: float) -> str:
    whole = max(int(seconds), 0)
    if whole < 60:
        return f"{whole} s ago"
    if whole < 3600:
        return f"{whole // 60} min ago"
    return f"{whole // 3600} h ago"


def format_megabytes(size: int) -> str:
    return f"{round(size / 1_000_000)} MB"


def format_number(value: float) -> str:
    return f"{value:,.0f}" if value >= 100 or value == int(value) else f"{value:g}"


def sparkline_points(
    series: tuple[float, ...] | list[float], width: float, height: float
) -> list[tuple[float, float]]:
    """Evenly spaced points scaled to the box, the largest value at the top, zero at the bottom.
    An all-zero series is a flat line along the bottom."""
    if not series:
        return []
    inner_w, inner_h = max(width - 2 * SPARK_PAD, 0.0), max(height - 2 * SPARK_PAD, 0.0)
    top = max(series)
    step = inner_w / (len(series) - 1) if len(series) > 1 else 0.0
    return [
        (
            round(SPARK_PAD + index * step, 2),
            round(SPARK_PAD + inner_h * (1 - (value / top if top > 0 else 0.0)), 2),
        )
        for index, value in enumerate(series)
    ]


_PILL_LABELS = {
    "ok": ("OK", TONE_OK),
    "idle": ("Idle", TONE_OK),
    "not_reported": ("Not reported", TONE_MUTED),
    "cleanup_failed": ("Cleanup issue", TONE_WARN),
    "workspace_untrusted": ("Folder not trusted", TONE_WARN),
}


def refresh_pill(result: str | None) -> tuple[str, str]:
    if result is None:
        return "No attempt yet", TONE_MUTED
    return _PILL_LABELS.get(result, ("Needs attention", TONE_WARN))


def _tile(
    key: str, label: str, value: str, unit: str, note: str, series: list[float], caption: str
) -> Tile:
    return Tile(key, label, value, unit, note, tuple(series), caption)


def build_view(snapshot: dict[str, Any], zone: Any = None) -> HealthView:
    requests, errors = snapshot["requests"], snapshot["errors"]
    loop, sockets = snapshot["loop_delay"], snapshot["terminal_sockets"]
    pct = error_rate_pct(snapshot)
    peak = requests["peak"]
    rates = [
        (e / r * 100 if r else 0.0)
        for e, r in zip(errors["server_series"], requests["series"], strict=True)
    ]
    tiles = (
        _tile(
            "requests",
            "REQUESTS",
            format_number(requests_per_minute(snapshot)),
            "/ min",
            f"Peak {format_number(peak)} / min at {clock_hhmm(requests['peak_at_ms'], zone)}"
            if peak
            else "No requests yet",
            requests["series"],
            "Requests per minute, last 15 minutes",
        ),
        _tile(
            "errors",
            "ERROR RATE",
            f"{pct:.1f}" if pct < 10 else f"{pct:.0f}",
            "%",
            f"{errors['server_total']} errors",
            rates,
            "Server error rate, last 15 minutes",
        ),
        _tile(
            "loop",
            "LOOP DELAY",
            format_number(loop["p95_ms"]),
            "ms p95",
            f"Max {format_number(loop['max_ms'])} ms at {clock_hhmm(loop['max_at_ms'], zone)}"
            if loop["max_at_ms"]
            else "No delay recorded",
            loop["p95_series"],
            "Event loop delay p95, last 15 minutes",
        ),
        _tile(
            "sockets",
            "TERMINAL SOCKETS",
            str(sockets["open"]),
            "open",
            f"{sockets['controlling']} controlling · {sockets['viewing']} viewing",
            sockets["series"],
            "Open terminal sockets, last 15 minutes",
        ),
    )
    conn = snapshot["connections"]
    sign = snapshot["sign_ins"]
    process = snapshot["process"]
    refreshes = []
    for item in snapshot["usage_refresh"]:
        label, tone = refresh_pill(item["result"])
        when = clock_hhmm(item["finished_ms"], zone) if item["finished_ms"] else "never"
        refreshes.append(RefreshRow(item["kind"], when, label, tone))
    return HealthView(
        status=status_for(pct, loop["p95_ms"]),
        uptime=format_uptime(snapshot["started_ms"], snapshot["generated_ms"]),
        generated_ms=snapshot["generated_ms"],
        tiles=tiles,
        routes=tuple(
            RouteRow(
                row["method"],
                row["path"],
                format_number(row["calls"]),
                f"{format_number(row['p50_ms'])} ms",
                f"{format_number(row['p95_ms'])} ms",
                str(row["errors"]),
            )
            for row in snapshot["routes"]
        ),
        connections=(
            ("Terminal sockets", str(conn["terminal_sockets"])),
            ("Leases (control / view)", f"{conn['leases_control']} / {conn['leases_view']}"),
            ("Event streams", str(conn["event_streams"])),
            ("Web app tabs", str(conn["web_tabs"])),
        ),
        sign_ins=(
            ("Token refreshes", str(sign["refreshes"])),
            ("Rejected", str(sign["rejected"])),
            ("Revoked", str(sign["revoked"])),
        ),
        refreshes=tuple(refreshes),
        footer=(
            f"Process {format_megabytes(process['rss_bytes'])} · "
            f"{process['cpu_percent']:.0f}% CPU. "
            "Numbers only, kept in memory and reset when the companion restarts."
        ),
    )


async def poll_loop(
    fetch: Callable[[], Awaitable[dict[str, Any]]],
    *,
    on_snapshot: Callable[[dict[str, Any]], None],
    on_error: Callable[[str], None],
    interval: float = POLL_INTERVAL_S,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """One request at a time, every `interval`. A failure reports a fixed code and keeps going;
    a refused scope (`unsupported_capability`, `unauthenticated`, `forbidden`) stops the loop."""
    while True:
        try:
            snapshot = await fetch()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - only a fixed code reaches the screen
            code = str(getattr(exc, "code", "")) or "unavailable"
            on_error(code)
            if code in REFUSED:
                return
        else:
            on_snapshot(snapshot)
        await sleep(interval)


REFUSED = frozenset({"unsupported_capability", "unauthenticated", "forbidden"})
