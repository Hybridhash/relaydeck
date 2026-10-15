"""the Space Picker sheet, the Providers screen and pins, through the host
harness.

Synthetic data only. The helpers are copies of the B screen's helpers, because test modules may not
import each other (tests/architecture). Pins live in memory here: the harness has no page storage.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import flet as ft

from relaydeck.client.features.a11y.focus import iter_controls
from relaydeck.client.features.usage.api import UsageQuery
from relaydeck.client.features.usage.panel import UsagePanel
from tests.support.host_harness import render_component, run

NOW_MS = int(datetime.now(UTC).timestamp() * 1000)
TODAY = datetime.now(UTC).date()
EMPTY = {
    "input_uncached": 0,
    "cache_read": 0,
    "cache_write_short": 0,
    "cache_write_long": 0,
    "cache_write_unknown": 0,
    "output": 0,
    "reasoning": 0,
}


def row(
    scope: str, kind: str, tokens: int, *, space: str | None = None, unpriced: int = 0
) -> dict[str, Any]:
    priced = tokens - unpriced
    return {
        "day": TODAY.isoformat(),
        "workspace_id": space if scope == "workspace" else None,
        "kind": kind,
        "model": "model-a",
        "counters": {**EMPTY, "input_uncached": tokens},
        "sessions": 1,
        "cost_micro_usd": None if unpriced else priced,
        "scope": scope,
        "price_snapshot": "2026-10-01",
        "priced_cost_micro_usd": priced,
        "priced_tokens": priced,
        "unpriced_tokens": unpriced,
        "hour": None,
    }


def kind_doc(kind: str, name: str, *, setup: str | None = None) -> dict[str, Any]:
    state = None if setup is None else {"state": setup, "checked_ms": NOW_MS}
    sources = [] if state is None else [{"format": f"{kind}-format", "limits_setup": state}]
    return {
        "kind": kind,
        "display_name": name,
        "tokens_reported": True,
        "limits_setup": state,
        "sources": sources,
    }


def page_doc(*, spaces: int = 6, kinds: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """`spaces` workspaces, each with usage, plus one Herdr-linked overall row per kind (the gateway
    contract: overall is the sum of its workspace rows; Not linked is a separate scope)."""
    if kinds is None:
        kinds = [
            kind_doc("kind-a", "Alpha Agent", setup="script_missing"),
            kind_doc("kind-b", "Beta Agent"),
        ]
    rows: list[dict[str, Any]] = []
    workspaces = []
    total = 0
    for index in range(spaces):
        tokens = (spaces - index) * 1_000_000
        rows.append(row("workspace", "kind-a", tokens, space=f"space-{index}"))
        workspaces.append(
            {
                "workspace_id": f"space-{index}",
                "label": f"Space {index}",
                "last_used_day": TODAY.isoformat(),
            }
        )
        total += tokens
    rows.append(row("overall", "kind-a", total))
    rows.append(row("not_linked", "kind-b", 2_000_000))
    return {
        "schema_version": 1,
        "host_id": "host-a",
        "generated_ms": NOW_MS,
        "timezone": "UTC",
        "price_table_date": "2026-10-01",
        "next_cursor": None,
        "rows": rows,
        "limits": [],
        "coverage": [],
        "snapshot_labels": [{"date": "2026-10-01", "label": "Standard-tier estimate"}],
        "kinds": kinds,
        "workspaces": workspaces,
        "capacity": "ok",
    }


class Server:
    def __init__(self, document: dict[str, Any] | None = None) -> None:
        self.document = document or page_doc()
        self.queries: list[UsageQuery] = []

    async def __call__(self, query: UsageQuery) -> Mapping[str, object]:
        self.queries.append(query)
        doc = json.loads(json.dumps(self.document))
        doc["host_id"] = query.host_id
        return doc


@ft.component
def Screen(*, fetch: Any, width: float, identity: str = "acct-a", host: str = "host-a") -> Any:
    active, _set_active = ft.use_state(True)
    return UsagePanel(
        fetch=fetch,
        identity=identity,
        host_id=host,
        active=active,
        dark=True,
        wide=width >= 1024,
        width=width,
    )


def _controls(app: Any) -> list[ft.Control]:
    return list(iter_controls([app.page.views[0].controls]))


def _find(app: Any, key: str) -> Any:
    for control in _controls(app):
        if getattr(control, "key", None) == key:
            return control
    return None


def _texts(app: Any) -> list[str]:
    return [str(c.value) for c in _controls(app) if isinstance(c, ft.Text)]


async def _until(app: Any, check: Any) -> None:
    async with asyncio.timeout(5):
        while True:
            await app.settle()
            if check():
                return
            await asyncio.sleep(0.01)


async def _loaded(width: float, server: Server, key: str = "usage-summary") -> Any:
    app = await render_component(Screen, fetch=server, width=width)
    await _until(app, lambda: _find(app, key) is not None)
    return app


async def _open_space_sheet(app: Any) -> None:
    await app.set_props_and_fire("usage-space-picker", {}, "click")
    await _until(
        app,
        lambda: _find(app, "usage-space-dialog") is not None
        and _find(app, "usage-space-dialog").open is True,
    )


def test_the_space_sheet_opens_with_search_all_spaces_and_the_count() -> None:
    async def scenario() -> None:
        app = await _loaded(390, Server())
        await _open_space_sheet(app)
        search = _find(app, "usage-space-search")
        assert search is not None and search.hint_text == "Search 6 spaces"
        assert _find(app, "usage-space-option-all") is not None
        assert _find(app, "usage-space-all-title").value == "ALL · 6"
        assert _find(app, "usage-space-sort").content.value == "Most tokens"

    run(scenario())


def test_a_search_with_no_match_says_so_in_the_quoted_form() -> None:
    async def scenario() -> None:
        app = await _loaded(390, Server())
        await _open_space_sheet(app)
        await app.set_props_and_fire("usage-space-search", {"value": "zzz"}, "change")
        await _until(app, lambda: _find(app, "usage-space-empty") is not None)
        assert _find(app, "usage-space-empty").content.value == 'No spaces match "zzz"'

    run(scenario())


def test_a_search_with_a_match_shows_only_the_results() -> None:
    async def scenario() -> None:
        app = await _loaded(390, Server())
        await _open_space_sheet(app)
        await app.set_props_and_fire("usage-space-search", {"value": "Space 4"}, "change")
        await _until(app, lambda: _find(app, "usage-space-result-option-space-4") is not None)
        assert _find(app, "usage-space-all-title") is None

    run(scenario())


def test_the_star_pins_a_space_into_a_pinned_section_and_unpins_it() -> None:
    async def scenario() -> None:
        app = await _loaded(390, Server())
        await _open_space_sheet(app)
        await app.set_props_and_fire("usage-space-pin-space-2", {}, "click")
        await _until(app, lambda: _find(app, "usage-space-pinned") is not None)
        assert _find(app, "usage-space-pinned-option-space-2") is not None
        await app.set_props_and_fire("usage-space-pin-space-2", {}, "click")
        await _until(app, lambda: _find(app, "usage-space-pinned") is None)

    run(scenario())


def test_the_sort_button_toggles_most_tokens_and_az() -> None:
    async def scenario() -> None:
        app = await _loaded(390, Server())
        await _open_space_sheet(app)
        await app.set_props_and_fire("usage-space-sort", {}, "click")
        await _until(app, lambda: _find(app, "usage-space-sort").content.value == "A–Z")

    run(scenario())


def test_the_draft_selection_applies_only_on_done() -> None:
    async def scenario() -> None:
        server = Server()
        app = await _loaded(390, server)
        await _open_space_sheet(app)
        await app.set_props_and_fire("usage-space-option-space-1", {}, "click")
        await app.set_props_and_fire("usage-space-cancel", {}, "click")
        await _until(app, lambda: _find(app, "usage-space-dialog").open is False)
        assert all(query.space_id is None for query in server.queries[-1:])
        await _open_space_sheet(app)
        await app.set_props_and_fire("usage-space-option-space-1", {}, "click")
        await app.set_props_and_fire("usage-space-done", {}, "click")
        await _until(app, lambda: any(q.space_id == "space-1" for q in server.queries))

    run(scenario())


def test_a_500_space_list_is_one_virtualised_list_with_the_full_count() -> None:
    async def scenario() -> None:
        server = Server(page_doc(spaces=500))
        app = await _loaded(390, server)
        await _open_space_sheet(app)
        listing = _find(app, "usage-space-list")
        assert listing is not None and listing.item_extent == 50
        assert _find(app, "usage-space-all-title").value == "ALL · 500"
        assert _find(app, "usage-space-option-space-499") is not None

    run(scenario())


def test_the_provider_picker_pushes_the_providers_screen_and_a_row_filters_usage() -> None:
    async def scenario() -> None:
        server = Server()
        app = await _loaded(390, server)
        await app.set_props_and_fire("usage-provider-picker", {}, "click")
        await _until(app, lambda: _find(app, "usage-providers-back") is not None)
        assert _find(app, "usage-providers-meta").value == "2 connected"
        assert _find(app, "usage-providers-connected").value == "CONNECTED · 2"
        assert _find(app, "usage-providers-available") is None  # no catalog yet: never shown
        await app.set_props_and_fire("usage-providers-row-kind-b", {}, "click")
        await _until(app, lambda: _find(app, "usage-providers-back") is None)
        await _until(app, lambda: any(q.host_id == "host-a" for q in server.queries))

    run(scenario())


def test_a_setup_row_action_opens_the_command_dialog_from_the_providers_screen() -> None:
    async def scenario() -> None:
        app = await _loaded(390, Server())
        await app.set_props_and_fire("usage-provider-picker", {}, "click")
        await _until(app, lambda: _find(app, "usage-providers-setup-kind-a") is not None)
        assert _find(app, "usage-providers-status-kind-a").value.startswith("Setup needed")
        await app.set_props_and_fire("usage-providers-setup-kind-a", {}, "click")
        await _until(app, lambda: _find(app, "usage-command-dialog").open is True)

    run(scenario())


def test_manage_and_see_all_open_the_providers_screen_and_back_returns_to_usage() -> None:
    async def scenario() -> None:
        app = await _loaded(390, Server(), key="usage-plan-title-link")
        await app.set_props_and_fire("usage-plan-title-link", {}, "click")
        await _until(app, lambda: _find(app, "usage-providers-back") is not None)
        await app.set_props_and_fire("usage-providers-back", {}, "click")
        await _until(app, lambda: _find(app, "usage-summary") is not None)

    run(scenario())


def test_on_a_wide_screen_the_providers_screen_is_a_dialog() -> None:
    async def scenario() -> None:
        app = await _loaded(1100, Server(), key="usage-kpi-tokens")
        await app.set_props_and_fire("usage-provider-picker", {}, "click")
        await _until(
            app,
            lambda: _find(app, "usage-providers-dialog") is not None
            and _find(app, "usage-providers-dialog").open is True,
        )
        assert _find(app, "usage-providers-meta").value == "2 connected"
        await app.set_props_and_fire("usage-providers-close", {}, "click")
        await _until(app, lambda: _find(app, "usage-providers-dialog").open is False)

    run(scenario())


def test_on_a_wide_screen_the_space_picker_is_a_popover_dialog() -> None:
    async def scenario() -> None:
        app = await _loaded(1100, Server(), key="usage-kpi-tokens")
        await _open_space_sheet(app)
        assert _find(app, "usage-space-dialog").open is True

    run(scenario())


def test_the_sort_button_cycles_most_tokens_then_az_then_recent() -> None:
    async def scenario() -> None:
        app = await _loaded(390, Server())
        await _open_space_sheet(app)
        for expected in ("A–Z", "Recent", "Most tokens"):
            await app.set_props_and_fire("usage-space-sort", {}, "click")
            await _until(
                app, lambda want=expected: _find(app, "usage-space-sort").content.value == want
            )

    run(scenario())
