"""Compact sorted JSON for stored records, digests and signed payloads."""

from __future__ import annotations

import json


def canonical_json(value: object, *, ensure_ascii: bool = True, allow_nan: bool = True) -> str:
    """Preserve each caller's Unicode and non-finite-number encoding policy."""
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=ensure_ascii, allow_nan=allow_nan
    )
