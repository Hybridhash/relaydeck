"""Pricing: only genuinely empty counters cost 0 (no rate lookup, no snapshot).
Any nonzero count with an unknown price stays None. Synthetic rates only."""

from __future__ import annotations

from relaydeck.usage.contracts import Counters, PriceRate
from relaydeck.usage.pricing import PriceTable


def table() -> PriceTable:
    return PriceTable(
        rates=(PriceRate("model-a", "2026-01-01", 1.0, 2.0),),
        provenance=("2026-10-01",),
        snapshot_sources=(("2026-10-01", "synthetic"),),
    )


def test_empty_counters_cost_zero_without_a_rate_or_a_snapshot() -> None:
    assert PriceTable().estimate("2026-10-11", None, Counters()) == (0, None)
    assert table().estimate("2026-10-11", "model-a", Counters()) == (0, None)
    assert table().estimate("2026-10-11", "unknown-model", Counters()) == (0, None)


def test_nonzero_counters_with_an_unknown_price_stay_null() -> None:
    assert table().estimate("2026-10-11", "unknown-model", Counters(input_uncached=1)) == (
        None,
        None,
    )
    assert table().estimate("2026-10-11", "model-a", Counters(cache_write_unknown=1)) == (
        None,
        None,
    )
    assert table().estimate("2026-10-11", "model-a", Counters(cache_read=1)) == (None, None)


def test_a_priced_nonzero_event_keeps_its_snapshot() -> None:
    assert table().estimate("2026-10-11", "model-a", Counters(input_uncached=1_000_000)) == (
        1_000_000,
        "2026-10-01",
    )
