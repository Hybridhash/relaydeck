"""The Server health panel's rules, without Flet: thresholds, wording, sparkline maths, polling."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from relaydeck.client.features.settings import server_health_model as m

SNAPSHOT: dict[str, Any] = json.loads(
    (Path(__file__).resolve().parents[1] / "fixtures/gateway/server-metrics.json").read_text()
)


@pytest.mark.parametrize(
    ("error_pct", "loop_ms", "tone"),
    [
        (0.0, 0.0, "ok"),
        (0.99, 49.9, "ok"),
        (1.0, 2.0, "warn"),
        (0.0, 50.0, "warn"),
        (4.99, 249.0, "warn"),
        (5.0, 1.0, "danger"),
        (0.0, 250.0, "danger"),
    ],
)
def test_status_thresholds(error_pct: float, loop_ms: float, tone: str) -> None:
    assert m.status_for(error_pct, loop_ms).tone == tone


def test_error_rate_counts_server_errors_only() -> None:
    assert m.error_rate_pct(SNAPSHOT) == pytest.approx(1 / 475 * 100)
    empty = {"requests": {"total": 0}, "errors": {"server_total": 0}}
    assert m.error_rate_pct(empty) == 0.0


@pytest.mark.parametrize(
    ("minutes", "text"),
    [(0, "0 min"), (45, "45 min"), (192, "3 h 12 min"), (60 * 52, "2 d 4 h")],
)
def test_uptime_wording(minutes: int, text: str) -> None:
    assert m.format_uptime(0, minutes * 60_000) == text


@pytest.mark.parametrize(
    ("seconds", "text"), [(-3, "0 s ago"), (2.9, "2 s ago"), (125, "2 min ago"), (7300, "2 h ago")]
)
def test_age_wording(seconds: float, text: str) -> None:
    assert m.format_age(seconds) == text


def test_numbers_and_sizes() -> None:
    assert m.format_number(5.0) == "5"
    assert m.format_number(0.25) == "0.25"
    assert m.format_number(1234) == "1,234"
    assert m.format_megabytes(182_000_000) == "182 MB"


def test_sparkline_scales_to_the_box_with_zero_at_the_bottom() -> None:
    points = m.sparkline_points([0, 5, 10], 104, 40)
    assert points == [(2.0, 38.0), (52.0, 20.0), (102.0, 2.0)]
    assert m.sparkline_points([0, 0, 0], 104, 40) == [(2.0, 38.0), (52.0, 38.0), (102.0, 38.0)]
    assert m.sparkline_points([7], 100, 40) == [(2.0, 2.0)]
    assert m.sparkline_points([], 100, 40) == []


def test_refresh_pills() -> None:
    assert m.refresh_pill("ok") == ("OK", "ok")
    assert m.refresh_pill(None) == ("No attempt yet", "muted")
    assert m.refresh_pill("needs_attention") == ("Needs attention", "warn")
    assert m.refresh_pill("workspace_untrusted") == ("Folder not trusted", "warn")
    assert m.refresh_pill("something_new")[0] == "Needs attention"


def test_the_view_reads_the_fixture() -> None:
    view = m.build_view(SNAPSHOT, zone=None)
    assert view.status.label == "Healthy"
    assert [tile.key for tile in view.tiles] == ["requests", "errors", "loop", "sockets"]
    requests, errors, loop, sockets = view.tiles
    assert (requests.value, requests.unit) == ("33", "/ min")
    assert requests.note.startswith("Peak 61 / min at ")
    assert (errors.value, errors.unit, errors.note) == ("0.2", "%", "1 errors")
    assert (loop.value, loop.unit) == ("5", "ms p95") and loop.note.startswith("Max 48.2 ms at ")
    assert (sockets.value, sockets.note) == ("2", "1 controlling · 1 viewing")
    assert len(requests.series) == 15
    assert view.routes[0].path == "/v1/hosts/{host_id}/usage"
    assert view.routes[0].p95 == "250 ms"
    assert dict(view.connections)["Leases (control / view)"] == "1 / 1"
    assert [r.label for r in view.refreshes] == ["OK", "No attempt yet"]
    assert "182 MB" in view.footer and "reset when the companion restarts" in view.footer


def test_poll_loop_keeps_one_request_in_flight_and_survives_errors() -> None:
    async def scenario() -> None:
        active = 0
        peak = 0
        calls = 0
        seen: list[Any] = []
        errors: list[str] = []
        done = asyncio.Event()

        class Boom(Exception):
            code = "host_offline"

        async def fetch() -> dict[str, Any]:
            nonlocal active, peak, calls
            active += 1
            peak = max(peak, active)
            calls += 1
            await asyncio.sleep(0.01)
            active -= 1
            if calls == 2:
                raise Boom
            if calls >= 4:
                done.set()
            return {"n": calls}

        task = asyncio.ensure_future(
            m.poll_loop(fetch, on_snapshot=seen.append, on_error=errors.append, interval=0.001)
        )
        await asyncio.wait_for(done.wait(), 5)
        task.cancel()
        assert peak == 1
        assert errors == ["host_offline"] and [s["n"] for s in seen][:3] == [1, 3, 4]

    asyncio.run(scenario())


def test_poll_loop_stops_when_the_scope_is_refused() -> None:
    async def scenario() -> list[str]:
        errors: list[str] = []

        class Refused(Exception):
            code = "unsupported_capability"

        async def fetch() -> dict[str, Any]:
            raise Refused

        await m.poll_loop(fetch, on_snapshot=lambda s: None, on_error=errors.append, interval=0)
        return errors

    assert asyncio.run(scenario()) == ["unsupported_capability"]
