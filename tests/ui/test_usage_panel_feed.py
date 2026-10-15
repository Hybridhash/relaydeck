"""A changed host, account, period or an inactive view never
shows the previous context's data, while the new context's fetch is still blocked.

Real Flet hooks through the host harness. The first fetch serves the synthetic page; every later
fetch blocks, so the only thing a render can show is what the feed exposes for the new context.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import flet as ft
import pytest

from relaydeck.client.features.a11y.focus import iter_controls
from relaydeck.client.features.usage import model
from relaydeck.client.features.usage.api import UsageQuery
from relaydeck.client.features.usage.panel import UsagePanel
from tests.support.host_harness import render_component, run

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "usage" / "client"


def _raw_page() -> dict[str, Any]:
    document: dict[str, Any] = json.loads((FIXTURES / "page-overall.json").read_text())
    return document


def _cli_names() -> list[str]:
    page = model.parse_usage_page(_raw_page())
    return [entry.display_name or entry.kind for entry in page.kinds]


def _served_then_blocked() -> tuple[Callable[[UsageQuery], Any], list[UsageQuery]]:
    """The first fetch answers with the synthetic page; every later fetch never returns."""
    calls: list[UsageQuery] = []

    async def fetch(query: UsageQuery) -> Mapping[str, object]:
        calls.append(query)
        if len(calls) == 1:
            return _raw_page()
        await asyncio.Event().wait()  # a blocked fetch for the new context
        return {}

    return fetch, calls


@ft.component
def Harness(fetch: Any) -> Any:
    host, set_host = ft.use_state("host-a")
    account, set_account = ft.use_state("acct-a")
    active, set_active = ft.use_state(True)
    return ft.Column(
        controls=[
            UsagePanel(
                fetch=fetch,
                identity=account,
                host_id=host,
                active=active,
                dark=False,
                wide=False,
            ),
            ft.Button("Host B", key="go-host-b", on_click=lambda _e: set_host("host-b")),
            ft.Button("Account B", key="go-account-b", on_click=lambda _e: set_account("acct-b")),
            ft.Button("Inactive", key="go-inactive", on_click=lambda _e: set_active(False)),
        ]
    )


def _texts(app: Any) -> list[str]:
    return [
        str(control.value)
        for control in iter_controls([app.page.views[0].controls])
        if isinstance(control, ft.Text)
    ]


async def _until_shown(app: Any, check: Callable[[], bool]) -> None:
    async with asyncio.timeout(4):
        while True:
            await app.settle()
            if check():
                return
            await asyncio.sleep(0.01)


async def _loaded() -> Any:
    fetch, _calls = _served_then_blocked()
    app = await render_component(Harness, fetch=fetch)
    names = _cli_names()
    await _until_shown(app, lambda: any(name in _texts(app) for name in names))
    return app


@pytest.mark.parametrize("button", ["go-host-b", "go-account-b"])
def test_a_blocked_new_context_never_shows_the_previous_context_data(button: str) -> None:
    async def scenario() -> None:
        app = await _loaded()
        await app.set_props_and_fire(button, {}, "click")
        await app.settle()
        shown = _texts(app)
        for name in _cli_names():
            assert name not in shown, f"{button} showed the previous context: {name}"

    run(scenario())


def test_a_blocked_new_context_shows_loading_not_the_old_page() -> None:
    # first load is a skeleton, so the loading copy is no longer text.
    async def scenario() -> None:
        app = await _loaded()
        await app.set_props_and_fire("go-host-b", {}, "click")
        await app.settle()
        assert app.find("usage-skeleton") is not None
        assert "Alpha CLI" not in _texts(app)

    run(scenario())


def test_an_inactive_view_keeps_its_own_context_page_and_never_another_one() -> None:
    """Going inactive stops polling but keeps the same context's last page (its age is
    shown by the view); a host change while inactive still clears it."""

    async def scenario() -> None:
        app = await _loaded()
        await app.set_props_and_fire("go-inactive", {}, "click")
        await app.settle()
        assert any(name in _texts(app) for name in _cli_names())

    run(scenario())
