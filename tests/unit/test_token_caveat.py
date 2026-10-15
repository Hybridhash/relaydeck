"""the token-totals caveat is built from the page's kinds and coverage only.

Synthetic kinds (neutral display names). Nothing here names a real tool, and the generator never
hard-codes one: each line comes from the page.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from relaydeck.client.features.usage import model
from relaydeck.client.features.usage import screen_model as sm

NOW_MS = int(datetime(2026, 10, 12, 12, 0, tzinfo=UTC).timestamp() * 1000)


def _page(kinds: list[dict[str, Any]], coverage: list[dict[str, Any]]) -> model.UsagePage:
    doc = {
        "schema_version": 1,
        "host_id": "h1",
        "generated_ms": NOW_MS,
        "timezone": "UTC",
        "price_table_date": "2026-10-01",
        "next_cursor": None,
        "rows": [],
        "limits": [],
        "coverage": coverage,
        "snapshot_labels": [],
        "kinds": kinds,
        "workspaces": [],
        "capacity": "ok",
    }
    return model.parse_usage_page(doc)


def _kind(kind: str, name: str, tokens_reported: bool = True) -> dict[str, Any]:
    return {
        "kind": kind,
        "display_name": name,
        "tokens_reported": tokens_reported,
        "limits_setup": None,
        "sources": [],
    }


def _cov(kind: str, state: str) -> dict[str, Any]:
    return {"kind": kind, "coverage": state, "observed_ms": None, "unmapped_sessions": 0}


def test_none_excluded_gives_no_caveat_line() -> None:
    page = _page(
        [_kind("kind-a", "Alpha CLI"), _kind("kind-b", "Beta CLI")],
        [_cov("kind-a", "complete"), _cov("kind-b", "complete")],
    )
    assert sm.token_caveat(page) == ""


def test_one_excluded_for_unverified_counts() -> None:
    page = _page(
        [_kind("kind-a", "Alpha CLI"), _kind("kind-b", "Beta CLI")],
        [_cov("kind-a", "complete"), _cov("kind-b", "unverified")],
    )
    assert sm.token_caveat(page) == (
        "Token totals do not include: Beta CLI (token counts not verified yet)."
    )


def test_two_excluded_are_joined_with_a_middle_dot() -> None:
    page = _page(
        [
            _kind("kind-a", "Alpha CLI"),
            _kind("kind-b", "Beta CLI"),
            _kind("kind-c", "Gamma CLI", tokens_reported=False),
        ],
        [_cov("kind-a", "complete"), _cov("kind-b", "unverified"), _cov("kind-c", "complete")],
    )
    assert sm.token_caveat(page) == (
        "Token totals do not include: Beta CLI (token counts not verified yet) · "
        "Gamma CLI (reports no token counts)."
    )


def test_a_tool_that_reports_no_tokens_is_excluded_for_that_reason() -> None:
    page = _page(
        [_kind("kind-c", "Gamma CLI", tokens_reported=False)],
        [_cov("kind-c", "complete")],
    )
    assert (
        sm.token_caveat(page) == "Token totals do not include: Gamma CLI (reports no token counts)."
    )


def test_a_tool_with_no_reading_right_now_is_excluded_for_that_reason() -> None:
    page = _page(
        [_kind("kind-d", "Delta CLI")],
        [_cov("kind-d", "unavailable")],
    )
    assert sm.token_caveat(page) == "Token totals do not include: Delta CLI (no reading right now)."


def test_each_different_reason_is_named_once_for_its_tool() -> None:
    page = _page(
        [
            _kind("kind-a", "Alpha CLI", tokens_reported=False),
            _kind("kind-b", "Beta CLI"),
            _kind("kind-c", "Gamma CLI"),
            _kind("kind-d", "Delta CLI"),
        ],
        [
            _cov("kind-a", "complete"),
            _cov("kind-b", "unverified"),
            _cov("kind-c", "complete"),
            _cov("kind-d", "unavailable"),
        ],
    )
    line = sm.token_caveat(page)
    assert line == (
        "Token totals do not include: Alpha CLI (reports no token counts) · "
        "Beta CLI (token counts not verified yet) · "
        "Delta CLI (no reading right now)."
    )
    assert "Gamma CLI" not in line  # the included tool is not named


def test_names_come_from_the_page_and_are_never_guessed() -> None:
    page = _page(
        [_kind("zeta-7", "Zeta Tool", tokens_reported=False)],
        [_cov("zeta-7", "complete")],
    )
    line = sm.token_caveat(page)
    assert "Zeta Tool" in line
    assert "zeta-7" not in line  # the display name is used, not the internal kind id


def test_the_line_is_one_sentence_and_wording_is_stable() -> None:
    page = _page(
        [_kind("kind-b", "Beta CLI")],
        [_cov("kind-b", "unverified")],
    )
    line = sm.token_caveat(page)
    assert line.startswith("Token totals do not include: ") and line.endswith(".")
    assert line.count(".") == 1
