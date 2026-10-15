"""The screen model, feed and bar rules that the review must-fixes depend on.

Synthetic data only. Each test names the rule it covers.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest

from relaydeck.client.components.design_system.tokens import usage_palette
from relaydeck.client.features.usage import model
from relaydeck.client.features.usage import screen_model as sm
from relaydeck.client.features.usage import selectors as s
from relaydeck.client.features.usage.components import rows
from relaydeck.client.features.usage.loop import UsageFeed

NOW_MS = int(datetime(2026, 10, 12, 12, tzinfo=UTC).timestamp() * 1000)
TODAY = date(2026, 10, 12)
EMPTY = {
    "input_uncached": 0,
    "cache_read": 0,
    "cache_write_short": 0,
    "cache_write_long": 0,
    "cache_write_unknown": 0,
    "output": 0,
    "reasoning": 0,
}
DAY_MS = 86_400_000


def row(
    scope: str,
    kind: str,
    tokens: int,
    *,
    space: str | None = None,
    unpriced: int = 0,
    day: date = TODAY,
    hour: int | None = None,
) -> dict[str, Any]:
    priced = tokens - unpriced
    return {
        "day": day.isoformat(),
        "workspace_id": space if scope == "workspace" else None,
        "kind": kind,
        "model": "m",
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


def kind(
    name: str,
    *,
    setup: str | None = None,
    reported: bool = True,
    display: str | None = None,
) -> dict[str, Any]:
    state = None if setup is None else {"state": setup, "checked_ms": NOW_MS}
    sources = [] if state is None else [{"format": f"{name}-format", "limits_setup": state}]
    return {
        "kind": name,
        "display_name": display or name.upper(),
        "tokens_reported": reported,
        "limits_setup": state,
        "sources": sources,
    }


def ready_build(page: model.UsagePage, **kwargs: Any) -> sm.ScreenModel:
    return sm.build(page, state=sm.STATE_READY, **kwargs)


def doc(
    rows: list[dict[str, Any]],
    *,
    kinds: list[dict[str, Any]] | None = None,
    workspaces: list[tuple[str, str]] | None = None,
    limits: list[dict[str, Any]] | None = None,
    capacity: str = "ok",
    coverage: list[dict[str, Any]] | None = None,
) -> model.UsagePage:
    return model.parse_usage_page(
        {
            "schema_version": 1,
            "host_id": "h1",
            "generated_ms": NOW_MS,
            "timezone": "UTC",
            "price_table_date": "2026-10-01",
            "next_cursor": None,
            "rows": rows,
            "limits": limits or [],
            "coverage": coverage or [],
            "snapshot_labels": [],
            "kinds": kinds if kinds is not None else [kind("kind-a", display="Alpha CLI")],
            "workspaces": [
                {"workspace_id": key, "label": label, "last_used_day": TODAY.isoformat()}
                for key, label in (workspaces or [])
            ],
            "capacity": capacity,
        }
    )


def reading(
    kind_key: str, *, observed_ms: int, windows_status: str, used: float = 40.0
) -> dict[str, Any]:
    return {
        "kind": kind_key,
        "quota_key": "q1",
        "observed_ms": observed_ms,
        "status": "fresh",
        "windows": [
            {
                "window_key": "primary",
                "used_percent": used,
                "window_minutes": 10080,
                "resets_at_ms": NOW_MS + 3 * DAY_MS,
                "status": windows_status,
            }
        ],
    }


def build(
    page: model.UsagePage | None,
    *,
    state: str = sm.STATE_READY,
    flt: s.Filter | None = None,
    width: str = sm.WIDTH_COMPACT,
    inventory: model.UsagePage | None = None,
) -> sm.ScreenModel:
    return sm.build(
        page,
        state=state,
        flt=flt or s.Filter("7d"),
        now_ms=NOW_MS,
        width=width,
        inventory=inventory,
    )


# item 2: limit rows keep setup and reading apart ----------------------------------------------


@pytest.mark.parametrize("setup", ["script_missing", "conflict"])
@pytest.mark.parametrize(
    ("observed_ms", "window_status", "fresh"),
    [
        (NOW_MS - 120_000, "fresh", True),
        (NOW_MS - 3 * DAY_MS, "stale", False),
    ],
    ids=["fresh-window", "stale-window"],
)
def test_a_setup_problem_with_a_reading_keeps_the_age_and_ages_the_windows(
    setup: str, observed_ms: int, window_status: str, fresh: bool
) -> None:
    """Missing hook and conflict, each with a fresh and with a stale window (B2 item 2)."""
    kinds = [kind("kind-a", setup=setup, display="Alpha CLI")]
    page = doc(
        [row("overall", "kind-a", 1_000_000)],
        kinds=kinds,
        limits=[reading("kind-a", observed_ms=observed_ms, windows_status=window_status)],
    )
    plan = build(page).plan
    [limit] = plan
    label = "Setup needed" if setup == "script_missing" else "Conflict"
    assert limit.subline.startswith(f"{label} · last reading ")
    assert "No reading" not in limit.subline
    assert "No limits hook" not in limit.subline
    [window] = limit.windows
    assert window.percent == "40%"
    if fresh:
        assert window.role == "series_1"
        assert "stale" not in window.resets.lower()
    else:
        assert window.role == "neutral"
        assert window.resets.startswith("Reading stale")


def test_a_missing_hook_with_no_reading_says_no_reading_yet_not_no_hook_claim() -> None:
    kinds = [kind("kind-a", setup="script_missing", display="Alpha CLI")]
    [limit] = build(doc([row("overall", "kind-a", 1_000_000)], kinds=kinds)).plan
    assert limit.subline == "Setup needed · no reading yet"
    assert limit.windows == ()


def test_without_setup_evidence_a_reading_is_never_called_missing_hook() -> None:
    """No `limits_setup` evidence: the screen states the reading, never 'No limits hook
    installed'."""
    page = doc(
        [row("overall", "kind-a", 1_000_000)],
        kinds=[kind("kind-a", display="Alpha CLI")],
        limits=[reading("kind-a", observed_ms=NOW_MS - 120_000, windows_status="fresh")],
    )
    [limit] = build(page).plan
    assert "hook" not in limit.subline.lower()
    assert limit.subline.startswith("Read ")


