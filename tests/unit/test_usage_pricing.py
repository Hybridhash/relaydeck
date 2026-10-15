from pathlib import Path

import pytest

from relaydeck.usage.contracts import Counters
from relaydeck.usage.pricing import PriceTable, PriceTableError, load_price_table


def test_dated_prices_and_disjoint_buckets() -> None:
    table = load_price_table(Path(__file__).parents[1] / "fixtures/usage/prices.toml")
    assert table.price_table_date == "2026-10-01"
    assert table.cost_micro_usd("2026-09-30", "model-a", Counters(10, 4, 2, 1, 5, 3)) == 53
    assert table.cost_micro_usd("2026-10-01", "model-a", Counters(10, output=5)) == 60
    assert table.cost_micro_usd("2025-12-31", "model-a", Counters(1)) is None
    assert table.cost_micro_usd("2026-10-01", "model-b", Counters(1)) is None
    assert table.cost_micro_usd("2026-10-01", "model-a", Counters(cache_read=1)) is None
    assert table.cost_micro_usd("2026-01-01", "model-a", Counters(cache_write_unknown=1)) is None
    # A genuinely empty event costs 0 without any rate lookup.
    assert PriceTable().cost_micro_usd("2026-10-01", "model-a", Counters()) == 0


@pytest.mark.parametrize(
    "bad",
    [
        "input = -1",
        "input = inf",
        "input = nan",
        "input = true",
        'effective_from = "2026-02-30"',
        'effective_from = "20260101"',
        'model = ""',
        "cache_read = -1",
        'extra = "private-marker"',
    ],
)
def test_invalid_table_rejected_as_a_whole(tmp_path: Path, bad: str) -> None:
    values = {"model": '"model-a"', "effective_from": '"2026-01-01"', "input": "2", "output": "4"}
    key, value = bad.split(" = ", 1)
    values[key] = value
    path = tmp_path / "prices.toml"
    path.write_text(
        '[snapshot]\ndate="2026-10-11"\nsource="synthetic-pin"\n'
        "[[rates]]\n" + "\n".join(f"{k} = {v}" for k, v in values.items())
    )
    with pytest.raises(PriceTableError, match="^invalid_price_table$"):
        load_price_table(path)


def test_duplicate_rate_and_malformed_file_are_content_free(tmp_path: Path) -> None:
    path = tmp_path / "private-marker"
    row = '[[rates]]\nmodel="model-a"\neffective_from="2026-01-01"\ninput=1\noutput=1\n'
    for text in [row + row, "private-marker ???"]:
        path.write_text('[snapshot]\ndate="2026-10-11"\nsource="synthetic-pin"\n' + text)
        with pytest.raises(PriceTableError, match="^invalid_price_table$"):
            load_price_table(path)


def test_toml_date_and_micro_unit_rounding(tmp_path: Path) -> None:
    path = tmp_path / "prices.toml"
    path.write_text(
        '[snapshot]\ndate="2026-01-01"\nsource="synthetic-pin"\n'
        '[[rates]]\nmodel="model-a"\neffective_from=2026-01-01\ninput=0.1\noutput=1'
    )
    prices = load_price_table(path)
    assert prices.cost_micro_usd("2026-01-01", "model-a", Counters(15)) == 2
    assert prices.price_table_date == "2026-01-01"
