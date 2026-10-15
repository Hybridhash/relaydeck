"""Reviewed paste shared by AgentSession and ShellSession.

A thin, safety-preserving front for the generation-bound `PasteReviewSession`
that the session's input-session bundle owns. It adds nothing to the safety
model and must not weaken it:

- nothing happens unless a live writer is bound (`bound()`), so a paste, a
  review or a confirm can never reach a retired writer;
- a multiline/blocked paste only OPENS a verbatim review; text is written only
  by an explicit `confirm()`, and the review session rejects a confirm whose
  generation/input token is stale (`STALE_MESSAGE`), so a late or replayed
  confirm cannot send anything;
- `from_clipboard()` reads the clipboard once and drops the result if the input
  token or interaction generation changed meanwhile (`REQUEST_BLOCKED`);
- nothing is ever queued, retried or replayed here.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from relaydeck.client.adapters.clipboard import ClipboardReadStatus, read_clipboard
from relaydeck.client.features.terminal.paste_review import (
    REQUEST_BLOCKED,
    STALE_MESSAGE,
    PasteReviewSession,
)


@dataclass(frozen=True)
class PasteHooks:
    bound: Callable[[], bool]  # a live writer is bound
    review: Callable[[], PasteReviewSession]
    bracketed: Callable[[], bool]
    input_token: Callable[[], Any]
    interaction_generation: Callable[[], int]
    clipboard: Callable[[], Any | None]
    notify: Callable[[], None]


class PasteFlow:
    def __init__(self, hooks: PasteHooks) -> None:
        self._hooks = hooks

    async def request(
        self, text: str, *, require_review: bool = False, from_app: bool = False
    ) -> str:
        """Classify pasted text with the live-engine bracketed flag: single
        line inserts now, multiline opens a generation-bound review, unsafe
        controls reject, no writer blocks. Only the insert path sends."""
        hooks = self._hooks
        if not hooks.bound():
            return REQUEST_BLOCKED
        outcome = await hooks.review().request_paste(
            text, hooks.bracketed(), require_review=require_review, from_app=from_app
        )
        hooks.notify()
        return outcome

    async def confirm(self) -> bool:
        hooks = self._hooks
        if not hooks.bound():
            return False
        confirmed = await hooks.review().confirm_paste()
        hooks.notify()
        return confirmed

    def cancel(self) -> None:
        hooks = self._hooks
        if not hooks.bound():
            return
        hooks.review().cancel_paste()
        hooks.notify()

    async def from_clipboard(self) -> str:
        """Paste button: read the platform clipboard once, then classify
        through the reviewed-paste path (single-line inserts, multiline
        opens verbatim review with counts, unsafe rejects, no writer
        blocks). Never implies Enter; composer drafts are untouched."""
        hooks = self._hooks
        token, interaction = hooks.input_token(), hooks.interaction_generation()
        result = await read_clipboard(hooks.clipboard())
        if token != hooks.input_token() or interaction != hooks.interaction_generation():
            return REQUEST_BLOCKED
        if result.status == ClipboardReadStatus.UNAVAILABLE:
            return result.status
        # In-app copy standing in for a blocked clipboard: always review first.
        return await self.request(
            result.text or "", require_review=result.from_app, from_app=result.from_app
        )

    @staticmethod
    def stale_review_notice() -> str:
        return STALE_MESSAGE
