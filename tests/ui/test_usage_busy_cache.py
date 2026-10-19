"""A busy answer is loading with a backoff, a persistent failure is
unavailable, a period switch is the same loading state, and a switch back shows the cached page
at once ("updating…") while it refreshes. A host change clears the cache."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import flet as ft
import pytest

from relaydeck.client.features.a11y.focus import iter_controls
from relaydeck.client.features.usage import loop
from relaydeck.client.features.usage.api import UsageHttpError, UsageQuery
from relaydeck.client.features.usage.panel import UsageCard, UsagePanel
from tests.support.host_harness import render_component, run

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "usage" / "client"


def _page(host: str = "host-a") -> dict[str, Any]:
    document: dict[str, Any] = json.loads((FIXTURES / "page-overall.json").read_text())
    document["host_id"] = host
    return document


@ft.component
def Harness(fetch: Any) -> Any:
    host, set_host = ft.use_state("host-a")
    return ft.Column(
        controls=[
            UsagePanel(
                fetch=fetch, identity="acct", host_id=host, active=True, dark=False, wide=False
            ),
            ft.Button("Host B", key="go-host-b", on_click=lambda _e: set_host("host-b")),
            ft.Button("Host A", key="go-host-a", on_click=lambda _e: set_host("host-a")),
        ]
    )


@ft.component
def Both(fetch: Any, width: float = 390) -> Any:
    return ft.Column(
        controls=[
            UsageCard(
                fetch=fetch,
                identity="acct",
                host_id="host-a",
                active=True,
                dark=False,
                on_open=lambda: None,
                wide=width >= 1000,
            ),
            UsagePanel(
                fetch=fetch,
                identity="acct",
                host_id="host-a",
                active=True,
                dark=False,
                wide=width >= 1000,
                width=width,
            ),
        ]
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
def _fast_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(loop, "RETRY_CAP_SECONDS", 0.01)
    monkeypatch.setattr(loop, "UNAVAILABLE_AFTER_SECONDS", 0.4)


class Scripted:
    """Answers per call from a list of callables; the last one repeats."""

    def __init__(self, *steps: Callable[[UsageQuery], Any]) -> None:
        self.steps = steps
        self.calls: list[UsageQuery] = []

    async def __call__(self, query: UsageQuery) -> Mapping[str, object]:
        self.calls.append(query)
        step = self.steps[min(len(self.calls) - 1, len(self.steps) - 1)]
        result = step(query)
        if asyncio.iscoroutine(result):
            result = await result
        return result  # type: ignore[no-any-return]


def _busy(_query: UsageQuery) -> Any:
    raise UsageHttpError(0, "usage_busy")


def _ok(query: UsageQuery) -> Any:
    return _page(query.host_id)


def test_a_busy_answer_shows_loading_then_retries_until_it_succeeds() -> None:
    async def scenario() -> None:
        gate = asyncio.Event()

        async def first_busy(_query: UsageQuery) -> Any:
            await gate.wait()  # the first answer is slow, then busy
            raise UsageHttpError(0, "usage_busy")

        server = Scripted(first_busy, _busy, _ok)
        app = await render_component(Harness, fetch=server)
        await app.settle()
        assert _has(app, "usage-skeleton") and not _has(app, "usage-state-message")
        gate.set()
        await _until(app, lambda: _has(app, "summary-tokens"))
        assert len(server.calls) >= 3  # retried inside the one refresh loop
        assert "Usage is unavailable right now." not in _texts(app)

    run(scenario())


def test_a_persistent_busy_answer_ends_in_unavailable() -> None:
    async def scenario() -> None:
        app = await render_component(Harness, fetch=Scripted(_busy))
        await app.settle()
        assert not _has(app, "usage-state-message")  # still loading at first
        await _until(app, lambda: _has(app, "usage-state-message"))
        assert "Usage is unavailable right now." in _texts(app)

    run(scenario())


def test_a_period_switch_is_the_loading_state_not_unavailable() -> None:
    async def scenario() -> None:
        gate = asyncio.Event()

        async def slow(query: UsageQuery) -> Any:
            if query.hour:
                await gate.wait()
            return _page(query.host_id)

        app = await render_component(Harness, fetch=Scripted(slow))
        await _until(app, lambda: _has(app, "summary-tokens"))
        await app.set_props_and_fire("period-today", {}, "click")
        await app.settle()
        assert _has(app, "usage-skeleton") and _has(app, "usage-loading-icon")
        assert not _has(app, "usage-state-message")
        assert "Usage is unavailable right now." not in _texts(app)
        gate.set()

    run(scenario())


def test_switching_back_shows_the_cached_page_at_once_then_refreshes() -> None:
    async def scenario() -> None:
        release = asyncio.Event()
        seen: list[UsageQuery] = []

        async def fetch(query: UsageQuery) -> Any:
            seen.append(query)
            # The second request for 7d (the refresh behind the cache) waits for the release.
            if query.days == 7 and len([q for q in seen if q.days == 7]) >= 2:
                await release.wait()
            return _page(query.host_id)

        app = await render_component(Harness, fetch=fetch)
        await _until(app, lambda: _has(app, "summary-tokens"))
        await app.set_props_and_fire("period-today", {}, "click")
        await _until(app, lambda: any(q.hour for q in seen))
        await app.set_props_and_fire("period-7d", {}, "click")
        await app.settle()
        # The cached 7d page is on screen while its refresh is still blocked.
        assert _has(app, "summary-tokens") and not _has(app, "usage-skeleton")
        assert any("updating…" in text for text in _texts(app))
        release.set()
        await _until(app, lambda: not any("updating…" in text for text in _texts(app)))

    run(scenario())


def test_a_host_change_clears_the_cache() -> None:
    async def scenario() -> None:
        hold: dict[str, asyncio.Event] = {}

        async def fetch(query: UsageQuery) -> Any:
            gate = hold.get(query.host_id)
            if gate is not None:
                await gate.wait()
            return _page(query.host_id)

        app = await render_component(Harness, fetch=fetch)
        await _until(app, lambda: _has(app, "summary-tokens"))
        hold["host-b"] = asyncio.Event()
        await app.set_props_and_fire("go-host-b", {}, "click")
        await app.settle()
        assert _has(app, "usage-skeleton") and not _has(app, "summary-tokens")
        hold["host-a"] = asyncio.Event()  # A refreshes slowly now
        await app.set_props_and_fire("go-host-a", {}, "click")
        await app.settle()
        # Coming back to host A shows no cached page: the cache was cleared by the host change.
        assert _has(app, "usage-skeleton") and not _has(app, "summary-tokens")
        hold["host-a"].set()
        hold["host-b"].set()

    run(scenario())


def test_the_console_strip_and_the_usage_page_fetching_together_both_show_data() -> None:
    async def scenario() -> None:
        lock = asyncio.Lock()

        async def serialized(query: UsageQuery) -> Any:
            async with lock:  # the server answers one report at a time, in turn
                await asyncio.sleep(0.02)
                return _page(query.host_id)

        app = await render_component(Both, fetch=serialized)
        await _until(app, lambda: _has(app, "summary-tokens") and _has(app, "console-headline"))
        assert _has(app, "summary-tokens")
        headline = app.find("console-headline").value
        assert "unavailable" not in headline.lower() and headline != "Loading…"

    run(scenario())


@pytest.mark.parametrize("width", [390, 1280])
def test_completed_asof_time_is_visible_in_usage_and_console(width, monkeypatch) -> None:
    async def scenario():
        doc = _page()
        doc["timezone"] = "UTC"
        doc["as_of_ms"] = 1_791_676_800_000  # 11 Oct 2026 00:00:00 UTC
        monkeypatch.setattr(
            "relaydeck.client.features.usage.panel._now_ms", lambda: doc["as_of_ms"] + 60_000
        )

        async def fetch(query):
            return {**doc, "host_id": query.host_id}

        app = await render_component(Both, fetch=fetch, width=width)
        await _until(app, lambda: _has(app, "console-as-of") and _has(app, "usage-meta"))
        expected = "as of 11 Oct 00:00:00 UTC"
        assert app.find("console-as-of").value == expected
        assert expected in app.find("usage-meta").value

    run(scenario())
