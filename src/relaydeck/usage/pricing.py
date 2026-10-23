"""Explicit, dated API-equivalent estimates; unknown prices stay tokens-only."""

from __future__ import annotations

import math
import tomllib
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from relaydeck.usage.contracts import MAX_MODEL_CHARS, Counters, PriceRate


class PriceTableError(ValueError):
    def __init__(self) -> None:
        super().__init__("invalid_price_table")


@dataclass(frozen=True)
class PriceTable:
    rates: tuple[PriceRate, ...] = ()
    provenance: tuple[str, ...] = ()  # aligned with rates; actual pin date, not applicability
    snapshot_sources: tuple[tuple[str, str], ...] = ()
    snapshot_labels: tuple[tuple[str, str], ...] = ()
    # (logged model, priced model): explicit only, each target has a rate in this table.
    aliases: tuple[tuple[str, str], ...] = ()

    @property
    def price_table_date(self) -> str | None:
        return max((day for day, _ in self.snapshot_sources), default=None)

    def _eligible(self, event_day: str, model: str | None) -> list[tuple[int, PriceRate]]:
        return [
            (i, r)
            for i, r in enumerate(self.rates)
            if r.model == model and r.effective_from <= event_day
        ]

    def cost_micro_usd(self, event_day: str, model: str | None, counters: Counters) -> int | None:
        return self.estimate(event_day, model, counters)[0]

    def estimate(
        self, event_day: str, model: str | None, counters: Counters
    ) -> tuple[int | None, str | None]:
        if counters.total == 0 and not counters.cache_write_unknown:
            # Genuinely empty (every counter 0): nothing to price, so no rate lookup and no price
            # snapshot. Any nonzero count with an unknown price still returns None below.
            return 0, None
        eligible = self._eligible(event_day, model)
        if not eligible:
            # No exact rate: only an explicit alias may stand in, never a guessed match.
            target = dict(self.aliases).get(model) if model is not None else None
            if target is not None:
                eligible = self._eligible(event_day, target)
        if not eligible or counters.cache_write_unknown:
            return None, None
        index, rate = max(eligible, key=lambda pair: pair[1].effective_from)
        if index >= len(self.provenance):
            return None, None  # A rate without explicit pin provenance cannot form an estimate.
        snapshot = self.provenance[index]
        prompt = (
            counters.input_uncached
            + counters.cache_read
            + counters.cache_write_short
            + counters.cache_write_long
            + counters.cache_write_unknown
        )
        above = rate.tier_threshold_tokens is not None and prompt > rate.tier_threshold_tokens
        buckets = (
            (counters.input_uncached, rate.input_above_threshold if above else rate.input),
            (counters.output, rate.output_above_threshold if above else rate.output),
            (counters.cache_read, rate.cache_read_above_threshold if above else rate.cache_read),
            (
                counters.cache_write_short,
                rate.cache_write_short_above_threshold if above else rate.cache_write_short,
            ),
            (
                counters.cache_write_long,
                rate.cache_write_long_above_threshold if above else rate.cache_write_long,
            ),
        )
        if any(count and price is None for count, price in buckets):
            return None, None
        # USD / million tokens converts directly to micro-USD per token.
        total = sum(
            (
                Decimal(count) * Decimal(str(price))
                for count, price in buckets
                if count and price is not None
            ),
            Decimal(0),
        )
        return int(total.to_integral_value(rounding=ROUND_HALF_UP)), snapshot


def load_price_table(path: Path | tuple[Path, ...]) -> PriceTable:
    """Combine explicit pins, rejecting duplicate model/applicability pairs."""
    paths = (path,) if isinstance(path, Path) else path
    if not paths or len(paths) > 128:
        raise PriceTableError()
    tables = tuple(_load_pin(p) for p in paths)
    entries = sorted(
        ((r, pin) for t in tables for r, pin in zip(t.rates, t.provenance, strict=True)),
        key=lambda pair: (pair[0].model, pair[0].effective_from),
    )
    if len({(r.model, r.effective_from) for r, _ in entries}) != len(entries):
        raise PriceTableError()
    sources = tuple(sorted({s for t in tables for s in t.snapshot_sources}))
    if len({d for d, _ in sources}) != len(sources):
        raise PriceTableError()
    labels = tuple(sorted({s for t in tables for s in t.snapshot_labels}))
    if len({d for d, _ in labels}) != len(labels):
        raise PriceTableError()
    rates = tuple(r for r, _ in entries)
    return PriceTable(
        rates,
        tuple(p for _, p in entries),
        sources,
        labels,
        _resolve_aliases(rates, paths),
    )


PACKAGED_ALIASES = Path(__file__).with_name("price_aliases.toml")
USER_ALIASES = "aliases.toml"