# item 1: Not linked is always offered, under the local id -------------------------------------


def test_not_linked_is_offered_with_zero_usage_under_the_local_id() -> None:
    page = doc(
        [row("workspace", "kind-a", 1_000_000, space="s1"), row("overall", "kind-a", 1_000_000)],
        workspaces=[("s1", "Space Alpha")],
    )
    options = {key: label for key, label, _value in build(page).space_options}
    assert options[sm.NOT_LINKED_ID] == "Not linked"
    assert "unlinked" not in options and "s1" in options


# -- item 3: capacity keeps the inventory, the filter labels and unknown values --------------------


def test_the_capacity_dialogs_offer_the_real_spaces_and_providers_as_unknown_values() -> None:
    inventory = doc(
        [],
        kinds=[kind("kind-a", display="Alpha CLI"), kind("kind-b", display="Beta CLI")],
        workspaces=[("s1", "Space Alpha"), ("s2", "docs-site")],
        capacity="exceeded",
    )
    screen = build(None, state=sm.STATE_CAPACITY, inventory=inventory)
    options = {key: (label, value) for key, label, value in screen.space_options}
    assert options["s1"] == ("Space Alpha", "—")
    assert options[sm.NOT_LINKED_ID] == ("Not linked", "—")
    assert screen.provider_options == (
        ("kind-a", "Alpha CLI", "—"),
        ("kind-b", "Beta CLI", "—"),
    )
    assert all(value == "—" for _k, _l, value in screen.space_options)


