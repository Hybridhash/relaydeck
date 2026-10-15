"""Gateway: the hourly parameter, the known-space inventory with last-used days,
per-kind sources, and the capacity state. Synthetic store and labels only."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from starlette.datastructures import QueryParams

from relaydeck.gateway.usage import UsageService, parse_query
from relaydeck.usage.contracts import Basis, Batch, Counters, Coverage, SessionMapping, UsageEvent
from relaydeck.usage.store import SqliteUsageStore

SOURCE = "h1/kind-a/format-a"
ZONE = "UTC"
NOW = int(
    __import__("datetime").datetime(2026, 10, 11, 23, tzinfo=ZoneInfo(ZONE)).timestamp() * 1000
)


def request(query: str) -> SimpleNamespace:
    return SimpleNamespace(query_params=QueryParams(query))


def parse(query: str) -> tuple:
    return parse_query(request(query), "h1", ZONE, NOW)


def test_hour_bucket_needs_exactly_one_day_and_only_the_value_hour() -> None:
    query, selector = parse("days=1&bucket=hour")
    assert query.hour is True and selector.endswith(":hour")
    query, _ = parse("from=2026-10-11&to=2026-10-11&bucket=hour")
    assert query.hour is True
    for bad in (
        "days=7&bucket=hour",
        "from=2026-10-10&to=2026-10-11&bucket=hour",
        "days=1&bucket=day",
        "days=1&bucket=",
    ):
        with pytest.raises(ValueError):
            parse(bad)


def test_daily_queries_are_unchanged_and_keep_the_daily_binding() -> None:
    query, selector = parse("days=7")
    assert query.hour is False and selector == "days:7"


def _service(tmp_path: Path, labels: dict[str, str]) -> UsageService:
    store = SqliteUsageStore(tmp_path / "state/usage.db", ZONE, clock=lambda: NOW)
    cursor = store.cursor(SOURCE)
    event = UsageEvent(
        "s1", "e1", NOW - 3_600_000, "model-a", Counters(input_uncached=10), Basis.PER_CALL
    )
    store.apply(
        "kind-a",
        Batch((event,), cursor, Coverage.COMPLETE, cursor.revision, ()),
        (SessionMapping(SOURCE, "kind-a", "s1", "i1", "h1", "w1", 0, 2**62),),
        source_key=SOURCE,
    )
    config = SimpleNamespace(sources=(), timezone=ZONE, inferred_linking=True)
    return UsageService(store, config, "h1", tmp_path, lambda: NOW, workspace_labels=lambda: labels)


def test_known_spaces_include_every_inventory_label_with_a_day_only_when_reported(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path, {"w1": "Space One", "w2": "Space Two"})
    body = service._report(parse("days=7")[0], NOW)
    assert body["workspaces"] == [
        {"workspace_id": "w1", "label": "Space One", "last_used_day": "2026-10-11"},
        {
            "workspace_id": "w2",
            "label": "Space Two",
            "last_used_day": None,
        },  # no row: never invented
    ]
    scoped = service._report(parse("days=7&space_id=w2")[0], NOW)
    assert [space["workspace_id"] for space in scoped["workspaces"]] == ["w1", "w2"]
    # The space filter does not erase another space's recency: w1's last day is read before it.
    assert scoped["workspaces"][0]["last_used_day"] == "2026-10-11"
    assert [space["last_used_day"] for space in scoped["workspaces"]] == ["2026-10-11", None]


def test_a_report_over_the_cap_is_flagged_and_has_no_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(tmp_path, {})
    monkeypatch.setattr(
        SqliteUsageStore,
        "report_snapshot",
        lambda self, q, now, **kw: __import__(
            "relaydeck.usage.contracts", fromlist=["UsageReport"]
        ).UsageReport("h1", now, ZONE, (), (), (), capacity="exceeded"),
    )
    body = service._report(parse("days=7")[0], NOW)
    assert body["capacity"] == "exceeded" and not body["rows"]


def test_every_configured_source_of_a_kind_is_listed_with_its_own_format(tmp_path: Path) -> None:
    service = _service(tmp_path, {})
    source_a = SimpleNamespace(
        kind="kind-a",
        format="jsonl-message-usage-v1",
        display_name="Kind A",
        limits_settings=None,
        limits_cache=None,
    )
    source_b = SimpleNamespace(
        kind="kind-a",
        format="jsonl-event-usage-v1",
        display_name="Kind A",
        limits_settings=None,
        limits_cache=None,
    )
    service.config = SimpleNamespace(sources=(source_a, source_b), timezone=ZONE)
    service._checks = {
        (s.kind, s.format): SimpleNamespace(check=lambda now: None) for s in (source_a, source_b)
    }
    service_metadata = service._metadata(NOW)
    assert len(service_metadata) == 1
    entry = service_metadata[0]
    assert [s["format"] for s in entry["sources"]] == [
        "jsonl-message-usage-v1",
        "jsonl-event-usage-v1",
    ]
    assert entry["limits_setup"] is None  # the v1 field is still present
