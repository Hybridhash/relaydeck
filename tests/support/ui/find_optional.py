"""Look up a control that may be absent (for example, no host row with one host)."""

from __future__ import annotations

from typing import Any


def find_or_none(app: Any, key: str) -> Any:
    """The control with `key`, or None when the rendered tree has no such control."""
    try:
        return app.find(key)
    except KeyError:
        return None


def chip_key(item: Any) -> str | None:
    """The key of a filter chip, whether it is the button itself or wrapped in semantics."""
    for candidate in (
        item,
        getattr(item, "content", None),
        getattr(getattr(item, "content", None), "content", None),
    ):
        key = getattr(candidate, "key", None)
        if isinstance(key, str):
            return key
    return None


def chips_with(row: Any, prefix: str) -> list[Any]:
    """The chips of one filter row whose keys start with `prefix` (for example, the space chips)."""
    return [item for item in row.controls if (chip_key(item) or "").startswith(prefix)]
