"""the Usage screen shows how sessions are linked to spaces (wire fields).

Synthetic data only. Every field is optional on the wire: an older server's missing field shows
nothing,
and a field the server did send is shown even when it is zero.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

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
    day: date = TODAY,
    inferred: int | None = None,
    linked: int | None = None,
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "day": day.isoformat(),
        "workspace_id": space if scope == "workspace" else None,
        "kind": kind,
        "model": "m",
        "counters": {**EMPTY, "input_uncached": tokens},
        "sessions": 1,
        "cost_micro_usd": tokens,
        "scope": scope,
        "price_snapshot": "2026-10-01",
        "priced_cost_micro_usd": tokens,
        "priced_tokens": tokens,
        "unpriced_tokens": 0,
        "hour": None,
    }
    if inferred is not None:
        item["inferred_sessions"] = inferred
    if linked is not None:
        item["linked_sessions"] = linked
    return item


def kind(name: str, display: str) -> dict[str, Any]:
    return {
        "kind": name,
        "display_name": display,
        "tokens_reported": True,
        "limits_setup": None,
        "sources": [],
    }


def coverage(
    name: str,
    *,
    linked: int | None = None,
    inferred: int | None = None,
    unmapped: int = 0,
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "kind": name,
        "coverage": "complete",
        "observed_ms": NOW_MS,
        "unmapped_sessions": unmapped,
    }
    if linked is not None:
        entry["linked_sessions"] = linked
    if inferred is not None:
        entry["inferred_sessions"] = inferred
    return entry


def doc(
    rows: list[dict[str, Any]],
    *,
    kinds: list[dict[str, Any]] | None = None,
    coverage_entries: list[dict[str, Any]] | None = None,
    workspaces: list[tuple[str, str]] | None = None,
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
            "limits": [],
            "coverage": coverage_entries or [],
            "snapshot_labels": [],
            "kinds": kinds if kinds is not None else [kind("kind-a", "Alpha Agent")],
            "workspaces": [
                {"workspace_id": key, "label": label, "last_used_day": TODAY.isoformat()}
                for key, label in (workspaces or [])
            ],
            "capacity": "ok",
        }
    )


def build(page: model.UsagePage, flt: s.Filter | None = None) -> sm.ScreenModel:
    return sm.build(
        page,
        state=sm.STATE_READY,
        flt=flt or s.Filter("7d"),
        now_ms=NOW_MS,
        width=sm.WIDTH_WIDE,
    )


def _rows(page: model.UsagePage, flt: s.Filter | None = None) -> dict[str, sm.RowModel]:
    spaces = build(page, flt).spaces
    assert spaces is not None
    return {entry.key: entry for entry in spaces.rows}


# -- parsing: optional, never a false zero


def test_the_link_fields_parse_and_an_absent_field_stays_unknown() -> None:
    page = doc(
        [row("overall", "kind-a", 1_000)],
        coverage_entries=[coverage("kind-a", linked=12, inferred=9, unmapped=59)],
    )
    [entry] = page.coverage
    assert (entry.linked_sessions, entry.inferred_sessions, entry.unmapped_sessions) == (12, 9, 59)
    old = doc([row("overall", "kind-a", 1_000)], coverage_entries=[coverage("kind-a", unmapped=3)])
    [older] = old.coverage
    assert older.linked_sessions is None and older.inferred_sessions is None


def test_a_row_carries_its_link_counts_when_the_server_sent_them() -> None:
    page = doc([row("workspace", "kind-a", 1_000, space="s1", inferred=2, linked=5)])
    assert page.rows[0].linked_sessions == 5 and page.rows[0].inferred_sessions == 2
    older = doc([row("workspace", "kind-a", 1_000, space="s1")])
    assert older.rows[0].linked_sessions is None and older.rows[0].inferred_sessions is None


# -- the link line


def test_the_link_line_reads_linked_of_total_and_inferred() -> None:
    entry = coverage_entry(linked=12, inferred=9, unmapped=59)
    assert sm.link_line(entry) == "12 of 80 sessions linked · 9 inferred"


def test_a_real_zero_inferred_from_a_newer_server_is_shown_as_zero() -> None:
    assert sm.link_line(coverage_entry(linked=4, inferred=0, unmapped=1)) == (
        "4 of 5 sessions linked · 0 inferred"
    )


def test_an_older_server_shows_no_link_line_at_all() -> None:
    assert sm.link_line(coverage_entry(linked=None, inferred=None, unmapped=3)) is None
    assert sm.link_line(None) is None


def coverage_entry(
    *, linked: int | None, inferred: int | None, unmapped: int
) -> model.CoverageEntry:
    return model.CoverageEntry(
        kind="kind-a",
        coverage="complete",
        observed_ms=NOW_MS,
        unmapped_sessions=unmapped,
        linked_sessions=linked,
        inferred_sessions=inferred,
    )


def test_the_by_provider_subline_and_the_providers_screen_carry_the_same_link_line() -> None:
    page = doc(
        [row("overall", "kind-a", 1_000_000), row("overall", "kind-b", 2_000_000)],
        kinds=[kind("kind-a", "Alpha Agent"), kind("kind-b", "Beta Agent")],
        coverage_entries=[
            coverage("kind-a", linked=12, inferred=9, unmapped=59),
            coverage("kind-b", unmapped=4),
        ],
    )
    result = build(page)
    assert result.providers is not None
    by_key = {entry.key: entry for entry in result.providers.rows}
    assert by_key["kind-a"].links == "12 of 80 sessions linked · 9 inferred"
    assert by_key["kind-b"].links is None
    lines = {line.kind: line.links for line in result.connected}
    assert lines == {"kind-a": "12 of 80 sessions linked · 9 inferred", "kind-b": None}


# -- the inferred marker


def test_a_space_with_inferred_sessions_is_marked_in_its_row_and_its_label() -> None:
    page = doc(
        [
            row("workspace", "kind-a", 2_000_000, space="s1", inferred=3),
            row("overall", "kind-a", 2_000_000),
            row("workspace", "kind-a", 1_000_000, space="s2", inferred=0),
            row("overall", "kind-a", 1_000_000),
        ],
        workspaces=[("s1", "Space Alpha"), ("s2", "docs-site")],
    )
    rows = _rows(page)
    assert rows["s1"].inferred is True
    assert "includes inferred sessions" in rows["s1"].a11y
    assert rows["s2"].inferred is False
    assert "includes inferred sessions" not in rows["s2"].a11y


def test_the_inferred_marker_follows_the_selected_period() -> None:
    old = TODAY - timedelta(days=40)
    page = doc(
        [
            row("workspace", "kind-a", 2_000_000, space="s1", day=old, inferred=3),
            row("overall", "kind-a", 2_000_000, day=old),
        ],
        workspaces=[("s1", "Space Alpha")],
    )
    rows = _rows(page, s.Filter("7d"))
    # Outside the 7-day period the space has no usage, so it is not listed; it cannot be marked.
    assert "s1" not in rows
    wide = _rows(page, s.Filter("90d"))
    assert wide["s1"].inferred is True


# -- the Not linked explanation


def test_the_not_linked_row_explains_what_the_group_means() -> None:
    page = doc(
        [
            row("workspace", "kind-a", 2_000_000, space="s1"),
            row("overall", "kind-a", 2_000_000),
            row("not_linked", "kind-a", 500_000),
        ],
        workspaces=[("s1", "Space Alpha")],
    )
    rows = _rows(page)
    unlinked = rows[s.UNLINKED]
    assert unlinked.note == sm.NOT_LINKED_NOTE
    assert sm.NOT_LINKED_NOTE == "Sessions RelayDeck could not tie to one space"
    assert sm.NOT_LINKED_NOTE in unlinked.a11y
    assert rows["s1"].note is None


# -- archived generations keep their historical label


def test_an_archived_generation_id_shows_its_historical_label() -> None:
    page = doc(
        [
            row("workspace", "kind-a", 2_000_000, space="s2~g0"),
            row("overall", "kind-a", 2_000_000),
        ],
        workspaces=[("s2~g0", "Old project name"), ("s2", "Current name")],
    )
    names = {key: row.name for key, row in _rows(page).items()}
    assert names["s2~g0"] == "Old project name"
