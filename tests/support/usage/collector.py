"""Shared collector test helpers (moved out of test modules)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from relaydeck.usage.contracts import (
    Batch,
    Budget,
    Coverage,
    Cursor,
    FileCursor,
    LimitSnapshot,
    UsageEvent,
)


class FakeReader:
    format_id = "format-a"

    def __init__(self) -> None:
        self.cursors: list[Cursor] = []
        self.budgets: list[Budget] = []
        self.events: tuple[UsageEvent, ...] = ()
        self.limits: tuple[LimitSnapshot, ...] = ()
        self.error = False
        self.more = False
        self.coverage = Coverage.COMPLETE
        self.retry_blocked = False  # a current obstruction in this pass
        self.state: dict[str, str] | None = None
        self.files: dict[str, FileCursor] | None = None
        self.advance: Any = lambda: None

    def read(self, root: Path, cursor: Cursor, budget: Budget) -> Batch:
        self.cursors.append(cursor)
        self.budgets.append(budget)
        self.advance()
        if self.error:
            raise ValueError("private-marker session-marker")
        return Batch(
            self.events[: budget.max_events],
            Cursor(
                cursor.files if self.files is None else self.files,
                cursor.state if self.state is None else self.state,
                cursor.revision,
            ),
            self.coverage,
            cursor.revision,
            self.limits,
            more=self.more,
            retry_blocked=self.retry_blocked,
        )
