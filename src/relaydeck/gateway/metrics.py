"""In-memory server metrics: numbers only, 15 one-minute slots, reset on restart.

Every recorder runs on the event-loop thread, so there are no locks. Only route templates are
stored (`/v1/hosts/{host_id}/agents`), never a raw path, query, host name or token.
"""

from __future__ import annotations

import resource
import sys
import time
from bisect import bisect_left
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI

WINDOW_MINUTES = 15
SLOT_S = 60
UNMATCHED = "<unmatched>"
OTHER = "<other>"
MAX_UNMATCHED_SEGMENTS = 16
SEGMENT_CHARS = 32
_SEGMENT_OK = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._-")
MAX_ROUTES_PER_SLOT = 128
SLOWEST_ROUTES = 6
# Upper bounds in ms. A sample lands in the first bucket that holds it; quantiles report that
# bucket's upper bound, and the last one holds everything slower.
BOUNDS_MS = (0.25, 0.5, 1, 2, 5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000)
_SLOW = len(BOUNDS_MS)
SOCKET_SAMPLE_S = 1.0
_METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"})


def _quantile(buckets: list[int], fraction: float) -> float:
    total = sum(buckets)
    if total == 0:
        return 0.0
    needed = fraction * total
    seen = 0
    for index, count in enumerate(buckets):
        seen += count
        if seen >= needed:
            return float(BOUNDS_MS[min(index, _SLOW - 1)])
    return float(BOUNDS_MS[-1])


def _add(into: list[int], other: list[int]) -> None:
    for index, count in enumerate(other):
        into[index] += count


class _RouteSlot:
    __slots__ = ("buckets", "calls", "client_errors", "server_errors")

    def __init__(self) -> None:
        self.calls = self.client_errors = self.server_errors = 0
        self.buckets = [0] * (_SLOW + 1)


class _Slot:
    __slots__ = (
        "client_errors",
        "loop",
        "loop_max_ms",
        "loop_max_at_ms",
        "minute",
        "requests",
        "routes",
        "server_errors",
        "sockets_max",
        "unmatched",
    )

    def __init__(self, minute: int) -> None:
        self.minute = minute
        self.requests = self.client_errors = self.server_errors = 0
        self.loop = [0] * (_SLOW + 1)
        self.loop_max_ms = 0.0
        self.loop_max_at_ms = 0
        self.sockets_max = 0
        self.routes: dict[tuple[str, str], _RouteSlot] = {}
        self.unmatched: set[str] = set()


def unmatched_segment(path: str) -> str:
    """The first path segment only, clipped and made safe; never deeper, never the query."""
    first = path.partition("?")[0].lstrip("/").partition("/")[0]
    return "/" + "".join(c if c in _SEGMENT_OK else "?" for c in first[:SEGMENT_CHARS])


