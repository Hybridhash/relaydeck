from pathlib import Path

import pytest

from relaydeck.usage.contracts import Counters, PriceRate
from relaydeck.usage.pricing import PriceTable, PriceTableError, load_price_table
from relaydeck.usage.store import SqliteUsageStore
from tests.support.usage.store import NOW, event, report, write


def pin(path: Path, day: str, effective: str, rate: int) -> Path:
    path.write_text(
        f'[snapshot]\ndate="{day}"\nsource="commit-{day}"\n'
        f'[[rates]]\nmodel="model-a"\neffective_from="{effective}"\ninput={rate}\noutput=2\n'
    )
    return path


def test_snapshot_date_is_independent_and_earlier_pins_retained(tmp_path: Path) -> None:
    first = pin(tmp_path / "first.toml", "2026-10-01", "2026-07-04", 1)
    second = pin(tmp_path / "second.toml", "2026-10-11", "2026-10-11", 3)
    table = load_price_table((first, second))
    assert table.price_table_date == "2026-10-11"
    assert table.estimate("2026-09-01", "model-a", Counters(10)) == (10, "2026-10-01")
    assert table.estimate("2026-10-11", "model-a", Counters(10)) == (30, "2026-10-11")
    assert table.estimate("2026-07-03", "model-a", Counters(10)) == (None, None)
    assert table.estimate("2026-10-11", "model-a", Counters(cache_write_unknown=1)) == (None, None)
    with SqliteUsageStore(tmp_path / "state/u.db", "UTC", prices=table, clock=lambda: NOW) as s:
        write(s, (event(),))
        assert all(r.price_snapshot == "2026-10-11" for r in report(s).rows)
        write(s, (event(model="unknown"),))
        assert all(r.price_snapshot is None for r in report(s).rows)


def test_duplicate_pin_is_rejected(tmp_path: Path) -> None:
    path = pin(tmp_path / "one.toml", "2026-10-11", "2026-10-11", 1)
    with pytest.raises(PriceTableError):
        load_price_table((path, path))


def test_aggregate_mixed_and_unpriced_snapshot_labels(tmp_path: Path) -> None:
    from dataclasses import replace

    class MixedTable(PriceTable):
        def estimate(
            self, day: str, model: str | None, counters: Counters
        ) -> tuple[int | None, str | None]:
            if model is None:
                return None, None
            return 5, "2026-10-01" if counters.input_uncached == 10 else "2026-10-11"

    with SqliteUsageStore(
        tmp_path / "state/u.db", "UTC", prices=MixedTable(), clock=lambda: NOW
    ) as store:
        write(store, (event(), replace(event("second"), counters=Counters(20))))
        assert all(
            r.cost_micro_usd == 10 and r.price_snapshot == "mixed" for r in report(store).rows
        )
        write(store, (replace(event(), model=None),))
        assert next(r for r in report(store).rows if r.model is None).price_snapshot is None


def test_empty_pin_still_reports_actual_snapshot_date(tmp_path: Path) -> None:
    path = tmp_path / "empty.toml"
    path.write_text('rates=[]\n[snapshot]\ndate="2026-10-11"\nsource="synthetic-pin"\n')
    table = load_price_table(path)
    assert table.price_table_date == "2026-10-11"
    assert table.estimate("2026-10-11", "model-a", Counters(10)) == (None, None)


def test_rate_without_snapshot_provenance_stays_tokens_only() -> None:
    table = PriceTable((PriceRate("model-a", "2026-01-01", 1, 2),))
    assert table.estimate("2026-10-11", "model-a", Counters(10)) == (None, None)