def test_the_capacity_state_keeps_the_filter_labels_the_user_chose() -> None:
    inventory = doc(
        [],
        kinds=[kind("kind-a", display="Alpha CLI")],
        workspaces=[("s1", "Space Alpha")],
        capacity="exceeded",
    )
    screen = build(
        None,
        state=sm.STATE_CAPACITY,
        flt=s.Filter("7d", space="s1", provider="kind-a"),
        inventory=inventory,
    )
    assert screen.space_value == "Space Alpha"
    assert screen.space_active is True
    assert screen.provider_value == "Alpha CLI"
    assert screen.provider_active is True


def test_the_feed_keeps_an_exceeded_page_as_inventory_and_never_as_data() -> None:
    capacity = doc(
        [], kinds=[kind("kind-a")], workspaces=[("s1", "Space Alpha")], capacity="exceeded"
    )
    feed = UsageFeed()
    feed.set_key("k")
    generation = feed.begin()
    feed.accept(generation, capacity, NOW_MS)
    assert feed.state.page is None
    assert feed.state.error == "capacity_exceeded"
    assert feed.state.inventory is capacity


# -- item 6: the command copies only the command, quoted and with --source= -----------------------


def test_the_command_works_as_typed_and_uses_the_source_flag_form() -> None:
    page = doc(
        [row("overall", "kind-a", 1_000_000)], kinds=[kind("kind-a", setup="script_missing")]
    )
    [command] = build(page).commands
    assert command.lines == (
        "relaydeck usage install --source=kind-a/kind-a-format --install-limits-hook",
    )


# -- deviations: bounded rows, attention overflow, share bar, single provider, chart ---------------


def test_status_only_providers_are_bounded_with_their_names_as_accessible_overflow() -> None:
    kinds = [kind(f"kind-{index}", reported=False, display=f"Tool {index}") for index in range(6)]
    screen = build(doc([row("overall", "kind-0", 1_000_000)], kinds=kinds), width=sm.WIDTH_COMPACT)
    providers = screen.providers
    assert providers is not None
    assert len(providers.rows) <= sm.top_n(sm.WIDTH_COMPACT)
    shown = {r.key for r in providers.rows}
    assert providers.overflow, "the rows beyond the bound are named, not dropped"
    assert all(name not in shown for name in providers.overflow)


def test_the_attention_overflow_keeps_every_item_for_the_temporary_list() -> None:
    kinds = [
        kind(f"kind-{index}", setup="script_missing", display=f"Tool {index}") for index in range(5)
    ]
    screen = build(doc([row("overall", "kind-0", 1_000_000)], kinds=kinds))
    assert len(screen.attention_all.items) == 5
    assert len(screen.attention.items) < len(screen.attention_all.items)
    assert screen.attention.more == len(screen.attention_all.items) - len(screen.attention.items)


def test_a_single_provider_picker_shows_its_name() -> None:
    screen = build(
        doc([row("overall", "kind-a", 1_000_000)], kinds=[kind("kind-a", display="Alpha CLI")])
    )
    assert screen.provider_value == "Alpha CLI"


@pytest.mark.parametrize("fraction", [0.001, 0.02])
def test_a_positive_share_always_has_a_visible_fill_on_the_full_width_track(
    fraction: float,
) -> None:
    """The track spans the row; a positive share fills at least 1.2% of it (about 3 px on a
    phone)."""
    tokens = usage_palette(True)
    track = rows._share_track(fraction, None, tokens)
    fill, rest = track.controls[0], track.controls[1]
    assert fill.expand >= rows.MIN_FILL_UNITS
    assert fill.expand + rest.expand == rows.TRACK_UNITS


def test_an_empty_share_has_no_fill_and_a_full_share_fills_the_track() -> None:
    tokens = usage_palette(True)
    empty = rows._share_track(0.0, None, tokens)
    assert len(empty.controls) == 1 and empty.controls[0].expand == rows.TRACK_UNITS
    full = rows._share_track(1.0, None, tokens)
    assert len(full.controls) == 1 and full.controls[0].expand == rows.TRACK_UNITS


