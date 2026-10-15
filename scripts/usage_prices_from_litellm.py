"""Convert a local, pinned pricing JSON to dated TOML; no network or inferred aliases."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
import tomllib
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

MAX_BYTES = 16 * 1024 * 1024
PRICE_FIELDS = {
    "input_cost_per_token": "input",
    "output_cost_per_token": "output",
    "cache_read_input_token_cost": "cache_read",
}
CACHE_FIELDS = {
    "cache_creation_input_token_cost": "cache_write_short",
    "cache_creation_input_token_cost_above_1hr": "cache_write_long",
}
ESTIMATE_LABEL = "standard-tier token-only API-equivalent estimate"
SERVICE_TIERS = re.compile(r"_(batches|flex|priority|ultrafast)$")
PROMPT_TIER = re.compile(r"(.+)_above_([1-9][0-9]*)k_tokens$")
MIT_NOTICE = """MIT License

Copyright (c) 2023 Berri AI
Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:
The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.
THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE."""


def unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def key_ok(key: object) -> bool:
    return (
        isinstance(key, str)
        and 0 < len(key) <= 128
        and all(ord(c) >= 32 and ord(c) != 127 for c in key)
    )


def bounded(path: Path, bound: int) -> bytes:
    with path.open("rb") as stream:
        data = stream.read(bound + 1)
    if len(data) > bound:
        raise ValueError
    return data


def rates_for(row: dict[str, Any]) -> dict[str, Decimal] | None:
    if row.get("mode", "chat") not in ("chat", "completion"):
        return None
    if not {"input_cost_per_token", "output_cost_per_token"} <= row.keys():
        return None
    result = {}
    thresholds: set[int] = set()
    for field, value in row.items():
        if "cost" not in field or field == "search_context_cost_per_query":
            continue
        standard = SERVICE_TIERS.sub("", field)
        tier = PROMPT_TIER.fullmatch(standard)
        base = tier[1] if tier else standard
        if base not in PRICE_FIELDS and base not in CACHE_FIELDS:
            return None  # unsupported modalities, ambiguous units or tier shapes
        if standard != field:
            continue  # allowlisted alternate service tier, not a standard token charge
        threshold = int(tier[2]) * 1000 if tier else None
        if threshold is not None:
            if threshold > 2**63 - 1:
                return None
            thresholds.add(threshold)
        try:
            if type(value) not in (int, float) or value < 0 or not math.isfinite(value):
                return None
            rate = Decimal(str(value)) * 1_000_000
            if not math.isfinite(float(rate)):
                return None
        except OverflowError:
            return None
        if base in CACHE_FIELDS:
            # Exact provider evidence establishes five-minute/default and one-hour writes.
            # Other providers' creation fields do not establish these TTL semantics.
            if row.get("litellm_provider") != "anthropic":
                continue
            bucket = CACHE_FIELDS[base]
        else:
            bucket = PRICE_FIELDS[base]
        result[bucket + ("_above_threshold" if tier else "")] = rate
    if len(thresholds) > 1:
        return None
    if thresholds:
        if not {"input_above_threshold", "output_above_threshold"} <= result.keys():
            return None
        result["tier_threshold_tokens"] = Decimal(next(iter(thresholds)))
    return result


def convert(
    path: Path,
    pin: str,
    commit: str,
    retention: int,
    first: bool,
    aliases: Path | None,
) -> tuple[str, int, int, int]:
    day = date.fromisoformat(pin)
    if day.isoformat() != pin or not re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", commit):
        raise ValueError
    if not 1 <= retention <= 90:
        raise ValueError
    data = bounded(path, MAX_BYTES)
    document = json.loads(data, object_pairs_hook=unique_pairs)
    if not isinstance(document, dict) or any(
        not key_ok(k) or not isinstance(v, dict) for k, v in document.items()
    ):
        raise ValueError
    rates: dict[str, dict[str, Decimal]] = {}
    skipped = 0
    for model, row in document.items():
        value = rates_for(row)
        if value is None:
            skipped += 1
        else:
            rates[model] = value
    alias_count = 0
    if aliases is not None:
        mapping = tomllib.loads(bounded(aliases, 1024 * 1024).decode())
        if set(mapping) != {"aliases"} or not isinstance(mapping["aliases"], dict):
            raise ValueError
        originals = dict(rates)
        for alias, target in mapping["aliases"].items():
            if not key_ok(alias) or not key_ok(target) or target not in originals or alias in rates:
                raise ValueError
            rates[alias] = originals[target]
            alias_count += 1
    effective = (day - timedelta(days=retention - 1)).isoformat() if first else pin
    lines = [f"# {line}" for line in MIT_NOTICE.splitlines()]
    lines += [
        f"# Input SHA-256: {hashlib.sha256(data).hexdigest()}",
        "# Cache-write rates omitted unless the exact provider establishes their TTL.",
    ]
    if not rates:
        lines.append("rates = []")
    lines += [
        "",
        "[snapshot]",
        f"date = {json.dumps(pin)}",
        f"source = {json.dumps(commit)}",
        f"label = {json.dumps(ESTIMATE_LABEL)}",
    ]
    for model, prices in sorted(rates.items()):
        lines += [
            "",
            "[[rates]]",
            f"model = {json.dumps(model, ensure_ascii=False)}",
            f"effective_from = {json.dumps(effective)}",
        ]
        for field, price in sorted(prices.items()):
            lines.append(f"{field} = {format(price, 'f')}")
    return "\n".join(lines) + "\n", len(rates), skipped, alias_count


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("--snapshot-date", required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--retention-days", type=int, default=90)
    parser.add_argument("--first-pin", action="store_true")
    parser.add_argument("--aliases", type=Path)
    args = parser.parse_args()
    try:
        output, count, skipped, aliases = convert(
            args.input,
            args.snapshot_date,
            args.source_commit,
            args.retention_days,
            args.first_pin,
            args.aliases,
        )
    except (OSError, ValueError, TypeError, OverflowError):
        print("invalid_price_input", file=sys.stderr)
        return 2
    sys.stdout.write(output)
    print(f"converted={count} skipped={skipped} aliases={aliases}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
