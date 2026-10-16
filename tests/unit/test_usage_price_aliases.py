"""A logged model may be priced through an explicit alias, never through a guess."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from relaydeck.usage.contracts import Counters
from relaydeck.usage.pricing import PriceTable, load_price_table
from relaydeck.usage.store import SqliteUsageStore
from tests.support.usage.store import event, report, write

PIN = """
[snapshot]
date = "2026-10-01"
source = "test pin"

[[rates]]
model = "vendor/tool-1.3"
effective_from = "2026-01-01"
input = 2.0
output = 4.0

[[rates]]
model = "tool-2.0"
effective_from = "2026-01-01"
input = 1.0
output = 1.0
"""
USAGE = Counters(10, output=5)  # 10 * 2.0 + 5 * 4.0 at the aliased rate


def _pin(tmp_path: Path, text: str = PIN) -> Path:
    path = tmp_path / "prices.toml"
    path.write_text(text)
    return path


def test_a_packaged_alias_prices_a_model_with_a_provider_prefixed_rate(tmp_path: Path) -> None:
    table = load_price_table(_pin(tmp_path, PIN.replace("vendor/tool-1.3", "meta/muse-spark-1.3")))
    assert ("muse-spark-1.3", "meta/muse-spark-1.3") in table.aliases
    assert table.cost_micro_usd("2026-10-05", "muse-spark-1.3", USAGE) == 40
    assert table.cost_micro_usd("2026-10-05", "meta/muse-spark-1.3", USAGE) == 40


def test_an_alias_whose_target_is_not_priced_is_dropped(tmp_path: Path) -> None:
    table = load_price_table(_pin(tmp_path))  # the pin has no muse rates at all
    assert table.aliases == ()
    assert table.cost_micro_usd("2026-10-05", "muse-spark-1.3", USAGE) is None


def test_an_unknown_model_stays_unpriced_never_zero_or_guessed(tmp_path: Path) -> None:
    (tmp_path / "aliases.toml").write_text('[aliases]\n"known" = "vendor/tool-1.3"\n')
    table = load_price_table(_pin(tmp_path))
    assert table.cost_micro_usd("2026-10-05", "known", USAGE) == 40
    for unknown in ("tool-1.3", "vendor/tool-1.3-x", "other"):
        assert table.cost_micro_usd("2026-10-05", unknown, USAGE) is None


def test_a_user_alias_file_beside_the_pin_extends_the_packaged_set(tmp_path: Path) -> None:
    (tmp_path / "aliases.toml").write_text('[aliases]\n"tool-1.3" = "vendor/tool-1.3"\n')
    table = load_price_table(_pin(tmp_path))
    assert ("tool-1.3", "vendor/tool-1.3") in table.aliases


def test_a_malformed_user_alias_file_contributes_nothing(tmp_path: Path) -> None:
    (tmp_path / "aliases.toml").write_text("not [valid toml")
    table = load_price_table(_pin(tmp_path))
    assert table.cost_micro_usd("2026-10-05", "tool-2.0", USAGE) == 15


def test_a_model_with_its_own_rate_is_never_aliased(tmp_path: Path) -> None:
    (tmp_path / "aliases.toml").write_text('[aliases]\n"tool-2.0" = "vendor/tool-1.3"\n')
    table = load_price_table(_pin(tmp_path))
    assert table.aliases == ()
    assert table.cost_micro_usd("2026-10-05", "tool-2.0", USAGE) == 15


def test_existing_unpriced_rows_are_recosted_when_the_alias_set_changes(tmp_path: Path) -> None:
    (tmp_path / "aliases.toml").write_text('[aliases]\n"tool-1.3" = "vendor/tool-1.3"\n')
    with_alias = load_price_table(_pin(tmp_path))
    without = replace(with_alias, aliases=())
    db = tmp_path / "usage.db"
    with SqliteUsageStore(db, "UTC", prices=without) as store:
        write(store, (event(model="tool-1.3"),))  # 10 * 2.0 + 2 * 4.0 once aliased
        assert all(row.cost_micro_usd is None for row in report(store).rows)
    with SqliteUsageStore(db, "UTC", prices=with_alias) as store:
        done, remaining = store.backfill_prices()
        assert done >= 1 and remaining == 0
        assert all(row.cost_micro_usd == 28 for row in report(store).rows)
    assert PriceTable().aliases == ()