class Metrics:
    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._slots: list[_Slot | None] = [None] * WINDOW_MINUTES
        self._started_s = clock()
        self._started_mono = time.monotonic()
        self._last_cpu: tuple[float, float] | None = None
        self._last_sample_s = 0.0
        self.sign_ins = {"refreshes": 0, "rejected": 0, "revoked": 0}

    # -- recording ----------------------------------------------------------------

    def _slot(self, now_s: float) -> _Slot:
        minute = int(now_s // SLOT_S)
        index = minute % WINDOW_MINUTES
        slot = self._slots[index]
        if slot is None or slot.minute != minute:
            slot = self._slots[index] = _Slot(minute)
        return slot

    def record_request(self, method: str, template: str, status: int, elapsed_s: float) -> None:
        slot = self._slot(self._clock())
        slot.requests += 1
        # The method is client-chosen: an unknown one must not mint new keys.
        method = method if method in _METHODS else OTHER
        key = (method, template)
        route = slot.routes.get(key)
        if route is None:
            if len(slot.routes) >= MAX_ROUTES_PER_SLOT:
                key = (OTHER, OTHER)
            route = slot.routes.get(key)
            if route is None:
                route = slot.routes[key] = _RouteSlot()
        route.calls += 1
        route.buckets[bisect_left(BOUNDS_MS, elapsed_s * 1000)] += 1
        if status >= 500:
            slot.server_errors += 1
            route.server_errors += 1
        elif status >= 400:
            slot.client_errors += 1
            route.client_errors += 1

    def record_unmatched(self, method: str, path: str, status: int, elapsed_s: float) -> None:
        """A request no route took (assets, scans, guard rejections): keyed by its first path
        segment and status class, with at most 16 distinct segments a minute."""
        slot = self._slot(self._clock())
        segment = unmatched_segment(path)
        if segment not in slot.unmatched:
            if len(slot.unmatched) >= MAX_UNMATCHED_SEGMENTS:
                segment = OTHER
            else:
                slot.unmatched.add(segment)
        self.record_request(method, f"{UNMATCHED} {segment} {status // 100}xx", status, elapsed_s)

    def record_loop_delay(self, seconds: float) -> None:
        now_s = self._clock()
        slot = self._slot(now_s)
        ms = max(seconds, 0.0) * 1000
        slot.loop[bisect_left(BOUNDS_MS, ms)] += 1
        if ms > slot.loop_max_ms:
            slot.loop_max_ms, slot.loop_max_at_ms = ms, int(now_s * 1000)

    def sample_sockets(self, open_sockets: int) -> None:
        """Called from the heartbeat; keeps the busiest second of each minute."""
        now_s = self._clock()
        if now_s - self._last_sample_s < SOCKET_SAMPLE_S:
            return
        self._last_sample_s = now_s
        slot = self._slot(now_s)
        slot.sockets_max = max(slot.sockets_max, open_sockets)

    def count_sign_in(self, kind: str, amount: int = 1) -> None:
        self.sign_ins[kind] += amount

    # -- reading ------------------------------------------------------------------

    def _window(self, now_s: float) -> list[_Slot | None]:
        """The last 15 minutes, oldest first; a minute with no traffic is None."""
        current = int(now_s // SLOT_S)
        out: list[_Slot | None] = []
        for minute in range(current - WINDOW_MINUTES + 1, current + 1):
            slot = self._slots[minute % WINDOW_MINUTES]
            out.append(slot if slot is not None and slot.minute == minute else None)
        return out

    def _cpu_percent(self) -> float:
        cpu, mono = time.process_time(), time.monotonic()
        before = self._last_cpu or (0.0, self._started_mono)
        self._last_cpu = (cpu, mono)
        span = mono - before[1]
        return round(max(cpu - before[0], 0.0) / span * 100, 1) if span > 0 else 0.0

    @staticmethod
    def _rss_bytes() -> int:
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return int(peak if sys.platform == "darwin" else peak * 1024)

    def snapshot(self, live: dict[str, Any]) -> dict[str, Any]:
        """The wire reply. `live` is read by the caller from existing state, not counted here."""
        now_s = self._clock()
        window = self._window(now_s)
        current = int(now_s // SLOT_S)
        starts = [(current - WINDOW_MINUTES + 1 + i) * SLOT_S * 1000 for i in range(WINDOW_MINUTES)]
        requests = [s.requests if s else 0 for s in window]
        server_errors = [s.server_errors if s else 0 for s in window]
        client_errors = [s.client_errors if s else 0 for s in window]
        loop_all = [0] * (_SLOW + 1)
        for slot in window:
            if slot is not None:
                _add(loop_all, slot.loop)
        peak = max(range(WINDOW_MINUTES), key=lambda i: (requests[i], i))
        loop_slot = max(
            (s for s in window if s is not None),
            key=lambda s: s.loop_max_ms,
            default=None,
        )
        routes: dict[tuple[str, str], _RouteSlot] = {}
        for slot in window:
            for key, part in (slot.routes if slot is not None else {}).items():
                merged = routes.setdefault(key, _RouteSlot())
                merged.calls += part.calls
                merged.client_errors += part.client_errors
                merged.server_errors += part.server_errors
                _add(merged.buckets, part.buckets)
        slowest = sorted(routes.items(), key=lambda item: -_quantile(item[1].buckets, 0.95))
        return {
            "generated_ms": int(now_s * 1000),
            "started_ms": int(self._started_s * 1000),
            "slots_ms": starts,
            "requests": {
                "series": requests,
                "total": sum(requests),
                "peak": requests[peak],
                "peak_at_ms": starts[peak] if requests[peak] else None,
            },
            "errors": {
                "server_series": server_errors,
                "client_series": client_errors,
                "server_total": sum(server_errors),
                "client_total": sum(client_errors),
            },
            "loop_delay": {
                "p95_series": [_quantile(s.loop, 0.95) if s else 0.0 for s in window],
                "p95_ms": _quantile(loop_all, 0.95),
                "max_ms": round(loop_slot.loop_max_ms, 1) if loop_slot else 0.0,
                "max_at_ms": loop_slot.loop_max_at_ms
                if loop_slot and loop_slot.loop_max_ms
                else None,
            },
            "terminal_sockets": {
                "series": [s.sockets_max if s else 0 for s in window],
                **{key: live[key] for key in ("open", "controlling", "viewing")},
            },
            "routes": [
                {
                    "method": method,
                    "path": path,
                    "calls": part.calls,
                    "p50_ms": _quantile(part.buckets, 0.5),
                    "p95_ms": _quantile(part.buckets, 0.95),
                    "errors": part.client_errors + part.server_errors,
                }
                for (method, path), part in slowest[:SLOWEST_ROUTES]
            ],
            "connections": live["connections"],
            "sign_ins": dict(self.sign_ins),
            "usage_refresh": live["usage_refresh"],
            "process": {"rss_bytes": self._rss_bytes(), "cpu_percent": self._cpu_percent()},
        }


class MetricsMiddleware:
    """Pure ASGI, HTTP only. Time to the response start, so a long stream is one fast request."""

    def __init__(self, app: Any, metrics: Metrics) -> None:
        self._app = app
        self._metrics = metrics

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> Any:
        if scope["type"] != "http":
            return await self._app(scope, receive, send)
        started = time.perf_counter()
        recorded = False

        async def send_timed(message: dict[str, Any]) -> None:
            nonlocal recorded
            if message["type"] == "http.response.start" and not recorded:
                recorded = True
                self._record(scope, message["status"], time.perf_counter() - started)
            await send(message)

        try:
            return await self._app(scope, receive, send_timed)
        except BaseException:
            if not recorded:
                self._record(scope, 500, time.perf_counter() - started)
            raise

    def _record(self, scope: dict[str, Any], status: int, elapsed_s: float) -> None:
        route = scope.get("route")
        template = getattr(route, "path", None)
        method = scope.get("method", "")
        if isinstance(template, str):
            self._metrics.record_request(method, template, status, elapsed_s)
        else:
            self._metrics.record_unmatched(method, str(scope.get("path", "")), status, elapsed_s)


def install_metrics(app: FastAPI, metrics: Metrics) -> None:
    app.add_middleware(MetricsMiddleware, metrics=metrics)