def test_a_thirty_day_chart_draws_thirty_bars_and_a_today_chart_draws_twenty_four() -> None:
    days = [
        row("overall", "kind-a", 1_000_000, day=TODAY - timedelta(days=back)) for back in range(30)
    ]
    thirty = build(doc(days), flt=s.Filter("30d"))
    assert thirty.chart is not None and len(thirty.chart.bars) == 30
    hours = [row("overall", "kind-a", 1_000_000, hour=hour) for hour in (1, 9, 17)]
    today = build(doc(hours), flt=s.Filter("today"))
    assert today.chart is not None and len(today.chart.bars) == 24


def test_a_thirty_day_chart_labels_every_fifth_day_and_keeps_every_tooltip_date() -> None:
    days = [
        row("overall", "kind-a", 1_000_000, day=TODAY - timedelta(days=back)) for back in range(30)
    ]
    chart = build(doc(days), flt=s.Filter("30d")).chart
    assert chart is not None
    labelled = [index for index, bar in enumerate(chart.bars) if bar.label]
    # Every fifth day counting back from the latest day, plus the peak (index 0 ties for it).
    assert labelled == [0, 4, 9, 14, 19, 24, 29]
    assert all(bar.tooltip for bar in chart.bars)


def test_future_hours_after_the_host_hour_never_report_usage() -> None:
    """The selector never reports an hour after the host's current hour."""
    rows = [row("overall", "kind-a", 2_000_000, hour=hour) for hour in range(24)]
    now = datetime(2026, 10, 12, 5, 30, tzinfo=UTC)
    result = sm.build(
        doc(rows),
        state=sm.STATE_READY,
        flt=s.Filter("today"),
        now_ms=int(now.timestamp() * 1000),
        width=sm.WIDTH_WIDE,
    )
    assert result.chart is not None
    bars = result.chart.bars
    assert [bar.future for bar in bars] == [hour > 5 for hour in range(24)]
    assert all(bar.value == "" and not bar.segments for bar in bars[6:])
    assert all(bar.now == (index == 5) for index, bar in enumerate(bars))
    assert "not yet" in bars[6].tooltip and bars[5].tooltip.startswith("05:00–06:00 · ")


def test_at_zero_hours_one_bucket_exists_and_the_chart_still_renders() -> None:
    rows = [row("overall", "kind-a", 1_000_000, hour=0)]
    now = datetime(2026, 10, 12, 0, 20, tzinfo=UTC)
    result = sm.build(
        doc(rows),
        state=sm.STATE_READY,
        flt=s.Filter("today"),
        now_ms=int(now.timestamp() * 1000),
        width=sm.WIDTH_COMPACT,
    )
    assert result.chart is not None and len(result.chart.bars) == 24
    assert [bar.future for bar in result.chart.bars].count(False) == 1
    assert result.chart.bars[0].peak and result.chart.bars[0].value == "1.0M"
    assert result.chart.meta.startswith("Peak 1.0M · 00:00–01:00")


def test_the_today_title_and_meta_name_the_hourly_window_and_host_time() -> None:
    now = datetime(2026, 10, 12, 13, 5, tzinfo=UTC)
    result = sm.build(
        doc([row("overall", "kind-a", 2_000_000, hour=9)]),
        state=sm.STATE_READY,
        flt=s.Filter("today"),
        now_ms=int(now.timestamp() * 1000),
        width=sm.WIDTH_COMPACT,
    )
    assert result.range_text == "Today · Oct 12 · since midnight · now 13:05 UTC"
    assert result.chart is not None
    assert result.chart.summary.startswith("Hourly tokens.")
    assert result.chart.bars[9].tooltip.startswith("09:00–10:00 · 2.0M tokens")


