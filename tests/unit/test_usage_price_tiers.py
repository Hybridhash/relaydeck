import json
from dataclasses import replace
from pathlib import Path

import pytest

from relaydeck.usage.contracts import Counters
from relaydeck.usage.pricing import PriceTable, PriceTableError, load_price_table
from tests.support.usage.converter import convert

FIXTURE = Path(__file__).parents[1] / "fixtures/usage/catalog-shapes.json"
LABEL = "standard-tier token-only API-equivalent estimate"


def table(tmp_path: Path) -> PriceTable:
    result = convert(tmp_path, json.loads(FIXTURE.read_text()))
    assert result.returncode == 0 and "converted=4 skipped=0" in result.stderr
    path = tmp_path / "prices.toml"
    path.write_text(result.stdout)
    return load_price_table(path)


def test_representative_shapes_and_label(tmp_path: Path) -> None:
    prices = table(tmp_path)
    assert prices.snapshot_labels == (("2026-10-11", LABEL),)
    assert {r.model for r in prices.rates} == {"model-a", "model-b", "model-c", "model-d"}
    row = next(r for r in prices.rates if r.model == "model-b")
    assert row.tier_threshold_tokens == 100000
    assert row.cache_write_long_above_threshold == 10
    assert next(r for r in prices.rates if r.model == "model-c").cache_write_short is None


@pytest.mark.parametrize("prompt,multiplier", [(99999, 1), (100000, 1), (100001, 2)])
def test_below_equal_above_strict_boundary(tmp_path: Path, prompt: int, multiplier: int) -> None:
    prices = table(tmp_path)
    assert prices.estimate("2026-10-11", "model-b", Counters(prompt, output=5, reasoning=3)) == (
        multiplier * (prompt * 2 + 20),
        "2026-10-11",
    )
    # Output alone never moves a request into the higher prompt tier.
    assert prices.cost_micro_usd("2026-10-11", "model-b", Counters(1, output=200001)) == 800006


def test_mixed_cache_prompt_size_and_missing_rates(tmp_path: Path) -> None:
    prices = table(tmp_path)
    counts = Counters(10000, 80000, 9000, 1001, 5, 3)
    assert counts.total == 100006
    assert prices.estimate("2026-10-11", "model-b", counts) == (184050, "2026-10-11")
    assert prices.estimate("2026-10-11", "model-c", Counters(10, cache_write_short=1)) == (
        None,
        None,
    )
    assert prices.estimate("2026-10-11", "model-b", Counters(cache_write_unknown=100001)) == (
        None,
        None,
    )
    # No fallback to a cheaper base cache rate above the boundary.
    changed = tuple(
        replace(r, cache_read_above_threshold=None) if r.model == "model-b" else r
        for r in prices.rates
    )
    assert replace(prices, rates=changed).estimate("2026-10-11", "model-b", counts) == (None, None)


@pytest.mark.parametrize(
    "bad",
    [
        {"input_cost_per_token_above_100k_tokens": 0.1},
        {
            "input_cost_per_token_above_100k_tokens": 0.1,
            "output_cost_per_token_above_200k_tokens": 0.1,
        },
        {"input_cost_per_token_above_100001_tokens": 0.1},
        {
            "input_cost_per_token_above_100k_tokens": True,
            "output_cost_per_token_above_100k_tokens": 0.1,
        },
        {"input_cost_per_audio_token_batches": 0.1},
        {"input_cost_per_character": 0.1},
    ],
)
def test_unsupported_or_incomplete_tiers_fail_closed(
    tmp_path: Path, bad: dict[str, object]
) -> None:
    result = convert(
        tmp_path,
        {
            "model-a": {
                "input_cost_per_token": 0.000001,
                "output_cost_per_token": 0.000002,
                **bad,
            }
        },
    )
    assert result.returncode == 0 and "converted=0 skipped=1" in result.stderr


@pytest.mark.parametrize(
    "suffix",
    [
        "tier_threshold_tokens=0",
        "tier_threshold_tokens=true",
        "tier_threshold_tokens=10.5",
        "input_above_threshold=2",
        "tier_threshold_tokens=100000\ninput_above_threshold=2",
        "tier_threshold_tokens=100000\ninput_above_threshold=2\noutput_above_threshold=-1",
    ],
)
def test_invalid_toml_tiers_rejected(tmp_path: Path, suffix: str) -> None:
    path = tmp_path / "bad.toml"
    path.write_text(
        '[snapshot]\ndate="2026-10-11"\nsource="synthetic"\n'
        '[[rates]]\nmodel="model-a"\neffective_from="2026-10-11"\ninput=1\noutput=2\n' + suffix
    )
    with pytest.raises(PriceTableError):
        load_price_table(path)


def test_historical_flat_pin_and_per_event_store_pricing(tmp_path: Path) -> None:
    from relaydeck.usage.store import SqliteUsageStore
    from tests.support.usage.store import NOW, event, report, write

    current = table(tmp_path)
    path = tmp_path / "old.toml"
    path.write_text(
        '[snapshot]\ndate="2026-10-01"\nsource="old-pin"\n'
        '[[rates]]\nmodel="model-b"\neffective_from="2026-07-04"\ninput=1\noutput=2\n'
    )
    prices = load_price_table((path, tmp_path / "prices.toml"))
    assert prices.estimate("2026-10-10", "model-b", Counters(200001)) == (200001, "2026-10-01")
    assert prices.estimate("2026-10-11", "model-b", Counters(200001)) == current.estimate(
        "2026-10-11", "model-b", Counters(200001)
    )
    with SqliteUsageStore(
        tmp_path / "state/u.db", "UTC", prices=prices, clock=lambda: NOW
    ) as store:
        write(
            store,
            (
                replace(event(model="model-b"), counters=Counters(60000)),
                replace(event("second", model="model-b"), counters=Counters(60000)),
            ),
        )
        assert all(r.cost_micro_usd == 240000 for r in report(store).rows)


@pytest.mark.parametrize("provider", ["anthropic", "Anthropic", "anthropic.example", None])
def test_cache_ttl_requires_exact_documented_provider(tmp_path: Path, provider: str | None) -> None:
    result = convert(
        tmp_path,
        {
            "model-a": {
                "litellm_provider": provider,
                "input_cost_per_token": 0.000001,
                "output_cost_per_token": 0.000002,
                "cache_creation_input_token_cost": 0.000003,
                "cache_creation_input_token_cost_above_1hr": 0.000004,
            }
        },
    )
    path = tmp_path / "prices.toml"
    path.write_text(result.stdout)
    prices = load_price_table(path)
    expected = (7, "2026-10-11") if provider == "anthropic" else (None, None)
    assert (
        prices.estimate("2026-10-11", "model-a", Counters(cache_write_short=1, cache_write_long=1))
        == expected
    )
