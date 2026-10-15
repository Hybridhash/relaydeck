"""The Console strip's second line: every tool's top fresh limit window ranked by used %, the
fallback to the most used tool, stale readings left out, and narrow versus wide. Generic: the
tool names come from the page."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from relaydeck.client.features.usage import model
from relaydeck.client.features.usage import screen_model as sm

NOW = int(datetime(2026, 10, 11, 12, tzinfo=UTC).timestamp() * 1000)
DAY_MS = 86_400_000
EMPTY = {
    "input_uncached": 0,
    "cache_read": 0,
    "cache_write_short": 0,
    "cache_write_long": 0,
    "cache_write_unknown": 0,
    "output": 0,
    "reasoning": 0,
}


def _kind(kind: str, name: str) -> dict[str, Any]:
    return {
        "kind": kind,
        "display_name": name,
        "tokens_reported": True,
        "limits_setup": None,
        "sources": [],
    }


def _window(minutes: int, used: float, resets: int | None) -> dict[str, Any]:
    return {
        "window_key": f"w{minutes}",
        "used_percent": used,
        "window_minutes": minutes,
        "resets_at_ms": resets,
        "status": "fresh",
    }


def _limit(kind: str, windows: list[dict[str, Any]], observed: int = NOW) -> dict[str, Any]:
    return {
        "kind": kind,
        "quota_key": f"{kind}-q",
        "observed_ms": observed,
        "status": "fresh",
        "windows": windows,
    }


def _row(kind: str, tokens: int) -> dict[str, Any]:
    return {
        "day": "2026-10-11",
        "workspace_id": None,
        "kind": kind,
        "model": "m",
        "counters": {**EMPTY, "input_uncached": tokens},
        "sessions": 1,
        "cost_micro_usd": tokens,
        "scope": "overall",
        "price_snapshot": None,
        "priced_cost_micro_usd": tokens,
        "priced_tokens": tokens,
        "unpriced_tokens": 0,
        "hour": None,
    }


def _card(
    limits: list[dict[str, Any]], tokens: dict[str, int] | None = None, tools: int = 4
) -> sm.ConsoleCardModel:
    names = [("k1", "Alpha"), ("k2", "Beta"), ("k3", "Gamma"), ("k4", "Delta")][:tools]
    doc = {
        "schema_version": 1,
        "host_id": "h1",
        "generated_ms": NOW,
        "timezone": "UTC",
        "price_table_date": None,
        "next_cursor": None,
        "rows": [_row(kind, count) for kind, count in (tokens or {}).items()],
        "limits": limits,
        "coverage": [],
        "snapshot_labels": [],
        "kinds": [_kind(kind, name) for kind, name in names],
        "workspaces": [],
        "capacity": "ok",
    }
    page = model.parse_usage_page(doc)
    return sm.console_card(page, state=sm.STATE_READY, now_ms=NOW)


SOON = NOW + 2 * DAY_MS
LATER = NOW + 5 * DAY_MS


def test_no_tool_has_a_limit_reading_shows_the_most_used_tool_today() -> None:
    card = _card([], {"k1": 400_000_000, "k2": 1_200_000_000})
    assert sm.console_line2(card) == "Most used: Beta 1.20B"


def test_no_limits_and_no_usage_today_hides_line_2() -> None:
    assert sm.console_line2(_card([], {})) == ""


def test_one_tool_with_a_limit_shows_its_window_and_reset() -> None:
    card = _card([_limit("k1", [_window(10080, 51, SOON)])], {"k1": 5})
    line = sm.console_line2(card)
    assert line.startswith("Alpha 51% weekly · resets ")


def test_each_tool_once_with_its_highest_window_ranked_by_used_percent() -> None:
    card = _card(
        [
            _limit("k1", [_window(300, 20, SOON), _window(10080, 51, LATER)]),
            _limit("k2", [_window(10080, 19, SOON)]),
        ],
        {"k1": 5},
    )
    assert card.picks == ("Alpha 51% weekly", "Beta 19% weekly")
    assert sm.console_line2(card) == "Alpha 51% weekly · Beta 19% weekly"
    assert card.pick.startswith("Alpha 51% weekly")


def test_ties_go_to_the_sooner_reset() -> None:
    card = _card(
        [_limit("k1", [_window(10080, 40, LATER)]), _limit("k2", [_window(10080, 40, SOON)])]
    )
    assert card.picks[0].startswith("Beta")


def test_three_or_more_tools_add_a_plus_count() -> None:
    card = _card(
        [
            _limit("k1", [_window(10080, 51, SOON)]),
            _limit("k2", [_window(10080, 19, SOON)]),
            _limit("k3", [_window(10080, 7, SOON)]),
            _limit("k4", [_window(10080, 3, SOON)]),
        ]
    )
    assert sm.console_line2(card, wide=True) == "Alpha 51% weekly · Beta 19% weekly · +2"


def test_stale_readings_are_excluded() -> None:
    stale = NOW - 3 * DAY_MS
    card = _card(
        [
            _limit("k1", [_window(10080, 90, LATER)], observed=stale),
            _limit("k2", [_window(10080, 19, SOON)]),
        ],
        {"k1": 7},
    )
    assert card.picks == ("Beta 19% weekly",)  # Alpha's 90% reading is stale and left out
    assert "Alpha" not in sm.console_line2(card)


def test_an_ended_window_is_excluded() -> None:
    card = _card([_limit("k1", [_window(10080, 90, NOW - 1)])], {"k1": 9})
    assert card.picks == ()
    assert sm.console_line2(card).startswith("Most used: Alpha")


def test_narrow_keeps_one_tool_when_two_do_not_fit_and_wide_shows_two() -> None:
    long_names = [
        _limit("k1", [_window(10080, 51, SOON)]),
        _limit("k2", [_window(10080, 19, SOON)]),
    ]
    card = _card(long_names)
    card = sm.ConsoleCardModel(
        **{
            **card.__dict__,
            "picks": ("A Very Long Tool Name 51% weekly", "Another Long Tool Name 19% weekly"),
        }
    )
    narrow = sm.console_line2(card, wide=False)
    wide = sm.console_line2(card, wide=True)
    assert narrow.startswith("A Very Long Tool Name 51% weekly") and "Another" not in narrow
    assert narrow.endswith("+1")
    assert wide == "A Very Long Tool Name 51% weekly · Another Long Tool Name 19% weekly"


def test_an_unavailable_strip_has_no_second_line() -> None:
    unavailable = sm.ConsoleCardModel(
        False, False, "Usage is unavailable right now.", "", "", False
    )
    assert sm.console_line2(unavailable) == ""
