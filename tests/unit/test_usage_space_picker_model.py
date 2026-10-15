"""pins, the Space picker's arrangement and the Providers status lines.

Synthetic data only. Pins use a fake storage pair, never the device's storage.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, date, datetime
from typing import Any

import pytest

from relaydeck.client.features.usage import model
from relaydeck.client.features.usage import pins as pin_store
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


def row(scope: str, kind: str, tokens: int, *, space: str | None = None) -> dict[str, Any]:
    return {
        "day": TODAY.isoformat(),
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


def doc(
    rows: list[dict[str, Any]],
    *,
    kinds: list[dict[str, Any]] | None = None,
    workspaces: list[tuple[str, str]] | None = None,
    limits: list[dict[str, Any]] | None = None,
    coverage: list[dict[str, Any]] | None = None,
    capacity: str = "ok",
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
            "kinds": kinds if kinds is not None else [kind("kind-a", display="Alpha Agent")],
            "workspaces": [
                {"workspace_id": key, "label": label, "last_used_day": TODAY.isoformat()}
                for key, label in (workspaces or [])
            ],
            "capacity": capacity,
        }
    )


def build(
    page: model.UsagePage | None, *, state: str = sm.STATE_READY, **kwargs: Any
) -> sm.ScreenModel:
    return sm.build(
        page,
        state=state,
        flt=kwargs.pop("flt", s.Filter("7d")),
        now_ms=NOW_MS,
        width=kwargs.pop("width", sm.WIDTH_WIDE),
        **kwargs,
    )


def space(key: str, tokens: int, *, name: str | None = None) -> sm.SpaceRow:
    return sm.SpaceRow(
        key=key,
        name=name or key,
        tokens=tokens,
        tokens_text=s.tokens_text(tokens),
        unlinked=key == sm.NOT_LINKED_ID,
        zero=tokens == 0,
    )


# -- pins: keys, the bound and the storage adapter -------------------------------------------------


def test_the_pin_key_is_opaque_and_differs_by_account_and_host() -> None:
    key = pin_store.pin_key("account-secret-id", "host-secret-name")
    assert key.startswith(pin_store.KEY_PREFIX)
    assert "account-secret-id" not in key and "host-secret-name" not in key
    assert pin_store.pin_key("account-secret-id", "host-secret-name") == key
    assert pin_store.pin_key("account-other", "host-secret-name") != key
    assert pin_store.pin_key("account-secret-id", "host-other") != key


def test_stored_pins_are_cleaned_tolerantly_and_bounded_to_fifty() -> None:
    raw = ["a", "a", "", 7, None, pin_store.NOT_LINKED_ID, "b"] + [f"s{i}" for i in range(80)]
    cleaned = pin_store.clean(raw)
    assert cleaned[:2] == ("a", "b")
    assert pin_store.NOT_LINKED_ID not in cleaned
    assert len(cleaned) == pin_store.MAX_PINS
    assert pin_store.clean("not a list") == ()


def test_toggle_pins_and_unpins_and_refuses_not_linked_and_a_full_list() -> None:
    pins = pin_store.toggle((), "space-1")
    assert pins == ("space-1",)
    assert pin_store.toggle(pins, "space-1") == ()
    assert pin_store.toggle(pins, pin_store.NOT_LINKED_ID) == pins
    full = tuple(f"s{i}" for i in range(pin_store.MAX_PINS))
    assert pin_store.toggle(full, "extra") == full


def test_pins_round_trip_through_the_stored_json() -> None:
    assert pin_store.clean(json.loads(pin_store.encode(("b", "a")))) == ("b", "a")


class FakeStore:
    def __init__(self, value: Any = None, *, fail: bool = False) -> None:
        self.value = value
        self.fail = fail
        self.written: list[tuple[str, str]] = []

    async def get(self, key: str) -> Any:
        if self.fail:
            raise RuntimeError("storage unavailable")
        return self.value

    async def set(self, key: str, value: str) -> None:
        if self.fail:
            raise RuntimeError("storage unavailable")
        self.written.append((key, value))


def test_pins_load_from_storage_and_missing_or_corrupt_storage_gives_none() -> None:
    store = FakeStore(json.dumps(["x", "y"]))
    assert asyncio.run(pin_store.load(store.get, "k")) == ("x", "y")
    assert asyncio.run(pin_store.load(FakeStore("not json").get, "k")) == ()
    assert asyncio.run(pin_store.load(FakeStore(None).get, "k")) == ()
    assert asyncio.run(pin_store.load(FakeStore(fail=True).get, "k")) == ()


def test_pins_save_to_the_key_and_unavailable_storage_keeps_them_in_memory() -> None:
    store = FakeStore()
    assert asyncio.run(pin_store.save(store.set, "k", ("x",))) is True
    assert store.written == [("k", json.dumps(["x"]))]
    assert asyncio.run(pin_store.save(FakeStore(fail=True).set, "k", ("x",))) is False


# -- the Space picker's arrangement
# -----------------------------------------------------------------


def test_pinned_spaces_come_first_in_pin_order_and_the_all_list_sorts_by_tokens() -> None:
    rows = [space("a", 10), space("b", 50), space("c", 30), space("d", 0)]
    arranged = sm.arrange_spaces(rows, query="", sort=sm.SORT_MOST, pins=("c", "a"))
    assert [row.key for row in arranged.pinned] == ["c", "a"]
    assert [row.key for row in arranged.rest] == ["b", "c", "a", "d"]  # zero-usage last
    assert arranged.results is None


def test_a_zero_usage_space_sorts_last_in_both_orders() -> None:
    rows = [space("zeta", 0), space("alpha", 5)]
    by_name = sm.arrange_spaces(rows, query="", sort=sm.SORT_AZ, pins=())
    assert [row.key for row in by_name.rest] == ["alpha", "zeta"]
    assert sm.arrange_spaces(rows, query="", sort=sm.SORT_MOST, pins=()).rest[-1].key == "zeta"


def test_not_linked_is_never_pinned_even_if_its_id_is_stored() -> None:
    rows = [space("a", 10), space(sm.NOT_LINKED_ID, 3, name="Not linked")]
    arranged = sm.arrange_spaces(rows, query="", sort=sm.SORT_MOST, pins=(sm.NOT_LINKED_ID,))
    assert arranged.pinned == ()
    assert any(row.unlinked for row in arranged.rest)


def test_search_puts_substring_matches_before_fuzzy_ones() -> None:
    rows = [
        space("1", 1, name="deck-ally"),
        space("2", 1, name="docs-deck"),
        space("3", 1, name="d-e-l-a-y-x"),
    ]
    arranged = sm.arrange_spaces(rows, query="deck", sort=sm.SORT_MOST, pins=())
    assert arranged.results is not None
    assert [row.key for row in arranged.results] == ["1", "2"]
    fuzzy = sm.arrange_spaces(rows, query="dly", sort=sm.SORT_MOST, pins=())
    assert fuzzy.results is not None and {row.key for row in fuzzy.results} == {"1", "3"}


def test_a_search_with_no_match_gives_an_empty_result_not_the_sections() -> None:
    arranged = sm.arrange_spaces(
        [space("a", 1, name="alpha")], query="zzz", sort=sm.SORT_MOST, pins=()
    )
    assert arranged.results == ()
    assert arranged.pinned == () and arranged.rest == ()


def test_five_hundred_spaces_arrange_completely() -> None:
    rows = [space(f"s{index}", index) for index in range(500)]
    arranged = sm.arrange_spaces(rows, query="", sort=sm.SORT_MOST, pins=("s3",))
    assert len(arranged.rest) == 500 and arranged.rest[0].key == "s499"
    assert len(arranged.pinned) == 1


def test_the_model_offers_every_space_with_its_period_total_and_not_linked_always() -> None:
    page = doc(
        [row("workspace", "kind-a", 2_000_000, space="s1"), row("overall", "kind-a", 2_000_000)],
        workspaces=[("s1", "Space Alpha"), ("s2", "docs-site")],
    )
    result = build(page, flt=s.Filter("7d", space="s1"))
    keys = {entry.key: entry for entry in result.space_rows}
    assert set(keys) == {"s1", "s2", sm.NOT_LINKED_ID}
    assert keys["s2"].zero and keys["s2"].tokens == 0
    assert keys[sm.NOT_LINKED_ID].unlinked is True


# -- the Providers screen: one status line per CLI
# --------------------------------------------------


def test_a_setup_problem_reads_the_plan_line_and_offers_set_up() -> None:
    page = doc(
        [row("overall", "kind-a", 1_000_000)],
        kinds=[kind("kind-a", setup="script_missing", display="Alpha Agent")],
        limits=[
            {
                "kind": "kind-a",
                "quota_key": "q",
                "observed_ms": NOW_MS - 120_000,
                "status": "fresh",
                "windows": [],
            }
        ],
    )
    [line] = build(page).connected
    plan = build(page).plan[0]
    assert line.status == plan.subline and line.status.startswith("Setup needed")
    assert line.warn is True and line.action == "setup"


def test_an_honest_token_status_shows_when_no_limit_problem_exists() -> None:
    page = doc(
        [row("overall", "kind-c", 1_000_000)],
        kinds=[kind("kind-c", reported=False, display="Gamma Agent")],
    )
    [line] = build(page).connected
    assert line.status == "Tokens not reported"
    assert line.action is None and line.warn is False


def test_a_connected_cli_with_a_fresh_reading_shows_the_plan_line_as_is() -> None:
    page = doc(
        [row("overall", "kind-a", 1_000_000)],
        kinds=[kind("kind-a", display="Alpha Agent")],
        limits=[
            {
                "kind": "kind-a",
                "quota_key": "q",
                "observed_ms": NOW_MS - 60_000,
                "status": "fresh",
                "windows": [],
            }
        ],
    )
    result = build(page)
    [line] = result.connected
    assert line.status == result.plan[0].subline and line.action is None


def test_over_capacity_the_space_rows_and_provider_lines_are_unknown_not_zero() -> None:
    inventory = doc(
        [],
        kinds=[kind("kind-a", display="Alpha Agent")],
        workspaces=[("s1", "Space Alpha")],
        capacity="exceeded",
    )
    result = sm.build(
        None,
        state=sm.STATE_CAPACITY,
        flt=s.Filter("7d"),
        now_ms=NOW_MS,
        width=sm.WIDTH_WIDE,
        inventory=inventory,
    )
    assert all(entry.tokens is None and entry.tokens_text == "—" for entry in result.space_rows)
    assert any(entry.key == sm.NOT_LINKED_ID for entry in result.space_rows)
    assert [line.name for line in result.connected] == ["Alpha Agent"]
    assert all(line.action is None for line in result.connected)


# -- the Recent sort: last_used_day is parsed tolerantly and never invented
# --------------------------


def page_with_days(days: dict[str, Any]) -> model.UsagePage:
    """A page whose workspaces carry `last_used_day` values as given (any JSON value)."""
    workspaces = []
    for key, value in days.items():
        entry: dict[str, Any] = {"workspace_id": key, "label": key}
        if value is not _ABSENT:
            entry["last_used_day"] = value
        workspaces.append(entry)
    doc_value = {
        "schema_version": 1,
        "host_id": "h1",
        "generated_ms": NOW_MS,
        "timezone": "UTC",
        "price_table_date": "2026-10-01",
        "next_cursor": None,
        "rows": [],
        "limits": [],
        "coverage": [],
        "snapshot_labels": [],
        "kinds": [kind("kind-a")],
        "workspaces": workspaces,
        "capacity": "ok",
    }
    return model.parse_usage_page(doc_value)


_ABSENT = object()


@pytest.mark.parametrize(
    "value",
    [None, "not-a-date", "2026-13-40", 20261012, ["2026-10-12"], {"day": "x"}, _ABSENT],
    ids=["null", "text", "impossible-date", "number", "list", "object", "absent"],
)
def test_an_invalid_or_missing_last_used_day_is_unknown_and_the_page_still_parses(
    value: Any,
) -> None:
    page = page_with_days({"space": value})
    assert dict(page.workspace_days)["space"] is None


def test_a_valid_last_used_day_is_kept_as_its_calendar_day() -> None:
    page = page_with_days({"space-a": "2026-10-11"})
    assert dict(page.workspace_days)["space-a"] == "2026-10-11"


def test_recent_sorts_newest_first_then_unknown_days_then_zero_usage_last() -> None:
    rows = [
        sm.SpaceRow("old", "Old", 5, "5", False, False, "2026-10-01"),
        sm.SpaceRow("new", "New", 1, "1", False, False, "2026-10-12"),
        sm.SpaceRow("unknown", "Unknown", 9, "9", False, False, None),
        sm.SpaceRow("idle", "Idle", 0, "0", False, True, "2026-10-12"),
    ]
    arranged = sm.arrange_spaces(rows, query="", sort=sm.SORT_RECENT, pins=())
    assert [row.key for row in arranged.rest] == ["new", "old", "unknown", "idle"]


def test_the_sort_cycle_is_most_tokens_then_az_then_recent() -> None:
    from relaydeck.client.features.usage.components import space_sheet

    assert space_sheet.NEXT_SORT == {
        sm.SORT_MOST: sm.SORT_AZ,
        sm.SORT_AZ: sm.SORT_RECENT,
        sm.SORT_RECENT: sm.SORT_MOST,
    }
    assert space_sheet.SORT_LABEL[sm.SORT_RECENT] == "Recent"
