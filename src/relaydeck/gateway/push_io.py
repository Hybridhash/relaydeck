"""Ordered, cancellation-settled storage work outside the application loop."""

from __future__ import annotations

from typing import Any
from weakref import WeakKeyDictionary

from relaydeck.offload import OrderedGate, run_blocking

_gates: WeakKeyDictionary[Any, OrderedGate] = WeakKeyDictionary()


async def push_io(store: Any, operation: str, *args: Any, **kwargs: Any) -> Any:
    """Admission occurs on the loop; SQL and native store-lock waits do not."""
    gate = _gates.setdefault(store, OrderedGate())
    async with gate.hold():
        return await run_blocking(getattr(store, operation), *args, **kwargs)
