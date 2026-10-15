"""Shared web/packaged runtime construction with explicit resource ownership."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from concurrent.futures import Future
from dataclasses import dataclass, field
from functools import partial
from typing import Any

import flet as ft
import httpx

from relaydeck.client import open_timing
from relaydeck.client.adapters.clipboard import FletClipboard
from relaydeck.client.adapters.ports import ClipboardPort, CredentialStore
from relaydeck.client.components.appearance import configure_page_theme
from relaydeck.client.features.composer.observation import CommandObserver
from relaydeck.client.features.inventory.routing import DEFAULT_HEIGHT, RouteBridge
from relaydeck.client.features.shell.ops import HttpxShellGatewayOps
from relaydeck.client.features.sync.backoff import Backoff
from relaydeck.client.features.sync.controller import SyncController
from relaydeck.client.features.sync.transport import HttpxGatewayTransport, TransportError
from relaydeck.client.features.terminal.diagnostics import diagnostics_logger
from relaydeck.client.features.terminal.ui_resume import TerminalUIResumes, terminal_transition
from relaydeck.client.pending_commands import MemoryCommandStore, PendingCommands
from relaydeck.client.retained_sessions import RetainedSession, RetainedSessions
from relaydeck.client.sessions import SessionFactory, production_stream_factory, ws_base_for

OUTPUT_ACK_DEADLINE = 2.0


class VisibleSize:
    """Single owner of the visible page size fed to the route bridge.

    Resize and media (keyboard inset) events both feed one model:
    visible = layout height - bottom inset. `baseline` is the layout
    height per width with no keyboard. Browsers that shrink the page for
    the keyboard may deliver the shrunk resize BEFORE the inset, so a
    shrinking zero-inset resize does not replace the baseline until a
    grace period passes (a real window shrink) or a media event confirms
    the inset is zero. An inset is never subtracted from a height that
    is already at or below `baseline - inset`.
    """

    GRACE_SECONDS = 1.5

    def __init__(
        self, page: Any, bridge: RouteBridge, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._page = page
        self._bridge = bridge
        self._clock = clock
        self._baselines: dict[float, float] = {}
        self._pending: tuple[float, float, float] | None = None  # width, base, deadline
        self._last_width: float | None = None

    def _dims(self) -> tuple[float, float, float]:
        page = self._page
        width = getattr(page, "width", None) or 1280
        height = getattr(page, "height", None) or DEFAULT_HEIGHT
        insets = getattr(getattr(page, "media", None), "view_insets", None)
        return width, height, getattr(insets, "bottom", 0) or 0

    def _baseline_for(self, width: float) -> float | None:
        known = self._baselines.get(width)
        if known is not None:
            return known
        # Rotation with the keyboard open: the new layout height is
        # usually the old layout width of the same screen. Read the
        # previous orientation BEFORE `_last_width` is replaced.
        last = self._last_width
        if last is not None and last != width and self._baselines.get(last) == width:
            return last
        return None

    def measure(self, source: str = "resize") -> tuple[float, float]:
        width, height, inset = self._dims()
        now = self._clock()
        if inset <= 0:
            pending = self._pending
            if pending is not None and now > pending[2]:
                # Grace over with no inset: the shrink was a real window resize.
                self._baselines.pop(pending[0], None)
                self._pending = None
            base = self._baselines.get(width)
            if base is None or height >= base or source == "media":
                self._baselines[width] = height
                self._pending = None
            elif self._pending is None or self._pending[0] != width:
                # Shrunk resize with no inset yet: keep the old baseline
                # until a media event or the grace period decides.
                self._pending = (width, base, now + self.GRACE_SECONDS)
            self._last_width = width
            return width, height
        # Keyboard inset active: it confirms any pending shrink was the
        # keyboard, so the unoccluded baseline is kept for as long as the
        # inset lasts (no expiry).
        self._pending = None
        base = self._baseline_for(width)
        self._last_width = width
        if base is not None:
            self._baselines.setdefault(width, base)
            if height <= base - inset + 1:
                return width, height  # browser already resized for the keyboard
        return width, max(0.0, height - inset)

    def _sync(self, source: str) -> None:
        raw = self._dims()
        width, height = self.measure(source)
        # Content-free viewport trace (numbers and the event source only).
        diagnostics_logger().info(
            "viewport_event source=%s page_height=%s inset=%s measured_height=%s",
            source,
            round(raw[1]),
            round(raw[2]),
            round(height),
        )
        self._bridge.set_size(width, height)

    def sync(self, _event: Any = None) -> None:
        self._sync("resize")

    def sync_media(self, _event: Any = None) -> None:
        self._sync("media")

    def install(self) -> None:
        self._page.on_resize = self.sync
        self._page.on_media_change = self.sync_media
        self.sync_media()

    def uninstall(self) -> None:
        for name, handler in (("on_resize", self.sync), ("on_media_change", self.sync_media)):
            if getattr(self._page, name, None) == handler:
                setattr(self._page, name, None)


def visible_page_height(page: Any) -> float:
    """Page height minus the keyboard/gesture obscured area."""
    return VisibleSize(page, RouteBridge()).measure()[1]


@dataclass
class AppResources:
    props: dict[str, Any]
    client: httpx.AsyncClient
    owns_client: bool
    retained: RetainedSessions
    bridge: RouteBridge
    controller: SyncController
    transport: HttpxGatewayTransport
    commands: PendingCommands
    tasks: set[Any] = field(default_factory=set)
    ui_resumes: TerminalUIResumes = field(default_factory=TerminalUIResumes)
    visible_size: VisibleSize | None = None
    closing: bool = False
    ui_transport_generation: int = 0
    _ui_transport: Any = field(default=None, repr=False)
    _ui_current: Callable[[], bool] | None = field(default=None, repr=False)
    _ui_closed: asyncio.Event | None = field(default=None, repr=False)
    _ui_watchdog: asyncio.Task[None] | None = field(default=None, repr=False)
    _ui_output: dict[int, tuple[Any, int, int, float, int]] = field(
        default_factory=dict, repr=False
    )

    def __post_init__(self) -> None:
        self.retained.reconcile_ui = self._reconcile_terminal

    def fence_replaced_transport(self, transport: Any) -> None:
        """Fail closed before asynchronous identity revalidation on reconnect."""
        if transport is self._ui_transport:
            return
        self.ui_resumes.invalidate()
        for entry in self.retained.entries():
            term = entry.session.current_term
            if term is not None:
                term.invalidate_transport(self.ui_transport_generation)
            if entry.session.controller is not None:
                entry.session.controller.fence_ui_input(True)

    def _entry_current(self, entry: RetainedSession, generation: int) -> bool:
        try:
            authorized = self._ui_current is not None and self._ui_current()
        except Exception:
            return False
        return bool(
            not self.closing
            and authorized
            and self._ui_closed is not None
            and not self._ui_closed.is_set()
            and generation == self.ui_transport_generation
            and self.retained.device_foreground
            and entry is not None
            and self.retained.foreground is entry
            and not entry.stopped
            and not entry.watch_blocked
            and self.retained.get(entry.host_id, entry.kind, entry.key) is entry
        )

    def _reconcile_terminal(self, entry: RetainedSession, *, force: bool = False) -> None:
        generation = self.ui_transport_generation
        if self._ui_current is None or self._ui_closed is None:
            return
        session, term = entry.session, entry.session.current_term
        if term is not None and term is not entry.ui_term:
            if entry.ui_unsubscribe is not None:
                entry.ui_unsubscribe()
            entry.ui_term = term
            # Capture can arrive after binding/acquisition, without another lease event.
            entry.ui_unsubscribe = term.add_channel_listener(lambda: session._notify())
            term.invalidate_transport(generation)
        if session.controller is not None and (
            entry.ui_recovering
            or terminal_transition(session).control is not None
            or term is None
            or not term.transport_verified
        ):
            session.controller.fence_ui_input(True)
        if entry.ui_recovering or terminal_transition(session).control is not None:
            self.ui_resumes.defer(session)
            return
        if not self._entry_current(entry, generation) or term is None:
            return

        async def recover(reason: str) -> bool:
            if (
                not self._entry_current(entry, generation)
                or terminal_transition(session).control is not None
            ):
                return False
            held_controller, held_lease, lifecycle = (
                session.controller,
                session.lease,
                session.interaction_generation,
            )
            success = await self.retained.remount_ui(
                entry, generation, lambda: self._entry_current(entry, generation)
            )
            if (
                not success
                and self._entry_current(entry, generation)
                and not session.disposed
                and session.controller is held_controller
                and session.lease is held_lease
                and session.interaction_generation == lifecycle
                and terminal_transition(session).control is None
            ):
                session.require_ui_recovery()
            return success

        self.ui_resumes.start(
            session,
            transport=self._ui_transport,
            current=lambda: self._entry_current(entry, generation),
            closed=self._ui_closed,
            timeout=1.5,
            generation=generation,
            force=force,
            recover=recover,
        )
        self.retained.changed()  # Root's terminal-live indicator follows the verification fence.

    async def _watch_terminal_output(self) -> None:
        """No inactivity timer: only visible, outstanding unacknowledged output."""
        while not self.closing:
            await asyncio.sleep(0.1)
            entry = self.retained.foreground
            generation = self.ui_transport_generation
            timing = open_timing.of(entry.session) if entry is not None else None
            if timing is not None and entry is not None:
                timing.poll(entry.session.current_term, entry.session)
            if entry is None or not self._entry_current(entry, generation):
                self._ui_output.clear()
                continue
            session, term = entry.session, entry.session.current_term
            if (
                term is None
                or session.paused
                or session.controller is None
                or entry.ui_recovering
                or terminal_transition(session).control is not None
                or term._sent_bytes <= term._acked_bytes
            ):
                self._ui_output.clear()
                continue
            now = time.monotonic()
            previous = self._ui_output.get(id(entry))
            if previous is None or previous[:3] != (term, generation, term._acked_bytes):
                self._ui_output = {id(entry): (term, generation, term._acked_bytes, now, 0)}
                continue
            if now - previous[3] < OUTPUT_ACK_DEADLINE or id(term) in self.ui_resumes._active:
                continue
            self._ui_output[id(entry)] = (*previous[:3], now, previous[4] + 1)
            if previous[4] == 0:
                self._reconcile_terminal(entry, force=True)
            else:
                # A successful probe with no subsequent ACK progress is not live output.
                term.invalidate_transport(generation)
                session.controller.fence_ui_input(True)
                held_controller, held_lease, lifecycle = (
                    session.controller,
                    session.lease,
                    session.interaction_generation,
                )
                recovered = await self.retained.remount_ui(
                    entry,
                    generation,
                    partial(self._entry_current, entry, generation),
                )
                if (
                    not recovered
                    and self._entry_current(entry, generation)
                    and not session.disposed
                    and session.controller is held_controller
                    and session.lease is held_lease
                    and session.interaction_generation == lifecycle
                    and terminal_transition(session).control is None
                ):
                    session.require_ui_recovery()

    def resume_terminals(
        self, *, transport: Any, current: Callable[[], bool], closed: asyncio.Event
    ) -> None:
        """Persist the verified identity-bound transport, including hidden viewers."""
        if self.closing or closed.is_set() or not current():
            return
        # A resumed Flet session keeps this bridge but the browser may have a
        # different viewport now (reload at another width, Split View,
        # rotation) without a resize event: re-derive the size from the page.
        if self.visible_size is not None:
            self.visible_size.sync_media()
        replacement = self._ui_transport is not transport
        self._ui_transport, self._ui_current, self._ui_closed = transport, current, closed
        if replacement:
            self.ui_transport_generation += 1
            self.ui_resumes.invalidate()
            for entry in self.retained.entries():
                term = entry.session.current_term
                if term is not None:
                    term.invalidate_transport(self.ui_transport_generation)
                    term.rebind_transport()
                if entry.session.controller is not None:
                    entry.session.controller.fence_ui_input(True)
                self._reconcile_terminal(entry)
            self.retained.changed()
        elif self.retained.foreground is not None:
            self._reconcile_terminal(self.retained.foreground)
        if self._ui_watchdog is None:
            self._ui_watchdog = asyncio.create_task(self._watch_terminal_output())
            self.tasks.add(self._ui_watchdog)

    async def close(self) -> None:
        self.closing = True
        self.retained.reconcile_ui = lambda entry: None
        if self.visible_size is not None:
            self.visible_size.uninstall()
        await self.ui_resumes.close()
        for task in tuple(self.tasks):
            task.cancel()
        await asyncio.gather(
            *(
                asyncio.wrap_future(task) if isinstance(task, Future) else task
                for task in self.tasks
            ),
            return_exceptions=True,
        )
        await self.commands.close()
        await self.retained.shutdown()
        if self.owns_client:
            await self.client.aclose()


def build_resources(
    page: ft.Page,
    *,
    base_url: str,
    get_token: Callable[[], Any],
    host_id: str,
    account: str = "",
    device_id: str = "",
    on_logout: Callable[[], Any] | None = None,
    on_session_recovery: Callable[[], Any] | None = None,
    client: httpx.AsyncClient | None = None,
    clipboard: ClipboardPort | None = None,
    draft_store: CredentialStore | None = None,
    refresh_identity: Callable[[], Awaitable[bool]] | None = None,
    recovery_current: Callable[[], bool] = lambda: True,
    recovery_version: Callable[[], Any] = lambda: None,
    command_store: CredentialStore | None = None,
    command_current: Callable[[], bool] = lambda: True,
    command_version: Callable[[], Any] = lambda: None,
    command_origin: str | None = None,
    native_picture_upload: bool = False,
    track_keyboard: bool = False,
) -> AppResources:
    page.title = "RelayDeck"
    appearance = configure_page_theme(page)
    bridge = RouteBridge(
        route=page.route or "/",
        width=page.width or 1280,
        height=page.height or DEFAULT_HEIGHT,
    )
    page.on_route_change = lambda e: bridge.set_route(page.route or "/")
    visible_size: VisibleSize | None = None
    if track_keyboard:
        visible_size = VisibleSize(page, bridge)
        visible_size.install()
    else:
        page.on_resize = lambda e: bridge.set_size(
            page.width or 1280, page.height or DEFAULT_HEIGHT
        )

    def go(route: str) -> None:
        asyncio.create_task(page.push_route(route))

    owned = client is None
    client = client or httpx.AsyncClient(base_url=base_url, timeout=30.0)
    transport = HttpxGatewayTransport(base_url, get_token, client)
    commands = PendingCommands(
        command_store or MemoryCommandStore(),
        command_origin or base_url,
        account,
        device_id,
        current=command_current,
        version=command_version,
    )
    transport.commands = commands
    controller = SyncController(transport=transport, backoff=Backoff())

    async def recover_auth() -> bool:
        version = recovery_version()
        if not recovery_current():
            raise asyncio.CancelledError
        assert refresh_identity is not None
        recovered = await refresh_identity()
        if not recovery_current() or version != recovery_version():
            raise asyncio.CancelledError
        if not recovered:
            return False
        me = await transport.me()
        if (account and me["account"] != account) or (device_id and me["device_id"] != device_id):
            return False
        grants = await transport.hosts()
        if not recovery_current() or version != recovery_version():
            raise asyncio.CancelledError
        if not isinstance(grants.get("hosts"), list):
            raise TransportError("upstream_incompatible", "Invalid host grants", False)
        controller.apply_grants(grants["hosts"])
        return True

    if refresh_identity is not None:
        controller.recover_auth = recover_auth
    retained = RetainedSessions()
    factory = SessionFactory(
        ops=HttpxShellGatewayOps(
            base_url=base_url,
            token=get_token,
            client=client,
            native_picture_upload=native_picture_upload,
        ),
        open_stream=production_stream_factory(ws_base_for(base_url)),
        submit_command=transport.submit_command,
        prompt_text=None,
        observe_command=CommandObserver(transport.command_status).observe,
        renew_lease=transport.renew_lease,
        clipboard=clipboard or FletClipboard(ft.Clipboard()),
        device_id=device_id,
        draft_store=draft_store,
    )
    props = dict(
        controller=controller,
        transport=transport,
        host_id=host_id,
        bridge=bridge,
        go=go,
        session_factory=factory,
        account=account,
        on_logout=on_logout,
        on_session_recovery=on_session_recovery,
        appearance=appearance,
        retained_sessions=retained,
        pending_commands=commands,
    )
    resources = AppResources(
        props,
        client,
        owned,
        retained,
        bridge,
        controller,
        transport,
        commands,
        visible_size=visible_size,
    )
    # A notification tap opens /?rd_route=<token> once, from the page's first URL.
    from relaydeck.client.notifications_runtime import start_web_tap

    start_web_tap(page, resources, account, device_id)
    return resources
