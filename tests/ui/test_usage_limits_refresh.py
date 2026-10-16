"""LIMITS-REFRESH-UI on the Usage page: the refresh button of a tool read on demand (hidden for an
older server or when unavailable; press, spinner, success; cooldown; failure keeps it pressable)
and the credit row. Synthetic data; tool names come only from the page."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import flet as ft
import pytest

from relaydeck.client.features.a11y.focus import iter_controls
from relaydeck.client.features.usage import loop
from relaydeck.client.features.usage import screen_model as sm
from relaydeck.client.features.usage.api import RefreshOutcome, UsageQuery
from relaydeck.client.features.usage.panel import UsagePanel
from tests.support.host_harness import render_component, run

MIN = 60_000
EMPTY = {
    "input_uncached": 0,
    "cache_read": 0,
    "cache_write_short": 0,
    "cache_write_long": 0,
    "cache_write_unknown": 0,
    "output": 0,
    "reasoning": 0,
}


def _now() -> int:
    return int(time.time() * 1000)


class Server:
    """A usage fetch with the on-demand limits read. `info` is the kind's `limits_refresh`;
    `observed` the reading time; `outcome` what the POST answers."""

    def __init__(self, info: Any = None, *, credit: bool = False) -> None:
        self.info = info
        self.observed = _now() - 2 * MIN
        self.credit = credit
        self.reading = True
        self.outcome = RefreshOutcome("started", attempt_id="a1")
        self.presses: list[tuple[str, str]] = []

    def document(self) -> dict[str, Any]:
        kind: dict[str, Any] = {
            "kind": "k1",
            "display_name": "Tool One",
            "tokens_reported": True,
            "limits_setup": None,
            "sources": [],
        }
        if self.info is not None:
            kind["limits_refresh"] = self.info
        window: dict[str, Any] = {
            "window_key": "w",
            "used_percent": 42.0,
            "window_minutes": 10080,
            "resets_at_ms": _now() + 2 * 86_400_000,
            "status": "fresh",
        }
        extra: dict[str, Any] = {}
        if self.credit:
            when = datetime.now(UTC) + timedelta(days=20)
            window = {
                **window,
                "used_percent": 0.3,
                "window_minutes": 43200,
                "resets_at_ms": int(when.timestamp() * 1000),
                "used_amount": 0.15,
                "limit_amount": 50,
                "unit": "credit",
            }
            extra["plan_label"] = "TOOL FREE"
        return {
            "schema_version": 1,
            "host_id": "host-a",
            "generated_ms": _now(),
            "timezone": "UTC",
            "price_table_date": None,
            "next_cursor": None,
            "rows": [
                {
                    "day": datetime.now(UTC).date().isoformat(),
                    "workspace_id": None,
                    "kind": "k1",
                    "model": "m",
                    "counters": {**EMPTY, "input_uncached": 1000},
                    "sessions": 1,
                    "cost_micro_usd": 1,
                    "scope": "overall",
                    "price_snapshot": None,
                    "priced_cost_micro_usd": 1,
                    "priced_tokens": 1000,
                    "unpriced_tokens": 0,
                    "hour": None,
                }
            ],
            "limits": [
                {
                    "kind": "k1",
                    "quota_key": "q",
                    "observed_ms": self.observed,
                    "status": "fresh",
                    "windows": [window],
                    **extra,
                }
            ]
            if self.reading
            else [],
            "coverage": [],
            "snapshot_labels": [],
            "kinds": [kind],
            "workspaces": [],
            "capacity": "ok",
        }

    async def __call__(self, query: UsageQuery) -> Mapping[str, object]:
        return self.document()

    async def refresh_limits(self, host_id: str, kind: str) -> RefreshOutcome:
        self.presses.append((host_id, kind))
        return self.outcome


@ft.component
def Harness(fetch: Any) -> Any:
    return UsagePanel(
        fetch=fetch, identity="acct", host_id="host-a", active=True, dark=False, wide=False
    )


def _has(app: Any, key: str) -> bool:
    try:
        app.find(key)
    except KeyError:
        return False
    return True


def _texts(app: Any) -> list[str]:
    return [
        str(c.value)
        for c in iter_controls([app.page.views[0].controls])
        if isinstance(c, ft.Text) and c.value is not None
    ]


async def _until(app: Any, check: Callable[[], bool], seconds: float = 4) -> None:
    async with asyncio.timeout(seconds):
        while True:
            await app.settle()
            if check():
                return
            await asyncio.sleep(0.01)


@pytest.fixture(autouse=True)
def _fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sm, "REFRESH_POLL_SECONDS", (0.05,) * 60)
    monkeypatch.setattr(loop, "INTERVAL_SECONDS", 30.0)


async def _ready(server: Server) -> Any:
    app = await render_component(Harness, fetch=server)
    await _until(app, lambda: _has(app, "limit-k1"))
    return app


def _button(app: Any) -> Any:
    return app.find("limit-refresh-k1")


def test_an_older_server_shows_no_button() -> None:
    async def scenario() -> None:
        app = await _ready(Server(None))
        assert not _has(app, "limit-refresh-k1")
        assert any(text.startswith("Read ") for text in _texts(app))

    run(scenario())


def test_an_unavailable_tool_shows_no_button_and_the_no_space_hint() -> None:
    async def scenario() -> None:
        info = {"available": False, "reason": "no_space", "in_progress": False}
        app = await _ready(Server(info))
        assert not _has(app, "limit-refresh-k1")  # a reading exists: no button, no hint
        quiet = Server(info)
        quiet.reading = False
        app = await _ready(quiet)
        assert any("open it in a Herdr space once" in text for text in _texts(app))
        assert not _has(app, "limit-refresh-k1")

    run(scenario())


def test_an_available_tool_has_an_accessible_44px_button() -> None:
    async def scenario() -> None:
        app = await _ready(Server({"available": True, "reason": None, "in_progress": False}))
        button = _button(app)
        assert isinstance(button, ft.IconButton)
        assert button.tooltip == "Refresh Tool One limits"
        assert button.width >= 44 and button.height >= 44
        assert not button.disabled

    run(scenario())


def test_press_shows_a_spinner_then_checked_just_now() -> None:
    async def scenario() -> None:
        server = Server({"available": True, "reason": None, "in_progress": False})
        app = await _ready(server)
        server.info = {"available": True, "reason": None, "in_progress": True}
        await app.set_props_and_fire("limit-refresh-k1", {}, "click")
        await _until(app, lambda: not isinstance(_button(app), ft.IconButton))
        assert server.presses == [("host-a", "k1")]
        assert "reading…" in _texts(app)
        # The read finishes: a new observed time, no longer in progress.
        # in_progress ending alone is not success
        server.info = {"available": True, "reason": None, "in_progress": False}
        await asyncio.sleep(0.3)
        await app.settle()
        assert "checked just now" not in _texts(app)
        server.observed = _now()
        server.info = {
            "available": True,
            "reason": None,
            "in_progress": False,
            "last_attempt": {"id": "a1", "finished_ms": _now(), "result": "ok"},
        }
        await _until(app, lambda: "checked just now" in _texts(app))
        assert isinstance(_button(app), ft.IconButton) and not _button(app).disabled

    run(scenario())


def test_the_cooldown_disables_the_button_with_the_minutes_left() -> None:
    async def scenario() -> None:
        info = {
            "available": True,
            "reason": None,
            "in_progress": False,
            "cooldown_until_ms": _now() + 170_000,
        }
        app = await _ready(Server(info))
        assert _button(app).disabled
        assert "next refresh in 3 min" in _texts(app)
        assert "3 min" in _texts(app)
        assert "available in 3 min" in (_button(app).label or "")
        assert not isinstance(_button(app), ft.IconButton)
        assert any(
            getattr(c, "icon", None) == ft.Icons.SCHEDULE
            for c in iter_controls([app.page.views[0].controls])
        )

    run(scenario())


def test_a_cooldown_answer_to_the_press_disables_it_too() -> None:
    async def scenario() -> None:
        server = Server({"available": True, "reason": None, "in_progress": False})
        server.outcome = RefreshOutcome("cooldown", retry_after_s=120)
        app = await _ready(server)
        await app.set_props_and_fire("limit-refresh-k1", {}, "click")
        await _until(app, lambda: "next refresh in 2 min" in _texts(app))
        assert _button(app).disabled

    run(scenario())


def test_a_failed_read_names_the_problem_and_the_button_works_again() -> None:
    async def scenario() -> None:
        server = Server({"available": True, "reason": None, "in_progress": False})
        app = await _ready(server)
        server.info = {
            "available": True,
            "reason": None,
            "in_progress": False,
            "last_error": "auth_required",
            "last_attempt": {"id": "a1", "finished_ms": _now(), "result": "auth_required"},
        }
        await app.set_props_and_fire("limit-refresh-k1", {}, "click")
        await _until(app, lambda: "sign in to the tool first" in _texts(app))
        button = _button(app)
        assert isinstance(button, ft.IconButton) and not button.disabled
        # Pressable again at once: a second press goes out.
        await app.set_props_and_fire("limit-refresh-k1", {}, "click")
        await app.settle()
        assert len(server.presses) == 2

    run(scenario())


def test_a_credit_row_reads_x_of_y_credits_with_the_reset_date_and_plan() -> None:
    async def scenario() -> None:
        server = Server({"available": True, "reason": None, "in_progress": False}, credit=True)
        app = await _ready(server)
        texts = _texts(app)
        assert "0.15 of 50 credits" in texts
        reset = datetime.now(UTC) + timedelta(days=20)
        assert f"Resets {reset:%b} {reset.day} · TOOL FREE" in texts
        assert any(text.startswith("Checked ") for text in texts)

    run(scenario())
