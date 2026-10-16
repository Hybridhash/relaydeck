"""A limit reading is stale only when it may be wrong, not merely old."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from relaydeck.client.features.usage import model, screen_model
from relaydeck.client.features.usage import selectors as s

NOW = int(datetime(2026, 10, 16, 6, 7, tzinfo=ZoneInfo("UTC")).timestamp() * 1000)
HOUR = 3_600_000
MINUTE = 60_000
LATER = NOW + 86_400_000


def kind_doc(kind: str = "k", **extra: Any) -> dict[str, Any]:
    return {
        "kind": kind,
        "display_name": kind.upper(),
        "tokens_reported": True,
        "limits_setup": {"state": "installed", "checked_ms": NOW},
        "sources": [],
        **extra,
    }


def window(
    minutes: int = 300, used: float = 12, resets: int | None = LATER, **extra: Any
) -> dict[str, Any]:
    return {
        "window_key": f"w{minutes}",
        "used_percent": used,
        "window_minutes": minutes,
        "resets_at_ms": resets,
        "status": "fresh",
        **extra,
    }


def reading(kind: str, observed: int, windows: list[dict[str, Any]], **extra: Any) -> dict:
    return {
        "kind": kind,
        "quota_key": f"{kind}-quota",
        "observed_ms": observed,
        "status": "fresh",
        "windows": windows,
        **extra,
    }


def page(kinds: list[dict[str, Any]], limits: list[dict[str, Any]]) -> model.UsagePage:
    return model.parse_usage_page(
        {
            "schema_version": 1,
            "host_id": "h1",
            "generated_ms": NOW,
            "timezone": "UTC",
            "price_table_date": None,
            "next_cursor": None,
            "rows": [],
            "limits": limits,
            "coverage": [],
            "snapshot_labels": [],
            "kinds": kinds,
            "workspaces": [],
            "capacity": "ok",
        }
    )


def status_of(kinds: list[dict[str, Any]], limits: list[dict[str, Any]]) -> str:
    return s.providers(page(kinds, limits), NOW)[0].status


def test_an_old_reading_of_an_unused_tool_is_current() -> None:
    kind = kind_doc(last_activity_ms=NOW - 5 * HOUR)
    assert status_of([kind], [reading("k", NOW - 3 * HOUR, [window()])]) == s.STATUS_OK


def test_use_after_the_reading_makes_it_stale() -> None:
    kind = kind_doc(last_activity_ms=NOW - HOUR)
    assert status_of([kind], [reading("k", NOW - 3 * HOUR, [window()])]) == s.STATUS_STALE


def test_a_tool_with_no_events_at_all_keeps_its_reading() -> None:
    kind = kind_doc(last_activity_ms=None)
    assert status_of([kind], [reading("k", NOW - 3 * HOUR, [window()])]) == s.STATUS_OK


def test_an_older_server_falls_back_to_the_age_rule() -> None:
    kind = kind_doc()  # no activity field at all
    assert status_of([kind], [reading("k", NOW - 3 * HOUR, [window()])]) == s.STATUS_STALE
    assert status_of([kind], [reading("k", NOW - 5 * MINUTE, [window()])]) == s.STATUS_OK


def test_a_timed_source_is_stale_after_three_missed_checks() -> None:
    refresh = {"available": False, "reason": "not_configured", "in_progress": False}
    kind = kind_doc(last_activity_ms=NOW - 5 * HOUR, limits_refresh=refresh)
    stale = reading("k", NOW - 16 * MINUTE, [window()], source="timer")
    live = reading("k", NOW - 4 * MINUTE, [window()], source="timer")
    assert status_of([kind], [stale]) == s.STATUS_STALE
    assert status_of([kind], [live]) == s.STATUS_OK


def test_a_failed_last_attempt_makes_the_reading_stale() -> None:
    refresh = {"available": True, "reason": None, "in_progress": False, "last_error": "timeout"}
    kind = kind_doc(last_activity_ms=NOW - 5 * HOUR, limits_refresh=refresh)
    assert status_of([kind], [reading("k", NOW - HOUR, [window()], source="pane")]) == (
        s.STATUS_STALE
    )
    timed = kind_doc(
        last_activity_ms=NOW - 5 * HOUR,
        limits_state={"state": "error", "checked_ms": NOW, "error": "offline"},
    )
    assert status_of([timed], [reading("k", NOW - MINUTE, [window()], source="t")]) == (
        s.STATUS_STALE
    )


def test_the_other_actionable_states_are_unchanged() -> None:
    kind = kind_doc(last_activity_ms=NOW - 5 * HOUR)
    ended = reading("k", NOW - HOUR, [window(resets=NOW - 1)])
    near = reading("k", NOW - HOUR, [window(used=85)])
    failed = reading("k", NOW - HOUR, [], source="pane", error="unavailable")
    assert status_of([kind], [ended]) == s.STATUS_WINDOW_ENDED
    assert status_of([kind], [near]) == s.STATUS_NEAR_LIMIT
    assert status_of([kind], [failed]) == s.STATUS_READING_UNAVAILABLE
    missing = {**kind, "limits_setup": {"state": "script_missing", "checked_ms": NOW}}
    assert status_of([missing], [near]) == s.STATUS_SETUP_NEEDED


def old_unused_readings_page() -> model.UsagePage:
    first = kind_doc("kind-a", last_activity_ms=NOW - 4 * HOUR)
    pane = {"available": True, "reason": None, "in_progress": False, "last_error": None}
    second = kind_doc("kind-b", last_activity_ms=NOW - 4 * 24 * HOUR, limits_refresh=pane)
    return page(
        [first, second],
        [
            reading("kind-a", NOW - 3 * HOUR, [window()]),
            reading("kind-b", NOW - 17 * HOUR, [window(10080, 0.3, LATER)], source="pane"),
        ],
    )


def test_old_readings_of_unused_tools_need_no_attention() -> None:
    parsed = old_unused_readings_page()
    assert s.attention(parsed, NOW, compact=True).items == ()
    rows = screen_model._limit_rows(parsed, NOW, s.Filter("30d", s.ALL, s.ALL), "UTC")
    assert [row.subline for row in rows] == [
        "Read 3 h ago · no use since",
        "Checked 17 h ago · no use since",
    ]
    assert all(row.status == s.STATUS_OK for row in rows)


def test_an_event_after_the_reading_needs_attention() -> None:
    parsed = old_unused_readings_page()
    first = replace(parsed.kinds[0], last_activity_ms=NOW - MINUTE)
    used = replace(parsed, kinds=(first, *parsed.kinds[1:]))
    items = s.attention(used, NOW, compact=True).items
    assert [(p.kind, p.status) for p in items] == [("kind-a", s.STATUS_STALE)]


def test_an_idle_window_is_a_neutral_reading() -> None:
    kind = kind_doc("kind-a", last_activity_ms=NOW - 7 * HOUR)
    idle = reading("kind-a", NOW - MINUTE, [window(300, 0, None, idle=True)], source="pane")
    parsed = page([kind], [idle])
    assert s.attention(parsed, NOW, compact=True).items == ()
    (row,) = screen_model._limit_rows(parsed, NOW, s.Filter("30d", s.ALL, s.ALL), "UTC")
    assert row.subline == "5-hour window idle"
    assert row.windows[0].resets == "Idle · no use yet"


@pytest.mark.parametrize("active_after", [False, True])
def test_the_console_card_ranks_only_readings_that_may_be_right(active_after: bool) -> None:
    activity = NOW - MINUTE if active_after else NOW - 5 * HOUR
    parsed = page(
        [kind_doc(last_activity_ms=activity)], [reading("k", NOW - 3 * HOUR, [window(used=40)])]
    )
    card = model.console_card(parsed, NOW)
    assert (card.pick is None) is active_after
