"""View-model rules: pure, no Flet.

Synthetic pages only: invented provider and space names and numbers.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import pytest

from relaydeck.client.features.usage import model
from relaydeck.client.features.usage import screen_model as sm
from relaydeck.client.features.usage import selectors as s

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


def row(
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
        "day": TODAY.isoformat(),
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


def page(
    rows: list[dict[str, Any]],
    *,
    kinds: list[dict[str, Any]] | None = None,
    workspaces: list[dict[str, Any]] | None = None,
    capacity: str = "ok",
    coverage: list[dict[str, Any]] | None = None,
) -> model.UsagePage:
    doc = {
        "schema_version": 1,
        "host_id": "h1",
        "generated_ms": NOW_MS,
        "timezone": "UTC",
        "price_table_date": "2026-10-01",
        "next_cursor": None,
        "rows": rows,
        "limits": [],
        "coverage": coverage or [],
        "snapshot_labels": [],
        "kinds": kinds
        if kinds is not None
        else [
            {
                "kind": "kind-a",
                "display_name": "Alpha CLI",
                "tokens_reported": True,
                "limits_setup": None,
                "sources": [],
            },
        ],
        "workspaces": workspaces or [],
        "capacity": capacity,
    }
    return model.parse_usage_page(doc)


def spaces_page(count: int) -> model.UsagePage:
    rows = []
    workspaces = []
    for index in range(count):
        rows.append(row("workspace", "kind-a", (count - index) * 1_000_000, space=f"s{index}"))
        workspaces.append(
            {
                "workspace_id": f"s{index}",
                "label": f"Space {index}",
                "last_used_day": TODAY.isoformat(),
            }
        )
    rows += [row("overall", "kind-a", sum((count - i) * 1_000_000 for i in range(count)))]
    return page(rows, workspaces=workspaces)


def build(
    p: model.UsagePage,
    *,
    width: str = sm.WIDTH_COMPACT,
    flt: s.Filter | None = None,
    state: str = sm.STATE_READY,
    updating: bool = False,
) -> sm.ScreenModel:
    return sm.build(
        p, state=state, flt=flt or s.Filter("7d"), now_ms=NOW_MS, width=width, updating=updating
    )


# -- widths and top N -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("width", "expected"),
    [
        (None, sm.WIDTH_COMPACT),
        (390.0, sm.WIDTH_COMPACT),
        (599.9, sm.WIDTH_COMPACT),
        (600.0, sm.WIDTH_MEDIUM),
        (1023.9, sm.WIDTH_MEDIUM),
        (1024.0, sm.WIDTH_WIDE),
        (1180.0, sm.WIDTH_WIDE),
    ],
)
def test_width_classes_are_by_width_not_device(width: float | None, expected: str) -> None:
    assert sm.width_class(width) == expected


def test_top_n_is_four_on_a_phone_and_eight_otherwise() -> None:
    assert sm.top_n(sm.WIDTH_COMPACT) == 4
    assert sm.top_n(sm.WIDTH_MEDIUM) == 8 and sm.top_n(sm.WIDTH_WIDE) == 8


# -- rollups: top N, then one "Other" row; no rollup of exactly one -------------------------------


def test_six_spaces_on_a_phone_show_four_and_an_other_row_for_two() -> None:
    result = build(spaces_page(6), width=sm.WIDTH_COMPACT)
    assert result.spaces is not None
    assert len(result.spaces.rows) == 4
    assert result.spaces.rollup is not None
    assert result.spaces.rollup.text == "Other · 2 spaces"
    assert result.spaces.rollup.count == 2


def test_five_spaces_on_a_phone_show_all_five_with_no_rollup() -> None:
    result = build(spaces_page(5), width=sm.WIDTH_COMPACT)
    assert result.spaces is not None
    assert len(result.spaces.rows) == 5 and result.spaces.rollup is None


def test_the_regular_width_shows_eight_before_rolling_up() -> None:
    result = build(spaces_page(10), width=sm.WIDTH_MEDIUM)
    assert result.spaces is not None
    assert len(result.spaces.rows) == 8 and result.spaces.rollup is not None
    assert result.spaces.rollup.count == 2


def test_the_share_bar_is_relative_to_the_largest_value_and_the_top_item_is_full() -> None:
    result = build(spaces_page(4), width=sm.WIDTH_COMPACT)
    assert result.spaces is not None
    assert result.spaces.rows[0].share == 1.0
    assert 0 < result.spaces.rows[-1].share < 1.0


# -- totals reconcile: every space, Not linked included, adds to the total ----------------------


def test_the_spaces_and_not_linked_add_up_to_the_summary_total() -> None:
    rows = [
        row("overall", "kind-a", 300_000_000),
        row("workspace", "kind-a", 200_000_000, space="s1"),
        row("workspace", "kind-a", 100_000_000, space="s2"),
        row("not_linked", "kind-a", 5_000_000),
    ]
    p = page(
        rows,
        workspaces=[
            {"workspace_id": "s1", "label": "Space Alpha", "last_used_day": TODAY.isoformat()},
            {"workspace_id": "s2", "label": "Docs", "last_used_day": TODAY.isoformat()},
        ],
    )
    result = build(p, flt=s.Filter("today"))
    assert result.summary is not None and result.summary.tokens == "305.0M"
    assert result.spaces is not None
    names = [r.name for r in result.spaces.rows]
    assert names == ["Space Alpha", "Docs", "Not linked"]
    assert [r.value for r in result.spaces.rows] == ["200.0M", "100.0M", "5.0M"]
    assert result.spaces.rows[-1].italic is True


# -- cost and partial ---


def test_an_all_unpriced_selection_shows_an_em_dash_and_its_caption() -> None:
    rows = [row("overall", "kind-a", 1_000_000, unpriced=1_000_000)]
    result = build(page(rows))
    assert result.summary is not None
    assert result.summary.cost == "—"
    assert result.summary.cost_caption == "No pricing available for this selection"
    assert result.summary.partial is False


def test_a_partial_selection_shows_the_priced_subtotal_and_one_plural_note() -> None:
    rows = [
        row("overall", "kind-a", 1_000_000, hour=None),
        row("overall", "kind-b", 500_000, unpriced=500_000),
        row("overall", "kind-c", 300_000, unpriced=300_000),
    ]
    kinds = [
        {
            "kind": k,
            "display_name": k.upper(),
            "tokens_reported": True,
            "limits_setup": None,
            "sources": [],
        }
        for k in ("kind-a", "kind-b", "kind-c")
    ]
    result = build(page(rows, kinds=kinds))
    assert result.summary is not None and result.summary.partial is True
    assert result.summary.cost == "$1.00"
    assert result.summary.partial_note == (
        "2 providers (800.0K tokens) have no price yet, so the real total is higher."
    )


def test_a_free_model_is_priced_at_zero_and_never_unpriced() -> None:
    rows = [row("overall", "kind-a", 7_000_000, unpriced=0)]
    rows[0]["priced_cost_micro_usd"] = 0
    rows[0]["cost_micro_usd"] = 0
    result = build(page(rows))
    assert result.summary is not None and result.summary.cost == "$0.00"
    assert result.summary.partial is False and result.summary.cost_caption is None


# -- providers: honest status, one provider, zero usage ---


def test_unverified_and_unreported_providers_are_listed_with_status_text_not_numbers() -> None:
    kinds = [
        {
            "kind": "kind-a",
            "display_name": "Alpha",
            "tokens_reported": True,
            "limits_setup": None,
            "sources": [],
        },
        {
            "kind": "kind-b",
            "display_name": "Beta",
            "tokens_reported": True,
            "limits_setup": None,
            "sources": [],
        },
        {
            "kind": "kind-c",
            "display_name": "Gamma",
            "tokens_reported": False,
            "limits_setup": None,
            "sources": [],
        },
    ]
    coverage = [
        {"kind": "kind-b", "coverage": "unverified", "observed_ms": None, "unmapped_sessions": 0}
    ]
    result = build(page([row("overall", "kind-a", 2_000_000)], kinds=kinds, coverage=coverage))
    assert result.providers is not None
    by_name = {r.name: r for r in result.providers.rows}
    assert by_name["Beta"].value == "Tokens not verified"
    assert by_name["Gamma"].value == "Tokens not reported"
    assert by_name["Alpha"].value == "2.0M"
    assert by_name["Alpha"].series == "series_1"
    assert by_name["Beta"].series is None


def test_one_provider_hides_the_by_provider_section() -> None:
    result = build(page([row("overall", "kind-a", 2_000_000)]))
    assert result.providers is None


def test_zero_usage_providers_are_counted_in_words_not_listed() -> None:
    kinds = [
        {
            "kind": "kind-a",
            "display_name": "Alpha",
            "tokens_reported": True,
            "limits_setup": None,
            "sources": [],
        },
        {
            "kind": "kind-b",
            "display_name": "Beta",
            "tokens_reported": True,
            "limits_setup": None,
            "sources": [],
        },
        {
            "kind": "kind-c",
            "display_name": "Gamma",
            "tokens_reported": True,
            "limits_setup": None,
            "sources": [],
        },
    ]
    result = build(page([row("overall", "kind-a", 2_000_000)], kinds=kinds))
    assert result.provider_zero == "2 others had no usage this period"


# -- chart ---


def test_today_chart_labels_every_sixth_hour_and_marks_a_zero_hour_as_a_stub() -> None:
    rows = [
        row("overall", "kind-a", 2_000_000, hour=6),
        row("overall", "kind-a", 5_000_000, hour=12),
    ]
    result = build(page(rows), flt=s.Filter("today"))
    assert result.chart is not None and len(result.chart.bars) == 24
    labels = [bar.label for bar in result.chart.bars]
    assert labels[0] == "00:00" and labels[6] == "06:00" and labels[1] == ""
    assert result.chart.bars[1].stub is True
    assert result.chart.bars[12].peak is True and result.chart.bars[12].value == "5.0M"


def test_a_single_provider_chart_has_no_legend_and_several_have_top_two_and_other() -> None:
    one = build(page([row("overall", "kind-a", 2_000_000)]))
    assert one.chart is not None and one.chart.legend == ()
    kinds = [
        {
            "kind": k,
            "display_name": k.upper(),
            "tokens_reported": True,
            "limits_setup": None,
            "sources": [],
        }
        for k in ("kind-a", "kind-b", "kind-c")
    ]
    rows = [
        row("overall", k, t)
        for k, t in (("kind-a", 9_000_000), ("kind-b", 5_000_000), ("kind-c", 1_000_000))
    ]
    many = build(page(rows, kinds=kinds))
    assert many.chart is not None
    assert [name for _role, name in many.chart.legend] == ["KIND-A", "KIND-B", "Other"]


def test_a_chart_bar_fraction_is_relative_to_the_peak_bucket() -> None:
    rows = [
        row("overall", "kind-a", 2_000_000, hour=1),
        row("overall", "kind-a", 4_000_000, hour=2),
    ]
    result = build(page(rows), flt=s.Filter("today"))
    assert result.chart is not None
    peak = result.chart.bars[2]
    assert peak.peak and sum(seg.fraction for seg in peak.segments) == pytest.approx(1.0)


# -- attention ---


def test_attention_items_have_actions_and_setup_wording() -> None:
    kinds = [
        {
            "kind": "kind-a",
            "display_name": "Alpha",
            "tokens_reported": True,
            "limits_setup": {"state": "script_missing", "checked_ms": NOW_MS},
            "sources": [
                {"format": "fmt", "limits_setup": {"state": "script_missing", "checked_ms": NOW_MS}}
            ],
        }
    ]
    result = build(page([row("overall", "kind-a", 1_000_000)], kinds=kinds))
    [item] = result.attention.items
    assert item.reason == "Limits hook not installed"
    assert (item.action, item.action_label) == ("setup", "Set up")
    assert result.commands[0].lines == (
        "relaydeck usage install --source=kind-a/fmt --install-limits-hook",
    )


def test_an_outdated_hook_collapses_to_the_exact_repair_command() -> None:
    kinds = [
        {
            "kind": "kind-a",
            "display_name": "Alpha",
            "tokens_reported": True,
            "limits_setup": {"state": "script_outdated", "checked_ms": NOW_MS},
            "sources": [
                {
                    "format": "fmt",
                    "limits_setup": {"state": "script_outdated", "checked_ms": NOW_MS},
                }
            ],
        }
    ]
    result = build(page([row("overall", "kind-a", 1_000_000)], kinds=kinds))
    [command] = result.commands
    assert command.lines == ("relaydeck usage repair",)
    assert command.outdated is True


def test_a_mixed_kind_lists_repair_first_then_install() -> None:
    kinds = [
        {
            "kind": "kind-a",
            "display_name": "Alpha",
            "tokens_reported": True,
            "limits_setup": {"state": "script_outdated", "checked_ms": NOW_MS},
            "sources": [
                {
                    "format": "old",
                    "limits_setup": {"state": "script_outdated", "checked_ms": NOW_MS},
                },
                {
                    "format": "new",
                    "limits_setup": {"state": "script_missing", "checked_ms": NOW_MS},
                },
            ],
        }
    ]
    result = build(page([row("overall", "kind-a", 1_000_000)], kinds=kinds))
    [command] = result.commands
    assert command.lines == (
        "relaydeck usage repair",
        "relaydeck usage install --source=kind-a/new --install-limits-hook",
    )
    assert command.outdated is True


def test_no_command_line_mentions_a_placeholder_path() -> None:
    kinds = [
        {
            "kind": "kind-a",
            "display_name": "Alpha",
            "tokens_reported": True,
            "limits_setup": {"state": "script_outdated", "checked_ms": NOW_MS},
            "sources": [
                {
                    "format": "old",
                    "limits_setup": {"state": "script_outdated", "checked_ms": NOW_MS},
                },
                {
                    "format": "new",
                    "limits_setup": {"state": "script_missing", "checked_ms": NOW_MS},
                },
            ],
        }
    ]
    result = build(page([row("overall", "kind-a", 1_000_000)], kinds=kinds))
    lines = [line for command in result.commands for line in command.lines]
    assert lines
    assert all("<path to" not in line for line in lines)


def test_a_source_name_that_is_not_safe_gets_no_command() -> None:
    kinds = [
        {
            "kind": "bad name;rm",
            "display_name": "Bad",
            "tokens_reported": True,
            "limits_setup": None,
            "sources": [
                {"format": "fmt", "limits_setup": {"state": "script_missing", "checked_ms": NOW_MS}}
            ],
        }
    ]
    result = build(page([row("overall", "bad name;rm", 1_000_000)], kinds=kinds))
    assert result.commands == ()


# -- states, copy and the Console card ---


def test_capacity_and_unavailable_states_carry_their_copy_and_no_data() -> None:
    cap = sm.build(
        None, state=sm.STATE_CAPACITY, flt=s.Filter("7d"), now_ms=NOW_MS, width=sm.WIDTH_COMPACT
    )
    assert cap.summary is None and cap.spaces is None
    assert cap.message == "Too much data for this period — choose a shorter period or one space."
    off = sm.build(
        None, state=sm.STATE_UNAVAILABLE, flt=s.Filter("7d"), now_ms=NOW_MS, width=sm.WIDTH_COMPACT
    )
    assert off.message == "Usage is unavailable right now."


def test_an_over_capacity_page_is_never_built_into_figures() -> None:
    result = build(page([], capacity="exceeded"), state=sm.STATE_CAPACITY)
    assert result.summary is None and result.chart is None


def test_offline_meta_and_refresh_meta_use_the_spec_copy() -> None:
    offline = build(page([row("overall", "kind-a", 1_000_000)]), state=sm.STATE_OFFLINE)
    assert "offline · last updated" in offline.meta and offline.meta_tone == "warn"
    updating = build(page([row("overall", "kind-a", 1_000_000)]), updating=True)
    assert updating.meta.endswith("· updating…")


def test_the_console_card_is_unavailable_for_capacity_and_never_no_usage() -> None:
    card = sm.console_card(None, state=sm.STATE_CAPACITY, now_ms=NOW_MS)
    assert card.available is False and card.capacity is True
    assert card.headline == "Too much data today · open Usage"
    assert "No usage" not in card.headline


def test_the_console_card_names_a_partial_price_as_partial_not_unpriced() -> None:
    rows = [
        row("overall", "kind-a", 2_000_000),
        row("overall", "kind-a", 1_000_000, unpriced=1_000_000),
    ]
    card = sm.console_card(page(rows), state=sm.STATE_READY, now_ms=NOW_MS)
    assert card.available is True
    # the headline reads "Today $2.00 · … tokens"; Partial is its own badge.
    assert card.headline.startswith("Today $2.00 ·")
    assert "Partial" not in card.headline
    assert card.partial is True


def test_the_console_card_with_no_usage_says_so() -> None:
    card = sm.console_card(page([]), state=sm.STATE_READY, now_ms=NOW_MS)
    assert card.headline == "No usage today"


def test_a_snapshot_from_an_earlier_host_day_claims_no_today() -> None:
    rows = [row("overall", "kind-a", 2_000_000)]
    next_days = NOW_MS + 2 * 86_400_000
    card = sm.console_card(page(rows), state=sm.STATE_READY, now_ms=next_days)
    assert card.stale_day is True
    assert card.headline == "Last reading is from an earlier day; open Usage to refresh"
    assert "Today" not in card.headline


# -- formatting ---


@pytest.mark.parametrize(
    ("count", "words"),
    [
        (3_220_000_000, "3.22 billion tokens"),
        (741_000_000, "741.0 million tokens"),
        (9_400, "9.4 thousand tokens"),
        (812, "812 tokens"),
    ],
)
def test_accessible_token_words(count: int, words: str) -> None:
    assert sm.tokens_words(count) == words


def test_row_accessible_label_reads_name_tokens_and_cost() -> None:
    rows = [row("workspace", "kind-a", 3_220_000_000, space="s1", unpriced=3_220_000_000)]
    result = build(
        page(
            rows, workspaces=[{"workspace_id": "s1", "label": "Space Alpha", "last_used_day": None}]
        )
    )
    assert result.spaces is not None
    assert result.spaces.rows[0].a11y == "Space Alpha, 3.22 billion tokens, unpriced"


def test_money_uses_separators_and_an_em_dash_when_nothing_is_priced() -> None:
    assert sm.money(1_204_100_000) == "$1,204.10"
    assert sm.money(None) == "—"


def test_the_footnote_names_the_price_snapshot_or_says_it_is_unavailable() -> None:
    result = build(page([row("overall", "kind-a", 1_000_000)]))
    assert result.footnote == (
        "Standard-tier, token-only API-equivalent estimate · price snapshot 2026-10-01"
    )


def test_the_footnote_gives_a_range_for_mixed_snapshots_and_says_when_nothing_is_priced() -> None:
    older = row("overall", "kind-a", 1_000_000)
    older["price_snapshot"] = "2026-09-01"
    newer = row("overall", "kind-a", 2_000_000)
    assert build(page([older, newer])).footnote == (
        "Standard-tier, token-only API-equivalent estimate · price snapshots "
        "2026-09-01 to 2026-10-01"
    )
    unpriced = row("overall", "kind-a", 1_000_000, unpriced=1_000_000)
    footnote = build(page([unpriced])).footnote
    assert "no priced usage" in footnote
    assert "2026-10-01" not in footnote


def test_a_space_selection_totals_only_its_own_rows_and_never_a_false_zero() -> None:
    rows = [
        row("overall", "kind-a", 5_000_000),
        row("workspace", "kind-a", 2_000_000, space="w1"),
        row("workspace", "kind-a", 3_000_000, space="w2"),
        row("not_linked", "kind-a", 700_000),
    ]
    parsed = page(rows, workspaces=[{"workspace_id": "w1", "label": "One", "last_used_day": None}])
    one = build(parsed, flt=s.Filter("7d", space="w1"))
    assert one.summary is not None and one.summary.tokens == s.tokens_text(2_000_000)
    unlinked = build(parsed, flt=s.Filter("7d", space=s.UNLINKED))
    assert unlinked.summary is not None and unlinked.summary.tokens == s.tokens_text(700_000)


def test_the_busiest_day_card_reads_weekday_date_and_its_share() -> None:
    rows = [row("overall", "kind-a", 2_000_000), row("overall", "kind-a", 2_000_000)]
    result = build(page(rows))
    assert result.summary is not None
    assert result.summary.busiest_caption.startswith(f"{TODAY:%a}, {TODAY:%b} {TODAY.day} · ")
    assert result.summary.busiest_caption.endswith("% of the period")


@pytest.mark.parametrize(
    ("name", "tile"), [("Alpha", "Al"), ("Gamma CLI", "Ga"), ("Open Thing", "OT"), ("", "")]
)
def test_monograms_come_from_the_display_name(name: str, tile: str) -> None:
    assert s.monogram(name) == tile
