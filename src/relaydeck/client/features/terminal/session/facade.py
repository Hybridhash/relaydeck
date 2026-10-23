"""Delegated terminal state and interaction surface.

The base initializes state; this mixin exposes properties and delegates to
renderer, renewal, key, paste and selection collaborators without lifecycle logic."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from relaydeck.client import open_timing
from relaydeck.client.features.shell.ops import AttachmentInfo, LeaseInfo, ShellGatewayOps
from relaydeck.client.features.terminal.controller import TerminalController
from relaydeck.client.features.terminal.models import TerminalViewState
from relaydeck.client.features.terminal.paste_policy import PasteReview
from relaydeck.client.features.terminal.renewal import (
    DEFAULT_RENEWAL_INTERVAL_S,
    LeaseRenewalDriver,
)
from relaydeck.client.features.terminal.session.keys import KeyInput
from relaydeck.client.features.terminal.session.lease import LeaseRenewalOwner
from relaydeck.client.features.terminal.session.paste import PasteFlow
from relaydeck.client.features.terminal.session.rendering import (
    DEFAULT_REOPEN_NOTICE,
    RendererBinding,
)
from relaydeck.client.features.terminal.session.selection import SelectionController
from relaydeck.lease_diagnostics import LeaseTrace

if TYPE_CHECKING:
    from relaydeck.client.features.terminal.surfaces import DetailSessions


class SessionFacade:
    # State and collaborators created by `TerminalSessionBase.__init__`.
    _ops: ShellGatewayOps
    _sessions: DetailSessions
    _phase: str
    _host_id: str
    _attachment: AttachmentInfo | None
    _lease: LeaseInfo | None
    _controller: TerminalController | None
    _term_state: TerminalViewState
    _owner_label: str
    _notice: str
    _bracketed: bool
    _paused: bool
    _disposed: bool
    _foreground: bool
    _interaction_generation: int
    _listeners: list[Callable[[Any], None]]
    _renderer: RendererBinding
    _lease_owner: LeaseRenewalOwner
    _selection_ctl: SelectionController
    _paste_flow: PasteFlow
    _key_input: KeyInput

    # -- observable state ---------------------------------------------------------
    @property
    def phase(self) -> str:
        return self._phase

    @property
    def host_id(self) -> str:
        return self._host_id

    @property
    def picture_api(self) -> Any:
        return self._ops.picture_api

    @property
    def sessions(self) -> DetailSessions:
        return self._sessions

    @property
    def attachment(self) -> AttachmentInfo | None:
        return self._attachment

    @property
    def lease(self) -> LeaseInfo | None:
        return self._lease

    @property
    def controller(self) -> TerminalController | None:
        return self._controller

    @property
    def term_state(self) -> TerminalViewState:
        return self._term_state

    @property
    def owner_label(self) -> str:
        return self._owner_label

    @property
    def notice(self) -> str:
        return self._notice

    @property
    def paused(self) -> bool:
        """True between `pause()` and `resume_after_pause()`."""
        return self._paused

    @property
    def disposed(self) -> bool:
        return self._disposed

    @property
    def foreground(self) -> bool:
        return self._foreground

    @property
    def interaction_generation(self) -> int:
        return self._interaction_generation

    @property
    def bracketed_paste(self) -> bool:
        return self._bracketed

    def set_bracketed_paste(self, enabled: bool) -> None:
        """Live-engine bracketed flag plumbing: the xterm2 engine reports
        its mode over MODE_REPORT after every write/reset (routed here
        by the mount); paste framing captures it at review open."""
        if self._bracketed != enabled:
            self._bracketed = enabled
            self._notify()

    def dismiss_notice(self, notice: Any) -> None:
        """Clear a dismissed notice; never touches input, focus or the draft."""
        if self._controller is not None:
            self._controller.dismiss_notice(notice)
        elif self._term_state.notice is notice:
            self.update_term_state(replace(self._term_state, notice=None))

    def update_term_state(self, state: TerminalViewState) -> None:
        previous = self._term_state.status
        self._term_state = state
        # Reset only on entry to avoid a feedback loop from repeated state emissions.
        # A disposed session cannot reset a renderer it no longer owns.
        if state.status == "reset" and previous != "reset" and not self._disposed:
            self._reset_bound_renderers("stream_reset")
        self._notify()

    @property
    def pending_review(self) -> PasteReview | None:
        if not self._sessions.bound or self._sessions.paste.pending is None:
            return None
        return self._sessions.paste.pending.review

    @property
    def keys_enabled(self) -> bool:
        """Tools-dock enablement per the keys.py mount contract: the bound
        target has a live control writer."""
        return self._sessions.has_live_writer()

    @property
    def paste_error(self) -> str | None:
        if not self._sessions.bound:
            return None
        return self._sessions.paste.error

    @property
    def armed_modifiers(self) -> frozenset[str]:
        if not self._sessions.bound:
            return frozenset()
        return self._sessions.keys.pending_modifiers

    # Subscriptions keep the mounted view current.
    def subscribe(self, listener: Callable[[Any], None]) -> Callable[[], None]:
        """Mirror controller/bridge subscription: `listener` runs after
        every session mutation (sync or background). Returns unsubscribe."""
        self._listeners.append(listener)

        def unsubscribe() -> None:
            try:
                self._listeners.remove(listener)
            except ValueError:
                pass

        return unsubscribe

    def _notify(self) -> None:
        for listener in list(self._listeners):
            listener(self)

    @property
    def activations(self) -> int:
        return getattr(self, "_activations", 0)

    def note_activation(self) -> None:
        """The user is back: the surface re-settles its geometry once."""
        self._activations = self.activations + 1
        self._notify()

    # -- input tokens ---------------------------------------------------------------
    @property
    def input_token(self) -> tuple[int, int] | None:
        """Current input-callback token (bind epoch, keys token), or None
        when no writer is bound (see `KeyInput.input_token`)."""
        return self._key_input.input_token()

    def _token_live(self, token: tuple[int, int]) -> bool:
        return self._key_input.token_live(token)

    @property
    def current_term(self) -> Any | None:
        """The bound stable renderer, or None when no view is mounted."""
        return self._renderer.current_term

    @property
    def terminal_live(self) -> bool:
        """Metadata liveness never substitutes for a verified native viewer."""
        term, controller = self.current_term, self.controller
        return bool(
            term is not None
            and term.transport_verified
            and controller is not None
            and not controller.ui_input_fenced
            and self.term_state.status == "live"
        )

    # -- renderer binding (delegates) -----------------------------------------------
    def bind_renderer(self, sink: Callable[[bytes], None]) -> int:
        """Register an output sink; returns its registration token."""
        return self._renderer.bind_renderer(sink)

    def unbind_renderer(self, token: int) -> None:
        self._renderer.unbind_renderer(token)

    def is_sink_live(self, token: int) -> bool:
        return self._renderer.is_sink_live(token)

    def bind_view(self, term: Any, *, write: Callable[[Any, bytes], bool] | None = None) -> int:
        """Mount a renderer view (see `RendererBinding.bind_view`)."""
        timing = open_timing.of(self)
        if timing is not None:
            timing.observe(term)
        return self._renderer.bind_view(term, write=write)

    def unbind_view(self) -> None:
        """Unmount the renderer view (see `RendererBinding.unbind_view`)."""
        self._renderer.unbind_view()

    def require_ui_recovery(self) -> None:
        """Unverified transport continuity: explicit recovery, never automatic attach/reset."""
        self._renderer.require_ui_recovery()

    def _viewer_reopened(self, notice: str = DEFAULT_REOPEN_NOTICE) -> None:
        self._renderer.viewer_reopened(notice)

    def _reset_bound_renderers(self, reason: str = "controller_replaced") -> None:
        self._renderer.reset_bound_renderers(reason)

    @property
    def _view_token(self) -> int | None:
        return self._renderer.view_token

    @property
    def _viewer_binding(self) -> tuple[Any, int] | None:
        return self._renderer.viewer_binding

    # -- lease renewal (delegates) -----------------------------------------------------
    @property
    def renewal_running(self) -> bool:
        return self._lease_owner.running

    @property
    def _renewal(self) -> LeaseRenewalDriver | None:
        return self._lease_owner.driver

    @_renewal.setter
    def _renewal(self, driver: LeaseRenewalDriver | None) -> None:
        self._lease_owner.driver = driver

    def start_lease_renewal(
        self,
        interval_s: float = DEFAULT_RENEWAL_INTERVAL_S,
        *,
        renew_lease: Callable[[str], Awaitable[dict[str, Any]]] | None = None,
    ) -> None:
        """(Re)start renewal for the held control lease (see `LeaseRenewalOwner.start`)."""
        self._lease_owner.start(interval_s, renew_lease=renew_lease)

    def _lease_trace(self, lease: LeaseInfo | None = None) -> LeaseTrace:
        return self._lease_owner.trace(lease)

    def _restart_renewal(self) -> None:
        self._lease_owner.restart()

    async def stop_lease_renewal(self) -> None:
        """Cancel renewal and wait for the task (pause/dispose paths)."""
        await self._lease_owner.stop()

    async def _release_stale_lease(self, lease: LeaseInfo) -> None:
        await self._lease_owner.release_stale(lease)

    # -- keys / paste / selection (delegates) -------------------------------------------
    def _cancel_term_scroll(self) -> None:
        """Retire widget scroll motion before a server-side write, so wheel
        rows already in flight drop instead of landing after the key.
        Best-effort: nothing bound means nothing in flight, and a bound
        view without the scroll channel (older fakes) has nothing to
        retire. Never raises: input must not depend on the cancel."""
        term = self.current_term
        cancel = getattr(term, "cancel_scroll", None)
        if cancel is not None:
            cancel()

    async def type_text(self, text: str, *, token: int | None = None) -> str:
        """Committed text routed by workflow (see `KeyInput.type_text`)."""
        self._cancel_term_scroll()
        return await self._key_input.type_text(text, token=token)

    async def handle_view_input(
        self,
        origin: int,
        data: bytes,
        *,
        token: tuple[int, int] | None = None,
        native_text: bool = False,
    ) -> str:
        """Extension input packets, generation-bound (see `KeyInput.handle_view_input`)."""
        return await self._key_input.handle_view_input(
            origin, data, token=token, native_text=native_text
        )

    async def press_key(
        self, name: str, *, physical_modifiers: frozenset[str] = frozenset()
    ) -> bool:
        """One tools/dock key through the bound writer (see `KeyInput.press_key`)."""
        self._cancel_term_scroll()
        return await self._key_input.press_key(name, physical_modifiers=physical_modifiers)

    def toggle_modifier(self, modifier: str) -> None:
        self._key_input.toggle_modifier(modifier)

    def interrupt(self) -> bool:
        """Control-only interrupt (Ctrl-C): discards pre-armed mods."""
        return self._key_input.interrupt()

    async def flush_keys(self) -> int:
        return await self._key_input.flush()

    async def request_paste(
        self, text: str, *, require_review: bool = False, from_app: bool = False
    ) -> str:
        """Classify pasted text (see `PasteFlow.request`)."""
        self._cancel_term_scroll()
        return await self._paste_flow.request(
            text, require_review=require_review, from_app=from_app
        )

    async def confirm_paste(self) -> bool:
        self._cancel_term_scroll()
        return await self._paste_flow.confirm()

    def cancel_paste(self) -> None:
        self._paste_flow.cancel()

    async def paste_from_clipboard(self) -> str:
        """Paste button: read the clipboard once, then classify through the
        reviewed-paste path. Never implies Enter (see `PasteFlow`)."""
        self._cancel_term_scroll()
        return await self._paste_flow.from_clipboard()

    def stale_review_notice(self) -> str:
        return PasteFlow.stale_review_notice()

    def open_selection(self, anchor: int | None = None, focus: int | None = None) -> None:
        self._selection_ctl.open(anchor, focus)

    @property
    def selection_sheet_text(self) -> str:
        """Frozen engine snapshot when one was fetched for this sheet, else the raw output log."""
        return self._selection_ctl.sheet_text

    @property
    def _selection_snapshot(self) -> str | None:
        return self._selection_ctl.snapshot

    @_selection_snapshot.setter
    def _selection_snapshot(self, value: str | None) -> None:
        self._selection_ctl.snapshot = value

    async def open_selection_snapshot(
        self, anchor: int | None = None, focus: int | None = None
    ) -> None:
        """Open Select Text on a frozen ENGINE snapshot (see `SelectionController`)."""
        await self._selection_ctl.open_snapshot(anchor, focus)

    def move_selection(self, anchor: int, focus: int) -> None:
        self._selection_ctl.move(anchor, focus)

    def close_selection(self) -> None:
        self._selection_ctl.close()

    def jump_to_bottom(self) -> None:
        self._selection_ctl.jump_to_bottom()

    async def copy_selection(self, text: str, anchor: int, focus: int) -> str:
        """Copy the anchored range through the platform clipboard port.
        Never emits PTY input and never touches a composer draft."""
        return await self._selection_ctl.copy(text, anchor, focus)


__all__ = ["SessionFacade"]