def test_one_provider_is_one_status_in_the_picker_the_table_and_the_legend() -> None:
    """An unverified provider reads "Unavailable" in By provider, "—" in the picker, and its
    legend name carries the same status."""
    kinds = [kind("kind-a", display="Alpha CLI"), kind("kind-d", display="Relay Tool")]
    coverage = [
        {"kind": "kind-d", "coverage": "unavailable", "observed_ms": None, "unmapped_sessions": 0}
    ]
    rows_ = [row("overall", "kind-a", 2_000_000), row("overall", "kind-d", 3_000_000)]
    result = ready_build(
        doc(rows_, kinds=kinds, coverage=coverage),
        flt=s.Filter("7d"),
        now_ms=NOW_MS,
        width=sm.WIDTH_WIDE,
    )
    assert result.providers is not None
    honest = {r.key: r.honest for r in result.providers.rows}
    assert honest["kind-d"] == "Unavailable" and honest["kind-a"] is None
    values = {kind_id: value for kind_id, _label, value in result.provider_options}
    assert values == {"kind-a": "2.0M", "kind-d": "—"}
    assert result.chart is not None
    assert any(name == "Relay Tool · Unavailable" for _role, name in result.chart.legend)


def test_a_provider_with_verified_and_unverified_tokens_shows_its_tokens_everywhere() -> None:
    """`unverified` coverage with verified tokens above 0 is not status-only. Its
    tokens and cost show like any provider's, with a small "some sessions not verified" note, and
    the status is the same in the picker, the legend, the table and the caveat line."""
    kinds = [kind("kind-a", display="Alpha CLI"), kind("kind-d", display="Relay Tool")]
    coverage = [
        {"kind": "kind-d", "coverage": "unverified", "observed_ms": None, "unmapped_sessions": 0}
    ]
    rows_ = [row("overall", "kind-a", 2_000_000), row("overall", "kind-d", 3_000_000)]
    result = ready_build(
        doc(rows_, kinds=kinds, coverage=coverage),
        flt=s.Filter("7d"),
        now_ms=NOW_MS,
        width=sm.WIDTH_WIDE,
    )
    assert result.providers is not None
    by_key = {r.key: r for r in result.providers.rows}
    assert by_key["kind-d"].honest is None and by_key["kind-d"].value == "3.0M"
    assert by_key["kind-d"].note == "some sessions not verified"
    values = {kind_id: value for kind_id, _label, value in result.provider_options}
    assert values == {"kind-a": "2.0M", "kind-d": "3.0M"}
    assert result.chart is not None
    assert all("not verified" not in name for _role, name in result.chart.legend)
    assert "Relay Tool" in result.caveat and "Partly verified" in result.caveat
    assert "do not include" not in result.caveat  # no longer listed as left out


def test_a_provider_with_no_verified_tokens_stays_status_only() -> None:
    kinds = [kind("kind-a", display="Alpha CLI"), kind("kind-d", display="Relay Tool")]
    coverage = [
        {"kind": "kind-d", "coverage": "unverified", "observed_ms": None, "unmapped_sessions": 0}
    ]
    result = ready_build(
        doc([row("overall", "kind-a", 2_000_000)], kinds=kinds, coverage=coverage),
        flt=s.Filter("7d"),
        now_ms=NOW_MS,
        width=sm.WIDTH_WIDE,
    )
    assert result.providers is not None
    honest = {r.key: r.honest for r in result.providers.rows}
    assert honest["kind-d"] == "Tokens not verified"
    assert "Relay Tool (token counts not verified yet)" in result.caveat


