import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from tests.support.usage.converter import SCRIPT, convert


def test_converter_exact_keys_known_ttl_first_pin_aliases_and_notice(tmp_path: Path) -> None:
    models = {
        "provider/model-a-20261001": {
            "litellm_provider": "anthropic",
            "input_cost_per_token": 0.000002,
            "output_cost_per_token": 0.000004,
            "cache_read_input_token_cost": 0.0000005,
            "cache_creation_input_token_cost": 0.000003,
            "cache_creation_input_token_cost_above_1hr": 0.000005,
        }
    }
    alias = tmp_path / "aliases.toml"
    alias.write_text('[aliases]\n"audited-alias"="provider/model-a-20261001"\n')
    first = convert(tmp_path, models, "--first-pin", "--aliases", str(alias))
    assert first.returncode == 0
    assert first.stdout == convert(tmp_path, models, "--first-pin", "--aliases", str(alias)).stdout
    assert "MIT License" in first.stdout and "Copyright (c) 2023 Berri AI" in first.stdout
    assert 'THE SOFTWARE IS PROVIDED "AS IS"' in first.stdout
    value = tomllib.loads(first.stdout)
    assert value["snapshot"] == {
        "date": "2026-10-11",
        "source": "a" * 40,
        "label": "standard-tier token-only API-equivalent estimate",
    }
    assert {r["model"] for r in value["rates"]} == {"provider/model-a-20261001", "audited-alias"}
    for row in value["rates"]:
        assert row["effective_from"] == "2026-07-14"
        assert (row["input"], row["output"], row["cache_read"], row["cache_write_long"]) == (
            2,
            4,
            0.5,
            5,
        )
        assert row["cache_write_short"] == 3
    later = tomllib.loads(convert(tmp_path, models).stdout)
    assert later["rates"][0]["effective_from"] == "2026-10-11"


def test_converter_skips_tiered_modal_and_unsupported_counts(tmp_path: Path) -> None:
    good = {"input_cost_per_token": 0.000001, "output_cost_per_token": 0.000002}
    value = {
        "model-a": good,
        "tiered": {**good, "input_cost_per_token_above_200k_tokens": 0.1},
        "modal": {**good, "input_cost_per_audio_token": 0.1},
        "unsupported": {"input_cost_per_character": 0.1},
        "bad": {**good, "input_cost_per_token": True},
    }
    result = convert(tmp_path, value)
    assert result.returncode == 0 and "skipped=4" in result.stderr
    assert [r["model"] for r in tomllib.loads(result.stdout)["rates"]] == ["model-a"]


@pytest.mark.parametrize("value", [[], {"model-a": []}, {"bad\nkey": {}}, {"model-a": 1}])
def test_converter_rejects_invalid_shape_without_contents(tmp_path: Path, value: object) -> None:
    result = convert(tmp_path, value)
    assert result.returncode == 2 and result.stderr.strip() == "invalid_price_input"
    assert not result.stdout


def test_converter_size_alias_and_duplicate_json_rejected(tmp_path: Path) -> None:
    alias = tmp_path / "aliases.toml"
    alias.write_text('[aliases]\nwrong="absent"\n')
    result = convert(tmp_path, {}, "--aliases", str(alias))
    assert result.returncode == 2 and result.stderr.strip() == "invalid_price_input"
    source = tmp_path / "input.json"
    command = [
        sys.executable,
        "-B",
        str(SCRIPT),
        str(source),
        "--snapshot-date",
        "2026-10-11",
        "--source-commit",
        "a" * 40,
    ]
    for raw in (b"x" * (16 * 1024 * 1024 + 1), b'{"same":{},"same":{}}'):
        source.write_bytes(raw)
        result = subprocess.run(command, text=True, capture_output=True, timeout=5)
        assert result.returncode == 2 and result.stderr.strip() == "invalid_price_input"


def test_converter_unicode_exact_model_round_trips_through_loader(tmp_path: Path) -> None:
    from relaydeck.usage.contracts import Counters
    from relaydeck.usage.pricing import load_price_table

    model = "model-\U0001f680"
    result = convert(
        tmp_path, {model: {"input_cost_per_token": 0.000001, "output_cost_per_token": 0.000002}}
    )
    assert result.returncode == 0
    path = tmp_path / "prices.toml"
    path.write_text(result.stdout)
    table = load_price_table(path)
    assert table.estimate("2026-10-11", model, Counters(10)) == (10, "2026-10-11")
