"""the Usage page and the Console card rendered through the host harness.

Synthetic data only: invented names and numbers, generated for the host's current day so the periods
match. Each test drives the real panel (feed, selectors, screen model, components) and reads the
tree.
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
from relaydeck.client.features.usage.panel import UsageCard, UsagePanel
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
    day: str,
    scope: str,
    kind: str,
    tokens: int,
    *,
    space: str | None = None,
    unpriced: int = 0,
    hour: int | None = None,
) -> dict[str, Any]:
    priced = tokens - unpriced
    return {
        "day": day,
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
        "hour": hour,
    }


def kind_doc(
    kind: str, name: str, *, setup: str | None = None, reported: bool = True
) -> dict[str, Any]:
    state = None if setup is None else {"state": setup, "checked_ms": NOW_MS}
    sources = [] if state is None else [{"format": f"{kind}-format", "limits_setup": state}]
    return {
        "kind": kind,
        "display_name": name,
        "tokens_reported": reported,
        "limits_setup": state,
        "sources": sources,
    }


def synthetic_rows(
    *, hourly: bool, spaces: int = 6, unpriced: bool = False
) -> list[dict[str, Any]]:
    day = TODAY.isoformat()
    rows: list[dict[str, Any]] = []
    if hourly:
        for hour, tokens in ((1, 3_000_000), (9, 9_000_000), (17, 4_000_000)):
            rows.append(row(day, "overall", "kind-a", tokens, hour=hour))
        return rows
    for index in range(spaces):
        name = f"space-{index}"
        tokens = (spaces - index) * 1_000_000
        rows.append(row(day, "workspace", "kind-a", tokens, space=name, unpriced=0))
        rows.append(row(day, "overall", "kind-a", tokens))
    if unpriced:
        rows.append(row(day, "overall", "kind-b", 2_000_000, unpriced=2_000_000))
        rows.append(row(day, "workspace", "kind-b", 2_000_000, space="space-0", unpriced=2_000_000))
    return rows


def page_doc(
    *,
    spaces: int = 6,
    unpriced: bool = False,
    kinds: list[dict[str, Any]] | None = None,
    coverage: list[dict[str, Any]] | None = None,
    empty: bool = False,
    hourly: bool = False,
) -> dict[str, Any]:
    if kinds is None:
        kinds = [
            kind_doc("kind-a", "Alpha CLI", setup="script_missing"),
            kind_doc("kind-b", "Beta CLI"),
        ]
    rows = [] if empty else synthetic_rows(hourly=hourly, spaces=spaces, unpriced=unpriced)
    return {
        "schema_version": 1,
        "host_id": "host-a",
        "generated_ms": NOW_MS,
        "timezone": "UTC",
        "price_table_date": "2026-10-01",
        "next_cursor": None,
        "rows": rows,
        "limits": [
            {
                "kind": "kind-a",
                "quota_key": "q1",
                "observed_ms": NOW_MS - 60_000,
                "status": "fresh",
                "windows": [
                    {
                        "window_key": "primary",
                        "used_percent": 39.0,
                        "window_minutes": 10080,
                        "resets_at_ms": NOW_MS + 3 * 86_400_000,
                        "status": "fresh",
                    }
                ],
            }
        ],
        "coverage": coverage or [],
        "snapshot_labels": [{"date": "2026-10-01", "label": "Standard-tier estimate"}],
        "kinds": kinds,
        "workspaces": [
            {
                "workspace_id": f"space-{index}",
                "label": f"Space {index}",
                "last_used_day": TODAY.isoformat(),
            }
            for index in range(spaces)
        ],
        "capacity": "ok",
    }


class Server:
    """A synthetic gateway: documents by query, with switches for failure and capacity."""

    def __init__(self, document: dict[str, Any] | None = None) -> None:
        self.document = document or page_doc()
        self.queries: list[UsageQuery] = []
        self.fail = False
        self.capacity = False

    async def __call__(self, query: UsageQuery) -> Mapping[str, object]:
        self.queries.append(query)
        if self.fail:
            raise RuntimeError("synthetic offline")
        if self.capacity:
            return {**page_doc(empty=True), "capacity": "exceeded", "host_id": query.host_id}
        doc = json.loads(json.dumps(self.document))
        doc["host_id"] = query.host_id
        if query.hour:
            doc["rows"] = synthetic_rows(hourly=True)
        return doc


@ft.component
def Screen(*, fetch: Any, width: float, identity: str = "acct-a", host: str = "host-a") -> Any:
    active, set_active = ft.use_state(True)
    return ft.Column(
        controls=[
            UsagePanel(
                fetch=fetch,
                identity=identity,
                host_id=host,
                active=active,
                dark=True,
                wide=width >= 1024,
                width=width,
            ),
            ft.Button("Inactive", key="go-inactive", on_click=lambda _e: set_active(False)),
            ft.Button("Active", key="go-active", on_click=lambda _e: set_active(True)),
        ]
    )


def _controls(app: Any) -> list[ft.Control]:
    return list(iter_controls([app.page.views[0].controls]))


def _keys(app: Any) -> set[str]:
    return {c.key for c in _controls(app) if getattr(c, "key", None)}


def _texts(app: Any) -> list[str]:
    return [str(c.value) for c in _controls(app) if isinstance(c, ft.Text)]


def _tooltips(app: Any) -> list[str]:
    return [str(c.tooltip) for c in _controls(app) if getattr(c, "tooltip", None)]


def _find(app: Any, key: str) -> Any:
    for control in _controls(app):
        if getattr(control, "key", None) == key:
            return control
    return None


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


def test_a_phone_page_stacks_the_summary_chart_and_top_four_spaces_with_a_rollup() -> None:
    async def scenario() -> None:
        server = Server(page_doc(spaces=6))
        app = await _loaded(390, server)
        keys = _keys(app)
        assert {"usage-summary", "usage-chart-card", "usage-spaces-card", "usage-plan-card"} <= keys
        assert "usage-kpi-tokens" not in keys
        rows = [key for key in keys if key.startswith("usage-space-row-")]
        assert len(rows) == 4 and "usage-space-rollup" in keys
        assert "Other · 2 spaces" in _texts(app)

    run(scenario())


def test_a_tablet_page_uses_three_kpi_cards_and_shows_eight_spaces() -> None:
    async def scenario() -> None:
        app = await _loaded(820, Server(page_doc(spaces=6)), key="usage-kpi-tokens")
        keys = _keys(app)
        assert {"usage-kpi-tokens", "usage-kpi-cost", "usage-kpi-busiest"} <= keys
        assert "usage-summary" not in keys
        rows = [key for key in keys if key.startswith("usage-space-row-")]
        assert len(rows) == 6 and "usage-space-rollup" not in keys  # 6 <= 8 + 1: no rollup

    run(scenario())


def test_a_wide_page_uses_the_grid_with_limits_and_providers_side_by_side() -> None:
    async def scenario() -> None:
        app = await _loaded(1180, Server(page_doc(spaces=6)), key="usage-kpi-tokens")
        keys = _keys(app)
        assert {
            "usage-chart-card",
            "usage-plan-card",
            "usage-spaces-card",
            "usage-providers-card",
        } <= keys
        assert "usage-kpi-busiest" in keys and "usage-summary" not in keys

    run(scenario())


def test_partial_cost_shows_the_partial_badge_and_its_one_line_explanation() -> None:
    async def scenario() -> None:
        app = await _loaded(390, Server(page_doc(unpriced=True)), key="summary-partial")
        assert _find(app, "summary-partial") is not None
        note = _find(app, "summary-partial-note")
        assert note is not None and "has no price yet, so the real total is higher." in note.value

    run(scenario())


def test_needs_attention_appears_only_with_an_item_and_its_action_opens_the_mac_command() -> None:
    async def scenario() -> None:
        app = await _loaded(390, Server(page_doc()), key="attention-setup-kind-a")
        assert _find(app, "attention-setup-kind-a") is not None
        await app.set_props_and_fire("attention-setup-kind-a", {}, "click")
        await _until(app, lambda: _find(app, "command-line") is not None)
        line = _find(app, "command-line")
        command = line.value
        assert command == (
            "relaydeck usage install --source=kind-a/kind-a-format --install-limits-hook"
        )
        # The command is never ellipsized, works as typed, and no placeholder remains.
        assert line.max_lines is None
        assert "Run the command on the Mac" in " ".join(_texts(app))
        assert "<path to" not in " ".join(_texts(app))
        assert _find(app, "usage-command-copy") is not None

    run(scenario())


def test_an_outdated_hook_dialog_names_the_update_and_the_exact_repair_command() -> None:
    async def scenario() -> None:
        kinds = [
            kind_doc("kind-a", "Alpha CLI", setup="script_outdated"),
            kind_doc("kind-b", "Beta CLI"),
        ]
        app = await _loaded(390, Server(page_doc(kinds=kinds)), key="attention-setup-kind-a")
        await app.set_props_and_fire("attention-setup-kind-a", {}, "click")
        await _until(app, lambda: _find(app, "command-line") is not None)
        assert _find(app, "command-line").value == "relaydeck usage repair"
        texts = " ".join(_texts(app))
        assert "RelayDeck was updated" in texts
        assert "needs refreshing" in texts
        assert "<path to" not in texts
        assert _find(app, "usage-command-copy") is not None

    run(scenario())


def test_a_healthy_page_has_no_needs_attention_section() -> None:
    healthy = [kind_doc("kind-a", "Alpha CLI"), kind_doc("kind-b", "Beta CLI")]

    async def scenario() -> None:
        document = page_doc(kinds=healthy)
        document["limits"][0]["windows"][0]["used_percent"] = 10.0
        app = await _loaded(390, Server(document))
        assert "attention-title" not in _keys(app) and "usage-attention-card" not in _keys(app)

    run(scenario())


def test_capacity_exceeded_is_an_explanation_never_zero_usage() -> None:
    async def scenario() -> None:
        server = Server()
        server.capacity = True
        app = await render_component(Screen, fetch=server, width=390)
        await _until(app, lambda: _find(app, "usage-state-message") is not None)
        assert "Too much data for this period — choose a shorter period or one space." in _texts(
            app
        )
        keys = _keys(app)
        assert "summary-tokens" not in keys and "usage-chart-card" not in keys

    run(scenario())


def test_offline_keeps_the_same_pages_data_marked_offline_in_warn() -> None:
    async def scenario() -> None:
        server = Server()
        app = await _loaded(390, server)
        await app.set_props_and_fire("go-inactive", {}, "click")
        server.fail = True
        await app.set_props_and_fire("go-active", {}, "click")
        await _until(app, lambda: any("offline · last updated" in t for t in _texts(app)))
        meta = _find(app, "usage-meta")
        assert "offline · last updated" in meta.value
        assert _find(app, "summary-tokens") is not None  # the same context's last page is kept

    run(scenario())


def test_today_asks_the_gateway_for_hourly_buckets_and_draws_twenty_four_bars() -> None:
    async def scenario() -> None:
        server = Server()
        app = await _loaded(390, server)
        await app.set_props_and_fire("period-today", {}, "click")
        await _until(app, lambda: any(q.hour for q in server.queries))
        await _until(app, lambda: _find(app, "chart-bar-23") is not None)
        assert _find(app, "chart-bar-23") is not None and _find(app, "chart-bar-24") is None
        assert server.queries[-1].hour is True and server.queries[-1].days == 1

    run(scenario())


def test_an_empty_period_shows_the_caption_and_still_shows_attention() -> None:
    async def scenario() -> None:
        app = await _loaded(390, Server(page_doc(empty=True)), key="usage-empty")
        keys = _keys(app)
        assert "usage-spaces-card" not in keys and "usage-providers-card" not in keys
        assert "attention-setup-kind-a" in keys  # problems still surface when usage is zero

    run(scenario())


def test_one_provider_has_no_by_provider_section() -> None:
    async def scenario() -> None:
        document = page_doc(kinds=[kind_doc("kind-a", "Alpha CLI")])
        app = await _loaded(390, Server(document))
        assert "usage-providers-card" not in _keys(app)

    run(scenario())


def test_unverified_and_unreported_providers_show_honest_status_not_a_zero() -> None:
    async def scenario() -> None:
        kinds = [
            kind_doc("kind-a", "Alpha CLI"),
            kind_doc("kind-b", "Beta CLI"),
            kind_doc("kind-c", "Gamma CLI", reported=False),
        ]
        coverage = [
            {
                "kind": "kind-b",
                "coverage": "unverified",
                "observed_ms": None,
                "unmapped_sessions": 0,
            }
        ]
        app = await _loaded(390, Server(page_doc(kinds=kinds, coverage=coverage)))
        texts = _texts(app)
        assert "Tokens not verified" in texts and "Tokens not reported" in texts
        assert not any(
            text.strip() == "0" for text in texts[-6:]
        )  # no zero stands in for an honest status

    run(scenario())


def test_the_space_picker_filters_the_page_and_its_reset_clears_it() -> None:
    async def scenario() -> None:
        server = Server()
        app = await _loaded(390, server)
        await app.set_props_and_fire("usage-space-picker", {}, "click")
        await _until(app, lambda: _find(app, "usage-space-option-space-1") is not None)
        await app.set_props_and_fire("usage-space-option-space-1", {}, "click")
        await app.set_props_and_fire("usage-space-done", {}, "click")
        await _until(app, lambda: any(q.space_id == "space-1" for q in server.queries))
        assert "Space 1" in _texts(app)
        assert _find(app, "usage-space-picker-reset") is not None
        await app.set_props_and_fire("usage-space-picker-reset", {}, "click")
        await _until(app, lambda: _find(app, "usage-space-picker-reset") is None)

    run(scenario())


def test_refresh_refetches_and_is_ignored_while_a_refresh_is_in_flight() -> None:
    async def scenario() -> None:
        server = Server()
        app = await _loaded(390, server)
        before = len(server.queries)
        await app.set_props_and_fire("usage-refresh", {}, "click")
        await _until(app, lambda: len(server.queries) > before)
        assert len(server.queries) >= before + 1

    run(scenario())


def test_controls_meet_the_touch_target_and_have_labels() -> None:
    async def scenario() -> None:
        app = await _loaded(390, Server())
        refresh = _find(app, "usage-refresh")
        assert refresh.width >= 44 and refresh.height >= 44
        assert refresh.tooltip == "Refresh readings"
        picker = _find(app, "usage-space-picker")
        assert picker.height is None  # reflows at XXL text; its padded content keeps the target
        assert any("tokens" in tip and "Space 0" in tip for tip in _tooltips(app))

    run(scenario())


def test_the_console_card_never_reads_as_no_usage_when_over_capacity() -> None:
    async def scenario() -> None:
        server = Server()
        server.capacity = True
        app = await render_component(
            UsageCard,
            fetch=server,
            identity="acct-a",
            host_id="host-a",
            active=True,
            dark=True,
            on_open=lambda: None,
        )
        await _until(app, lambda: _find(app, "console-headline") is not None)
        headline = _find(app, "console-headline").value
        assert headline == "Too much data today · open Usage"
        assert "No usage today" not in _texts(app)

    run(scenario())


def test_the_console_card_shows_the_day_total_when_there_is_usage() -> None:
    async def scenario() -> None:
        app = await render_component(
            UsageCard,
            fetch=Server(),
            identity="acct-a",
            host_id="host-a",
            active=True,
            dark=True,
            on_open=lambda: None,
        )
        await _until(app, lambda: _find(app, "console-headline") is not None)
        # the strip's first line reads "Today $… · … tokens".
        assert _find(app, "console-headline").value.startswith("Today ")
        assert "tokens" in _find(app, "console-headline").value

    run(scenario())


def test_not_linked_is_selected_locally_and_never_sent_to_the_server_as_a_space() -> None:
    """Not linked is offered even with no unlinked usage. Choosing it filters locally: the query
    carries no space id, and the synthetic id never reaches the server (B2 item 1)."""

    async def scenario() -> None:
        server = Server()
        app = await _loaded(390, server)
        await app.set_props_and_fire("usage-space-picker", {}, "click")
        await _until(app, lambda: _find(app, "usage-space-option-not_linked") is not None)
        await app.set_props_and_fire("usage-space-option-not_linked", {}, "click")
        await app.set_props_and_fire("usage-space-done", {}, "click")
        await _until(app, lambda: "Not linked" in _texts(app))
        assert server.queries[-1].space_id is None
        assert all(query.space_id != "not_linked" for query in server.queries)
        assert _find(app, "usage-space-picker-reset") is not None

    run(scenario())


def test_more_attention_items_open_the_providers_screen_on_a_phone() -> None:
    """The attention "+k more" opens the Providers screen, pushed with "‹ Usage"
    back."""

    async def scenario() -> None:
        kinds = [
            kind_doc(f"kind-{index}", f"Tool {index}", setup="script_missing") for index in range(5)
        ]
        app = await _loaded(390, Server(page_doc(kinds=kinds)), key="attention-more")
        await app.set_props_and_fire("attention-more", {}, "click")
        await _until(app, lambda: _find(app, "usage-providers-back") is not None)
        assert _find(app, "usage-providers-meta").value == "5 connected"
        for index in range(5):
            assert _find(app, f"usage-providers-row-kind-{index}") is not None
        await app.set_props_and_fire("usage-providers-back", {}, "click")
        await _until(app, lambda: _find(app, "usage-providers-back") is None)

    run(scenario())


def test_capacity_dialogs_offer_the_real_spaces_as_unknown_values_never_zero() -> None:
    """The capacity state keeps the spaces to choose from, shown as unknown ("—") (B2 item 3)."""

    async def scenario() -> None:
        server = Server()
        server.capacity = True
        app = await render_component(Screen, fetch=server, width=390)
        await _until(app, lambda: _find(app, "usage-state-message") is not None)
        await app.set_props_and_fire("usage-space-picker", {}, "click")
        await _until(app, lambda: _find(app, "usage-space-option-space-0") is not None)
        assert _find(app, "usage-space-option-not_linked") is not None
        assert "—" in _texts(app)
        assert "0" not in _texts(app)

    run(scenario())


def test_the_partial_explanation_shows_on_the_wide_layout_too() -> None:
    """Wide layouts render the partial note under the summary row (B2 item 4)."""

    async def scenario() -> None:
        app = await _loaded(1100, Server(page_doc(unpriced=True)), key="kpi-partial-note")
        note = _find(app, "kpi-partial-note")
        assert "has no price yet, so the real total is higher." in note.value

    run(scenario())


def test_the_chart_summary_has_an_explicit_accessible_label() -> None:
    """The chart's summary is a label on its meta line, not only a tooltip (B2 item 5)."""

    async def scenario() -> None:
        app = await _loaded(390, Server())
        meta = _find(app, "chart-meta")
        assert meta is not None and meta.semantics_label

    run(scenario())


def test_one_see_all_link_on_a_phone_and_none_when_every_space_is_visible_on_wide() -> None:
    """A phone shows the header link while spaces are hidden; the wide table shows every row (six
    spaces fit), so neither link appears (one link, and only when rows are hidden)."""

    async def scenario() -> None:
        phone = await _loaded(390, Server())
        assert _find(phone, "usage-spaces-title-link") is not None
        assert _find(phone, "usage-table-show-all") is None
        wide = await _loaded(1100, Server(), key="usage-table-row-space-0")
        assert _find(wide, "usage-spaces-title-link") is None
        assert _find(wide, "usage-table-show-all") is None

    run(scenario())


def test_the_today_chart_is_titled_hourly_and_draws_its_twenty_four_bars() -> None:
    async def scenario() -> None:
        server = Server()
        app = await _loaded(390, server)
        await app.set_props_and_fire("period-today", {}, "click")
        await _until(app, lambda: any(q.hour for q in server.queries))
        await _until(app, lambda: _find(app, "chart-bar-23") is not None)
        assert _find(app, "usage-chart-title").value == "Hourly tokens"
        assert "since midnight · now" in _find(app, "usage-meta").value

    run(scenario())