def test_the_spaces_link_is_hidden_when_every_row_is_visible_and_counts_not_linked() -> None:
    """Six spaces plus Not linked are seven rows: on a wide page all seven show, so no link; a
    phone shows four and counts all seven."""
    workspaces = [(f"s{index}", f"Space {index}") for index in range(6)]
    rows_ = [row("workspace", "kind-a", 1_000_000, space=f"s{index}") for index in range(6)]
    rows_ += [row("overall", "kind-a", 6_000_000), row("workspace", "kind-a", 0, space="s0")]
    rows_.append(row("overall", "kind-b", 1_000_000, unpriced=0))
    rows_.append(row("workspace", "kind-b", 1_000_000, space=None))
    rows_ = [
        dict(r, scope="not_linked", workspace_id=None)
        if r["kind"] == "kind-b" and r["scope"] == "workspace"
        else r
        for r in rows_
    ]
    kinds = [kind("kind-a", display="Alpha CLI"), kind("kind-b", display="Beta CLI")]
    wide = ready_build(
        doc(rows_, kinds=kinds, workspaces=workspaces),
        flt=s.Filter("7d"),
        now_ms=NOW_MS,
        width=sm.WIDTH_WIDE,
    )
    assert wide.spaces is not None and len(wide.spaces.rows) == 7
    assert wide.space_total == 7
    assert wide.spaces_more is False
    phone = ready_build(
        doc(rows_, kinds=kinds, workspaces=workspaces),
        flt=s.Filter("7d"),
        now_ms=NOW_MS,
        width=sm.WIDTH_COMPACT,
    )
    assert phone.space_total == 7
    assert phone.spaces_more is True


def test_the_cost_amount_and_its_partial_flag_are_separate_fields() -> None:
    kinds = [kind("kind-a", display="Alpha CLI")]
    rows_ = [
        row("workspace", "kind-a", 2_000_000, space="s0", unpriced=500_000),
        row("overall", "kind-a", 2_000_000, unpriced=500_000),
    ]
    result = ready_build(
        doc(rows_, kinds=kinds, workspaces=[("s0", "Space 0")]),
        flt=s.Filter("7d"),
        now_ms=NOW_MS,
        width=sm.WIDTH_WIDE,
    )
    assert result.spaces is not None
    [space] = result.spaces.rows
    assert space.partial is True and space.amount and "Partial" in space.subline
    assert "Partial" not in space.amount


# The Today line uses the host's zone, never a guess ------------------------

_MIDDAY_UTC = datetime(2026, 10, 12, 13, 5, tzinfo=UTC)


@pytest.mark.parametrize(
    ("zone", "expected"),
    [
        ("Asia/Riyadh", "Today · Oct 12 · since midnight · now 16:05 Riyadh time"),
        ("America/New_York", "Today · Oct 12 · since midnight · now 09:05 New York time"),
        ("UTC", "Today · Oct 12 · since midnight · now 13:05 UTC"),
    ],
)
def test_the_today_line_says_the_host_clock_and_city(zone: str, expected: str) -> None:
    result = sm.build(
        None,
        state=sm.STATE_LOADING,
        flt=s.Filter("today"),
        now_ms=int(_MIDDAY_UTC.timestamp() * 1000),
        width=sm.WIDTH_COMPACT,
        timezone=zone,
    )
    assert result.range_text == expected


def test_a_loading_screen_keeps_the_host_zone_of_the_previous_page() -> None:
    now_ms = int(_MIDDAY_UTC.timestamp() * 1000)
    built = sm.build(
        None,
        state=sm.STATE_LOADING,
        flt=s.Filter("today"),
        now_ms=now_ms,
        width=sm.WIDTH_COMPACT,
        timezone="Asia/Riyadh",
    )
    assert "16:05 Riyadh time" in built.meta and "loading…" in built.meta
    assert "UTC" not in built.meta


def test_an_unknown_zone_omits_the_clock_and_never_says_utc() -> None:
    result = sm.build(
        None,
        state=sm.STATE_LOADING,
        flt=s.Filter("today"),
        now_ms=int(_MIDDAY_UTC.timestamp() * 1000),
        width=sm.WIDTH_COMPACT,
    )
    assert result.range_text.endswith("since midnight")
    assert "now" not in result.range_text and "UTC" not in result.range_text


def test_zone_labels() -> None:
    assert sm.zone_label("Asia/Riyadh") == "Riyadh time"
    assert sm.zone_label("America/New_York") == "New York time"
    assert sm.zone_label("America/Argentina/Buenos_Aires") == "Buenos Aires time"
    assert sm.zone_label("UTC") == "UTC"
    assert sm.zone_label("Etc/GMT+3") == "GMT+3"
