"""Generation-bound terminal input shared by AgentSession and ShellSession.

Routes committed text, engine-encoded keys and tools/dock keys into the one
live writer owned by the session's input-session bundle. It holds no session
reference; everything arrives through `KeyHooks`.

Input invariants:
- there is exactly one writer: every send goes through the bound
  `KeyInputSession` (`keys()`), and nothing is sent unless a live writer is
  bound (`bound()`);
- an input token (bind epoch, keys token) captured at render is rejected as
  soon as the writer changes (unmount, selection change, takeover, reconnect,
  remount, pause, background): `ROUTE_DROPPED`, nothing sent;
- nothing is replayed or queued across a writer change; a dropped or
  unencodable input simply returns, leaving modifiers armed;
- mouse reports never become input;
- the Composer (draft routing) stays agent-only: a shell has no store, so a
  shell can never route to a draft.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from relaydeck_terminal.protocol import ORIGIN_KEY, ORIGIN_MOUSE, ORIGIN_TEXT

from relaydeck.client.features.composer.domain import ComposerStore
from relaydeck.client.features.terminal.input_modes import (
    ROUTE_DRAFT,
    ROUTE_DROPPED,
    ROUTE_PTY,
    InputModeState,
    route_committed_text,
)
from relaydeck.client.features.terminal.keys import KeyInputSession
from relaydeck.client.features.terminal.paste_policy import contains_only_safe_scalars


async def _drop_review(_text: str) -> str:
    return ROUTE_DROPPED


@dataclass(frozen=True)
class KeyHooks:
    paused: Callable[[], bool]
    foreground: Callable[[], bool]
    bound: Callable[[], bool]  # a live writer is bound
    epoch: Callable[[], int]  # bind epoch of the live writer
    keys: Callable[[], KeyInputSession]
    modes: Callable[[], InputModeState]
    notify: Callable[[], None]
    note_input: Callable[[], None] = lambda: None
    review_text: Callable[[str], Awaitable[str]] = _drop_review
    # Agent-only differences; shells keep the defaults.
    store: Callable[[], ComposerStore | None] = lambda: None
    allow_draft: bool = False  # only the agent Composer may route to a draft
    drops_engine_keys: Callable[[], bool] = lambda: False  # agent Composer edits the draft


class KeyInput:
    def __init__(self, hooks: KeyHooks) -> None:
        self._hooks = hooks

    # -- input tokens ---------------------------------------------------------
    def input_token(self) -> tuple[int, int] | None:
        """Current input-callback token (bind epoch, keys token), or None
        when no writer is bound. Mounted input callbacks capture this at
        render; unmount, selection change, takeover, reconnect, and
        remount invalidate it (the epoch also guards the fresh-session
        token restart)."""
        hooks = self._hooks
        if hooks.paused() or not hooks.foreground() or not hooks.bound():
            return None
        return (hooks.epoch(), hooks.keys().token)

    def token_live(self, token: tuple[int, int]) -> bool:
        hooks = self._hooks
        return (
            hooks.foreground()
            and not hooks.paused()
            and hooks.bound()
            and hooks.epoch() == token[0]
            and hooks.keys().token == token[1]
        )

    # -- committed text / engine input ---------------------------------------
    async def type_text(self, text: str, *, token: int | None = None) -> str:
        """Committed text routed by workflow: the agent Composer appends to
        the real draft (the PTY sender is untouched); Direct Input and shells
        bind to the live writer generation. Returns `draft`/`pty`/`dropped`
        (a shell never returns `draft`)."""
        hooks = self._hooks
        if not hooks.bound():
            return ROUTE_DROPPED
        hooks.note_input()
        route = route_committed_text(
            hooks.modes(),
            store=hooks.store(),
            session=hooks.keys(),
            text=text,
            token=token,
        )
        if not hooks.allow_draft and route == ROUTE_DRAFT:
            raise ValueError("shell typing must never route to a draft")
        await hooks.keys().flush()
        hooks.notify()
        return route

    async def handle_view_input(
        self,
        origin: int,
        data: bytes,
        *,
        token: tuple[int, int] | None = None,
        native_text: bool = False,
    ) -> str:
        """Route generation-bound native input; drop mouse packets and stale input.
        Review native bulk text outside Composer; keep engine keys and field editing controls.
        """
        hooks = self._hooks
        if (
            hooks.paused()
            or not hooks.foreground()
            or (token is not None and not self.token_live(token))
        ):
            return ROUTE_DROPPED
        if origin == ORIGIN_MOUSE:
            return ROUTE_DROPPED
        if not hooks.bound():
            return ROUTE_DROPPED
        key_token = token[1] if token is not None else None
        if origin == ORIGIN_KEY:
            hooks.note_input()
            if hooks.drops_engine_keys():
                return ROUTE_DROPPED
            emission = hooks.keys().commit_bytes(bytes(data), token=key_token)
            if emission is None:
                return ROUTE_DROPPED
            await hooks.keys().flush()
            hooks.notify()
            return ROUTE_PTY
        text = data.decode("utf-8", errors="replace")
        if (
            native_text
            and origin == ORIGIN_TEXT
            and not hooks.modes().text_edits_draft
            and text not in ("\n", "\r")
            and (any(char in text for char in "\r\n\t") or not contains_only_safe_scalars(text))
        ):
            return await hooks.review_text(text)
        return await self.type_text(text, token=key_token)

    # -- tools/dock keys -------------------------------------------------------
    async def press_key(
        self, name: str, *, physical_modifiers: frozenset[str] = frozenset()
    ) -> bool:
        """One tools/dock key through the bound writer. False when no live
        writer, stale, or unencodable (nothing sent, modifiers preserved)."""
        hooks = self._hooks
        if not hooks.bound():
            return False
        hooks.note_input()
        emission = hooks.keys().press_key(name, physical_modifiers=physical_modifiers)
        if emission is None:
            return False
        await hooks.keys().flush()
        hooks.notify()
        return True

    def toggle_modifier(self, modifier: str) -> None:
        hooks = self._hooks
        if not hooks.bound():
            return
        hooks.keys().toggle_modifier(modifier)
        hooks.notify()

    def interrupt(self) -> bool:
        """Control-only interrupt (Ctrl-C): discards pre-armed mods."""
        hooks = self._hooks
        if not hooks.bound():
            return False
        return hooks.keys().interrupt() is not None

    async def flush(self) -> int:
        hooks = self._hooks
        if not hooks.bound():
            return 0
        return await hooks.keys().flush()