def _read_aliases(path: Path) -> dict[str, str]:
    """An `[aliases]` table of model -> model. A malformed file contributes nothing."""
    try:
        with path.open("rb") as stream:
            data = stream.read(64 * 1024 + 1)
        if len(data) > 64 * 1024:
            return {}
        table = tomllib.loads(data.decode("utf-8")).get("aliases", {})
    except (OSError, ValueError):
        return {}
    if not isinstance(table, dict):
        return {}
    return {
        key: value
        for key, value in table.items()
        if isinstance(value, str)
        and 0 < len(key) <= MAX_MODEL_CHARS
        and 0 < len(value) <= MAX_MODEL_CHARS
    }


def _resolve_aliases(
    rates: tuple[PriceRate, ...], pins: tuple[Path, ...]
) -> tuple[tuple[str, str], ...]:
    """Packaged aliases, extended by an `aliases.toml` beside any pin. An alias is kept only
    when its target is priced here and its own name is not, so an exact rate always wins."""
    merged = _read_aliases(PACKAGED_ALIASES)
    for directory in dict.fromkeys(pin.parent for pin in pins):
        merged.update(_read_aliases(directory / USER_ALIASES))
    priced = {r.model for r in rates}
    return tuple(
        sorted(
            (name, target)
            for name, target in merged.items()
            if target in priced and name not in priced
        )
    )


def _load_pin(path: Path) -> PriceTable:
    """Reject the entire file on error; callers keep PriceTable() for tokens-only."""
    try:
        if path.stat().st_size > 1024 * 1024:
            raise ValueError
        with path.open("rb") as stream:
            data = stream.read(1024 * 1024 + 1)
        if len(data) > 1024 * 1024:
            raise ValueError
        document = tomllib.loads(data.decode("utf-8"))
        rows = document["rates"]
        snapshot = document["snapshot"]
        if (
            set(document) != {"rates", "snapshot"}
            or not isinstance(rows, list)
            or len(rows) > 4096
            or not isinstance(snapshot, dict)
            or not {"date", "source"} <= snapshot.keys()
            or set(snapshot) - {"date", "source", "label"}
        ):
            raise ValueError
        pin, source = snapshot["date"], snapshot["source"]
        label = snapshot.get("label")
        if label is not None and (
            not isinstance(label, str)
            or not 0 < len(label) <= 256
            or any(ord(c) < 32 or ord(c) == 127 for c in label)
        ):
            raise ValueError
        if isinstance(pin, date) and not isinstance(pin, datetime):
            pin = pin.isoformat()
        if (
            not isinstance(pin, str)
            or date.fromisoformat(pin).isoformat() != pin
            or not isinstance(source, str)
            or not 0 < len(source) <= 256
            or any(ord(c) < 32 or ord(c) == 127 for c in source)
        ):
            raise ValueError
        rates = []
        seen = set()
        buckets = {
            "input",
            "output",
            "cache_read",
            "cache_write_short",
            "cache_write_long",
        }
        above_fields = {name + "_above_threshold" for name in buckets}
        fields = {"model", "effective_from", "tier_threshold_tokens", *buckets, *above_fields}
        for row in rows:
            if not isinstance(row, dict) or set(row) - fields:
                raise ValueError
            model, day = row["model"], row["effective_from"]
            if (
                not isinstance(model, str)
                or not 0 < len(model) <= MAX_MODEL_CHARS
                or any(ord(c) < 32 for c in model)
            ):
                raise ValueError
            if isinstance(day, date) and not isinstance(day, datetime):
                day = day.isoformat()
            if not isinstance(day, str) or date.fromisoformat(day).isoformat() != day:
                raise ValueError
            pair = model, day
            if pair in seen:
                raise ValueError
            seen.add(pair)
            prices: dict[str, float | None] = {}
            for name in buckets | above_fields:
                value = row.get(name)
                if value is None and name not in {"input", "output"}:
                    prices[name] = None
                elif (
                    not isinstance(value, int | float)
                    or isinstance(value, bool)
                    or not math.isfinite(value)
                    or value < 0
                ):
                    raise ValueError
                else:
                    prices[name] = float(value)
            assert prices["input"] is not None and prices["output"] is not None  # noqa: S101
            threshold = row.get("tier_threshold_tokens")
            if threshold is None:
                if any(prices[name] is not None for name in above_fields):
                    raise ValueError
            elif (
                type(threshold) is not int
                or not 0 < threshold <= 2**63 - 1
                or prices["input_above_threshold"] is None
                or prices["output_above_threshold"] is None
            ):
                raise ValueError
            rates.append(
                PriceRate(
                    model,
                    day,
                    prices["input"],
                    prices["output"],
                    prices["cache_read"],
                    prices["cache_write_short"],
                    prices["cache_write_long"],
                    threshold,
                    prices["input_above_threshold"],
                    prices["output_above_threshold"],
                    prices["cache_read_above_threshold"],
                    prices["cache_write_short_above_threshold"],
                    prices["cache_write_long_above_threshold"],
                )
            )
        ordered = tuple(sorted(rates, key=lambda r: (r.model, r.effective_from)))
        return PriceTable(
            ordered,
            (pin,) * len(ordered),
            ((pin, source),),
            ((pin, label),) if label is not None else (),
        )
    except (OSError, ValueError, KeyError, TypeError, OverflowError):
        raise PriceTableError() from None
