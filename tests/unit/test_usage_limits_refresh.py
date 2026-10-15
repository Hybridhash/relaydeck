"""LIMITS-REFRESH-UI model side: the additive wire fields (older servers send none), the refresh
button's states, the credit-row wording and how a started read settles. Synthetic data; tool names
come only from the page."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from relaydeck.client.features.usage import model
from relaydeck.client.features.usage import screen_model as sm
from relaydeck.client.features.usage import selectors as s

NOW = int(datetime(2026, 10, 12, 12, tzinfo=UTC).timestamp() * 1000)
MIN = 60_000
DAY = 86_400_000


def _kind(kind: str, name: str, refresh: Any = None) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "kind": kind,
        "display_name": name,
        "tokens_reported": False,
        "limits_setup": None,
        "sources": [],
    }
    if refresh is not None:
        doc["limits_refresh"] = refresh
    return doc


def _window(percent: float, minutes: int, resets: int | None, **extra: Any) -> dict[str, Any]:
    return {
        "window_key": f"w{minutes}",
        "used_percent": percent,
        "window_minutes": minutes,
        "resets_at_ms": resets,
        "status": "fresh",
        **extra,
    }


def _limit(
    kind: str, windows: list[dict[str, Any]], observed: int = NOW - 2 * MIN, **extra: Any
) -> dict[str, Any]:
    return {
        "kind": kind,
        "quota_key": f"{kind}-q",
        "observed_ms": observed,
        "status": "fresh",
        "windows": windows,
        **extra,
    }


def _page(kinds: list[Any], limits: list[Any]) -> model.UsagePage:
    return model.parse_usage_page(
        {
            "schema_version": 1,
            "host_id": "h1",
            "generated_ms": NOW,
            "timezone": "Asia/Riyadh",
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


def _plan(page: model.UsagePage, local: Any = None, now: int = NOW) -> dict[str, sm.LimitRowModel]:
    rows = sm._limit_rows(page, now, s.Filter("7d"), "Asia/Riyadh", local)  # noqa: SLF001
    return {row.key: row for row in rows}


AVAILABLE = {"available": True, "reason": None, "in_progress": False}


def test_an_older_server_has_no_refresh_fields() -> None:
    page = _page([_kind("k1", "Tool One")], [_limit("k1", [_window(40, 10080, NOW + DAY)])])
    assert page.kinds[0].refresh is None
    row = _plan(page)["k1"]
    assert row.refresh is None
    assert row.windows[0].amount == ""
    assert row.subline == "Read 2 min ago"


def test_refresh_fields_are_parsed_and_malformed_ones_are_dropped() -> None:
    cooldown = {
        "available": True,
        "reason": None,
        "in_progress": True,
        "cooldown_until_ms": NOW + 3 * MIN,
        "last_error": "timeout",
    }
    page = _page([_kind("k1", "Tool One", cooldown), _kind("k2", "Tool Two", "bogus")], [])
    info = page.kinds[0].refresh
    assert info == model.LimitsRefresh(True, None, True, NOW + 3 * MIN, "timeout")
    assert page.kinds[1].refresh is None  # not an object: treated as an older server


def test_a_credit_window_reads_x_of_y_credits_with_reset_date_and_plan() -> None:
    reset = int(datetime(2026, 11, 1, 0, tzinfo=UTC).timestamp() * 1000)
    window = _window(0.3, 43200, reset, used_amount=0.15, limit_amount=50, unit="credit")
    page = _page(
        [_kind("k1", "Tool One", AVAILABLE)],
        [_limit("k1", [window], plan_label="TOOL FREE")],
    )
    row = _plan(page)["k1"]
    shown = row.windows[0]
    assert shown.amount == "0.15 of 50 credits"
    assert shown.percent == "<1%"  # 0.15 of 50 is used, just under one percent
    assert shown.fraction == 0.003
    assert shown.resets == "Resets Nov 1 · TOOL FREE"
    assert row.subline == "Checked 2 min ago"


def test_amount_text_and_units() -> None:
    def line(used: float, limit: float, unit: str | None) -> str:
        return sm.amount_line(s.LimitWindow("w", "Monthly", 1.0, None, "fresh", used, limit, unit))

    assert line(1234.5, 5000, "credit") == "1,234.5 of 5,000 credits"
    assert line(12, 100, "request") == "12 of 100 request"
    assert line(3, 10, None) == "3 of 10"
    assert line(1, 0, "credit") == "1 credit used"  # no limit amount: the amount alone
    assert line(2.5, 0, "credit") == "2.5 credits used"
    assert sm.amount_line(s.LimitWindow("w", "Weekly", 5.0, None, "fresh")) == ""


def test_no_space_adds_the_hint_to_the_not_connected_line() -> None:
    hint = {"available": False, "reason": "no_space", "in_progress": False}
    plain = {"available": False, "reason": None, "in_progress": False}
    page = _page(
        [_kind("k1", "Tool One", hint), _kind("k2", "Tool Two", plain), _kind("k3", "Tool Three")],
        [],
    )
    rows = _plan(page)
    assert rows["k1"].subline == "Limits not connected yet · open it in a Herdr space once"
    assert rows["k2"].subline == "Limits not connected yet"
    assert rows["k3"].subline == "Limits not connected yet"
    assert rows["k1"].refresh is None and rows["k2"].refresh is None


def _refresh_row(info: dict[str, Any], local: Any = None, now: int = NOW) -> sm.RefreshModel:
    page = _page([_kind("k1", "Tool One", info)], [_limit("k1", [_window(40, 10080, NOW + DAY)])])
    refresh = _plan(page, {"k1": local} if local else None, now)["k1"].refresh
    assert refresh is not None
    return refresh


def test_an_available_tool_has_an_enabled_button_with_an_accessible_label() -> None:
    refresh = _refresh_row(AVAILABLE)
    assert refresh.enabled and not refresh.busy and refresh.note == ""
    assert refresh.label == "Refresh Tool One limits"


def test_in_progress_shows_a_spinner_and_disables_the_button() -> None:
    refresh = _refresh_row({**AVAILABLE, "in_progress": True})
    assert refresh.busy and not refresh.enabled
    local = sm.RefreshLocal(sm.REFRESH_RUNNING, at_ms=NOW)
    assert _refresh_row(AVAILABLE, local).busy


def test_the_cooldown_disables_the_button_and_says_how_long() -> None:
    refresh = _refresh_row({**AVAILABLE, "cooldown_until_ms": NOW + 3 * MIN + 1})
    assert not refresh.enabled and not refresh.busy
    assert refresh.note == "refresh in 4 min"
    soon = _refresh_row({**AVAILABLE, "cooldown_until_ms": NOW + 10_000})
    assert soon.note == "refresh in 1 min"
    past = _refresh_row({**AVAILABLE, "cooldown_until_ms": NOW - 1})
    assert past.enabled and past.note == ""


def test_a_cooldown_learnt_from_the_answer_counts_too() -> None:
    local = sm.RefreshLocal(sm.REFRESH_COOLDOWN, until_ms=NOW + 2 * MIN, at_ms=NOW)
    refresh = _refresh_row(AVAILABLE, local)
    assert not refresh.enabled and refresh.note == "refresh in 2 min"


def test_a_failure_leaves_the_button_enabled_with_a_short_line() -> None:
    for code, text in (
        ("timeout", "couldn't read it, try again"),
        ("auth_required", "sign in to the tool first"),
        ("parse", "unexpected screen"),
        ("something_new", "couldn't read it, try again"),
    ):
        refresh = _refresh_row(AVAILABLE, sm.RefreshLocal(sm.REFRESH_FAILED, error=code, at_ms=NOW))
        assert refresh.enabled and refresh.note == text
    from_server = _refresh_row({**AVAILABLE, "last_error": "auth_required"})
    assert from_server.enabled and from_server.note == "sign in to the tool first"


def test_success_says_checked_just_now_for_a_minute() -> None:
    local = sm.RefreshLocal(sm.REFRESH_DONE, at_ms=NOW - 10_000)
    assert _refresh_row(AVAILABLE, local).note == "checked just now"
    old = sm.RefreshLocal(sm.REFRESH_DONE, at_ms=NOW - 2 * MIN)
    assert _refresh_row(AVAILABLE, old).note == ""


def _running(attempt: str | None = "a1", base: int | None = NOW - 5 * MIN) -> sm.RefreshLocal:
    return sm.RefreshLocal(sm.REFRESH_RUNNING, at_ms=NOW, base_ms=base, attempt_id=attempt)


def _info(
    *, busy: bool = False, attempt: tuple[str, str] | None = None, error: str | None = None
) -> model.LimitsRefresh:
    last = model.RefreshAttempt(attempt[0], NOW, attempt[1]) if attempt else None
    return model.LimitsRefresh(True, in_progress=busy, last_error=error, last_attempt=last)


def test_success_needs_the_same_attempt_ok_and_a_newer_reading() -> None:
    done = sm.settle_refresh(_running(), _info(attempt=("a1", "ok")), NOW, NOW + 3_000)
    assert done.phase == sm.REFRESH_DONE


def test_in_progress_ending_is_not_success() -> None:
    ended = sm.settle_refresh(_running(), _info(), NOW - 5 * MIN, NOW + 5_000)
    assert ended.phase == sm.REFRESH_RUNNING


def test_another_attempt_or_an_old_reading_is_not_this_success() -> None:
    other = sm.settle_refresh(_running(), _info(attempt=("zz", "ok")), NOW, NOW + 3_000)
    assert other.phase == sm.REFRESH_RUNNING  # a newer reading, but not our attempt
    stale = sm.settle_refresh(_running(), _info(attempt=("a1", "ok")), NOW - 5 * MIN, NOW + 3_000)
    assert stale.phase == sm.REFRESH_RUNNING  # ok, but the snapshot is not newer yet


def test_our_failed_attempt_is_a_failure_named_by_its_result() -> None:
    failed = sm.settle_refresh(_running(), _info(attempt=("a1", "parse")), None, NOW + 2_000)
    assert failed.phase == sm.REFRESH_FAILED and failed.error == "parse"


def test_after_the_wait_a_read_still_in_progress_keeps_checking() -> None:
    late = sm.settle_refresh(_running(), _info(busy=True), NOW - 5 * MIN, NOW + 41_000)
    assert late.phase == sm.REFRESH_RUNNING
    row = _refresh_row({**AVAILABLE, "in_progress": True}, late, now=NOW + 41_000)
    assert row.busy and not row.enabled and row.note == "still checking…"
    early = _refresh_row({**AVAILABLE, "in_progress": True}, late, now=NOW + 10_000)
    assert early.note == "reading…"


def test_after_the_wait_an_unmatched_end_is_a_failure_or_dropped() -> None:
    failed = sm.settle_refresh(_running(), _info(error="timeout"), NOW - 5 * MIN, NOW + 41_000)
    assert failed.phase == sm.REFRESH_FAILED and failed.error == "timeout"
    quiet = sm.settle_refresh(_running(), _info(), NOW - 5 * MIN, NOW + 41_000)
    assert quiet.phase == sm.REFRESH_FAILED and quiet.error == "timeout"
    dropped = sm.settle_refresh(_running(), _info(), NOW, NOW + 41_000)
    assert dropped.phase == sm.REFRESH_IDLE  # a newer reading arrived some other way


def test_attempt_fields_are_parsed() -> None:
    doc = {**AVAILABLE, "last_attempt": {"id": "a1", "finished_ms": NOW, "result": "ok"}}
    page = _page(
        [
            _kind("k1", "Tool One", doc),
            _kind("k2", "Tool Two", {**AVAILABLE, "last_attempt": {"id": 3}}),
        ],
        [],
    )
    assert page.kinds[0].refresh is not None
    assert page.kinds[0].refresh.last_attempt == model.RefreshAttempt("a1", NOW, "ok")
    assert page.kinds[1].refresh is not None and page.kinds[1].refresh.last_attempt is None


def test_no_limit_amount_shows_the_amount_alone_without_bar_or_percent() -> None:
    for limit in (0, None):
        extra = {"used_amount": 12.5, "unit": "credit"}
        if limit is not None:
            extra["limit_amount"] = limit
        window = _window(0, 43200, NOW + DAY, **extra)
        page = _page([_kind("k1", "Tool One")], [_limit("k1", [window])])
        shown = _plan(page)["k1"].windows[0]
        assert shown.amount == "12.5 credits used"
        assert not shown.bar and shown.percent == ""


def test_over_allowance_is_a_full_bar_and_over_plan() -> None:
    window = _window(100, 43200, NOW + DAY, used_amount=60, limit_amount=50, unit="credit")
    window["over_allowance"] = True
    window["used_percent"] = 120  # out of range on the wire: clamped, never an error
    page = _page([_kind("k1", "Tool One")], [_limit("k1", [window])])
    shown = _plan(page)["k1"].windows[0]
    assert shown.amount == "60 of 50 credits"
    assert shown.percent == "over plan" and shown.fraction == 1.0 and shown.role == "danger"
