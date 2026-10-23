"""Packaged identity and resource ownership; native services are injected."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Callable
from typing import Any, cast
from urllib.parse import unquote, urlsplit

import flet as ft
import httpx

from relaydeck.client.adapters.ports import ClipboardPort, CredentialStore
from relaydeck.client.diagnostics import failure_category
from relaydeck.client.features.auth.service import (
    REFRESH_KEY,
    AuthService,
    AuthState,
    GatewayConfig,
    HttpxTransport,
)
from relaydeck.client.features.inventory.routing import format_route, parse_route
from relaydeck.client.features.lifecycle.machine import AppPhase, LifecycleMachine
from relaydeck.client.features.lifecycle.resume import ResumeOrchestrator, ResumeReport
from relaydeck.client.features.sync.transport import TransportError
from relaydeck.client.notifications_runtime import NotificationRuntime
from relaydeck.client.resources import AppResources, build_resources
from relaydeck.client.sessions import session_live
from relaydeck.contracts import paths
from relaydeck.contracts.schemas import Me, validate

logger = logging.getLogger(__name__)


class EndpointError(ValueError):
    """The pairing address is unusable. `reason` is one of a fixed set of
    sentences: it names the problem, never the address or any part of it."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


PAIRING_SETUP_FAILED = (
    "Could not set up pairing on this device. Check that secure storage is available and try again."
)


def private_endpoint(value: str) -> str:
    """Validate the Mac's private HTTPS origin; raises `EndpointError` (a
    `ValueError`) with a specific, content-free reason."""
    text = value.strip()
    if not text:
        raise EndpointError("Enter your Mac's private HTTPS address.")
    if len(value) > 1024:
        raise EndpointError("The address is too long.")
    try:
        parsed = urlsplit(text)
        port = parsed.port
    except ValueError:
        raise EndpointError("The port must be a number between 1 and 65535.") from None
    if parsed.scheme != "https":
        if "://" not in text:
            raise EndpointError("Start the address with https://.")
        if parsed.scheme == "http":
            raise EndpointError("Use https://, not http://.")
        raise EndpointError("The address must start with https://.")
    if not parsed.hostname:
        raise EndpointError("The address needs a host name.")
    if parsed.username or parsed.password:
        raise EndpointError("Remove the username and password from the address.")
    if parsed.query or parsed.fragment or parsed.path not in ("", "/"):
        raise EndpointError("Use only the host and port, without a path, query or fragment.")
    if port is not None and not 1 <= port <= 65535:
        raise EndpointError("The port must be a number between 1 and 65535.")
    return text.rstrip("/")


class ScopedDrafts(CredentialStore):
    """Native-safe hashed account/terminal keys over the secure credential adapter."""

    def __init__(self, base: CredentialStore, account: str) -> None:
        self.base = base
        self.scope = hashlib.sha256(account.encode()).hexdigest()

    def _key(self, key: str) -> str:
        return "draft-" + hashlib.sha256((self.scope + ":" + key).encode()).hexdigest()

    async def available(self) -> bool:
        return await self.base.available()

    async def get(self, key: str) -> str | None:
        return await self.base.get(self._key(key))

    async def set(self, key: str, value: str) -> None:
        await self.base.set(self._key(key), value)

    async def delete(self, key: str) -> None:
        await self.base.delete(self._key(key))

    async def clear_account(self) -> None:
        await self.base.clear_account()  # Explicit whole-app logout, including credentials/drafts.


class PackagedRuntime:
    def __init__(
        self,
        page: ft.Page,
        credentials: CredentialStore,
        preferences: Any,
        open_browser: Callable[[str], Any],
        notify: Callable[[], Any],
        *,
        client: httpx.AsyncClient | None = None,
        clipboard: ClipboardPort | None = None,
    ) -> None:
        self.page = page
        self.credentials = credentials
        self.preferences = preferences
        self.open_browser = open_browser
        self.notify = notify
        self.client = client or httpx.AsyncClient(timeout=30.0)
        self.owns_client = client is None
        self.clipboard = clipboard
        self.auth: AuthService | None = None
        self.resources: AppResources | None = None
        self.phase = "setup"
        self.notice = ""
        self.endpoint = ""
        self.generation = 0
        self.alive = True
        self.interactive = False
        self.pending_callback = ""
        self._lock = asyncio.Lock()
        self.foreground = True
        self.online = True
        self.lifecycle = LifecycleMachine()
        self.resume_report: ResumeReport | None = None
        self._pause_task: asyncio.Task[None] | None = None
        self._resume_task: asyncio.Task[None] | None = None
        self._tasks: set[asyncio.Task[Any]] = set()
        self._links: tuple[str, ...] | None = None
        self.notifications: NotificationRuntime | None = None
        from relaydeck.client.features.pairing.service import PairingFlow

        async def get_pairing(url: str, headers: dict[str, str]) -> Any:
            response = await self.client.get(url, headers=headers)
            return response.status_code, response.json()

        async def accept_pairing(
            tokens: dict[str, Any], principal: dict[str, Any], current: Any
        ) -> bool:
            async with self._lock:
                if (
                    not current()
                    or not self.alive
                    or self.auth is None
                    or self.generation != self.pairing_generation
                ):
                    return False
                return await self.auth.accept_pairing(
                    tokens,
                    lambda: current() and self.alive and self.generation == self.pairing_generation,
                )

        self.pairing_generation = -1
        self.pairing_available = False
        self.pairing = PairingFlow(
            self.credentials, HttpxTransport(self.client).post_json, get_pairing, accept_pairing
        )
        self.pairing.subscribe(lambda snapshot: self.notify() if self.alive else None)

    def input_allowed(self) -> bool:
        return (
            self.alive
            and self.foreground
            and self.online
            and self.interactive
            and self.phase == "ready"
        )

    def _spawn(self, operation: Any) -> asyncio.Task[Any]:
        task = asyncio.create_task(operation)
        self._tasks.add(task)

        def finished(done: asyncio.Task[Any]) -> None:
            self._tasks.discard(done)
            if not done.cancelled() and done.exception() is not None and self.alive:
                self.interactive = False
                self.phase = "error"
                self.notice = "Current state unavailable. Resume to retry without replaying input."
                self.notify()
            if (
                done is self._resume_task
                and self.alive
                and self.foreground
                and self.online
                and self.phase == "paused"
            ):
                self.request_resume()

        task.add_done_callback(finished)
        return task

    def background(self) -> None:
        self.foreground = False
        self._suspend()

    def _suspend(self) -> None:
        """Fence writers in the event callback, before any asynchronous work."""
        if not self.alive:
            return
        if self.pairing.snapshot.phase in {"connecting", "waiting", "approved"}:
            self.pairing.invalidate()
        self.generation += 1
        self.interactive = False
        if self.lifecycle.phase == AppPhase.FOREGROUND:
            self.lifecycle.on_background()
        if self.resources is not None:
            for entry in self.resources.retained.entries():
                entry.session.suspend_input()
            self.phase = "paused"
        if self._pause_task is None or self._pause_task.done():
            self._pause_task = self._spawn(self._pause())
        self.notify()

    async def _pause(self) -> None:
        async with self._lock:
            resources = self.resources
            if resources is not None:
                route = resources.bridge.state
                try:
                    await self.preferences.set(
                        "relaydeck-route", format_route(route.destination, route.selection)
                    )
                except Exception:  # noqa: BLE001 - keep the current in-memory route
                    self.notice = "Route storage unavailable; current route retained in memory."
                finally:
                    # Failure to persist a public route must never leave streams/writers live.
                    await asyncio.gather(
                        *(entry.session.pause() for entry in resources.retained.entries())
                    )
            if self.lifecycle.phase == AppPhase.BACKGROUND:
                self.lifecycle.apply_pause_policy()

    def request_resume(self) -> None:
        self.foreground = True
        if not self.alive or not self.online or self.input_allowed():
            return
        if self._resume_task is None or self._resume_task.done():
            self._resume_task = self._spawn(self.resume())

    def connectivity_changed(self, links: list[Any]) -> None:
        current = tuple(sorted(str(getattr(link, "value", link)) for link in links))
        if current == self._links:
            return
        initial, self._links = self._links is None, current
        self.online = bool(current) and any(link != "none" for link in current)
        if initial and self.online:
            return
        self._suspend()
        if self.foreground and self.online:
            self.request_resume()

    async def resume(self) -> None:
        if self._pause_task is not None:
            await self._pause_task
        async with self._lock:
            if not self.alive or not self.foreground or not self.online:
                return
            resources, auth = self.resources, self.auth
            if auth is None:
                return
            if resources is None:
                if auth.snapshot.state == AuthState.SIGNED_IN:
                    await self._connect()
                return
            self.interactive = False
            self.phase = "resuming"
            self.notify()
            generation = self.generation

            def live() -> None:
                if (
                    not self.alive
                    or not self.foreground
                    or not self.online
                    or generation != self.generation
                    or self.resources is not resources
                ):
                    raise RuntimeError("Resume superseded")

            await resources.commands.load()
            pending = {
                repr((host, command_id)): (host, command_id)
                for host, command_id in resources.commands.records
            }
            snapshots: dict[str, dict[str, Any]] = {}

            async def identity() -> dict[str, Any]:
                snapshot = await auth.refresh()
                live()
                if snapshot.state != AuthState.SIGNED_IN:
                    raise RuntimeError("Identity no longer valid")
                me = await resources.transport.me()
                live()
                if (
                    me["account"] != resources.props["account"]
                    or me["device_id"] != resources.props["session_factory"].device_id
                ):
                    await resources.commands.close()
                    await auth.logout()
                    raise RuntimeError("Identity changed")
                hosts = await resources.transport.hosts()
                live()
                resources.controller.apply_grants(hosts["hosts"])
                await resources.commands.load()
                for host, command_id in resources.commands.records:
                    if host in (resources.controller.state.granted_hosts or frozenset()):
                        pending[repr((host, command_id))] = (host, command_id)
                return me

            async def query(command_id: str) -> dict[str, Any] | None:
                host, actual_id = pending[command_id]
                if host not in (resources.controller.state.granted_hosts or frozenset()):
                    return None
                doc = await resources.transport.command_status(host, actual_id)
                live()
                return doc

            async def fresh() -> dict[str, Any] | None:
                live()
                granted = resources.controller.state.granted_hosts or frozenset()
                active = resources.retained.foreground
                host = active.host_id if active is not None else resources.props["host_id"]
                host = host if host in granted else next(iter(sorted(granted)), "")
                resources.props["host_id"] = host
                for host_id in sorted(
                    {host} | {entry.host_id for entry in resources.retained.entries()}
                ):
                    if not host_id or host_id not in granted:
                        continue
                    doc = await resources.transport.snapshot(host_id)
                    live()
                    resources.controller.accept_snapshot(host_id, doc)
                    snapshots[host_id] = doc
                state = resources.controller.state
                await resources.retained.prune(
                    {item.id: item.epoch for item in state.inventory.hosts.values()},
                    {(item.host_id, "agent", key) for key, item in state.inventory.agents.items()}
                    | {(item.host_id, "shell", key) for key, item in state.inventory.tabs.items()},
                )
                live()
                # Empty granted inventory is valid and has no metadata stream to resume.
                return snapshots.get(host)

            async def queue_cursor(cursor: str) -> None:
                live()
                host = resources.props["host_id"]
                if host in snapshots:
                    resources.controller.prepare_resume(host, snapshots[host])

            async def control() -> str | None:
                live()
                active = resources.retained.foreground
                if active is None:
                    return None
                await active.session.resume_after_pause()
                live()
                lease = active.session.lease
                return (
                    lease.lease_id if lease is not None and session_live(active.session) else None
                )

            orchestrator = ResumeOrchestrator(
                refresh_identity=identity,
                query_pending=query,
                fetch_snapshot=fresh,
                reconnect_cursor=queue_cursor,
                reacquire_control=control,
            )
            try:
                route = resources.bridge.state
                stored = str(
                    await self.preferences.get("relaydeck-route")
                    or format_route(route.destination, route.selection)
                )
                self.resume_report = await orchestrator.resume(
                    stored_route=stored, outstanding_command_ids=tuple(pending)
                )
                live()
                route = self.resume_report.route
                resources.bridge.set_route(format_route(route.destination, route.selection))
                if self.lifecycle.phase in {AppPhase.BACKGROUND, AppPhase.PAUSED}:
                    self.lifecycle.on_resume_complete()
                self.phase = "ready"
                self.interactive = True
                self.notice = ""
                self.notify()
                if self.notifications is not None:
                    self._spawn(self.notifications.sync())
                    self._spawn(self.notifications.flush())
            except Exception:  # noqa: BLE001 - no credential/terminal exception in presentation
                if not self.alive or generation != self.generation:
                    return
                self.interactive = False
                if auth.snapshot.state != AuthState.SIGNED_IN:
                    await self._close_resources()
                    self.phase = "login"
                    self.notice = "Sign in again to resume."
                else:
                    self.phase = "error"
                    self.notice = (
                        "Current state unavailable. Resume to retry without replaying input."
                    )
                self.notify()

    async def start(self) -> None:
        if not self.alive:
            return
        from relaydeck.client.features.pairing.service import discard_pending

        if not await self.credentials.available():
            self.phase = "unavailable"
            self.notice = "Secure credential storage is unavailable. Device pairing is disabled."
            self.notify()
            return
        try:
            await discard_pending(self.credentials)
        except Exception:  # noqa: BLE001 - storage failure must not permit pairing
            self.phase = "unavailable"
            self.notice = (
                "Pending pairing could not be cleared securely. Restart before connecting."
            )
            self.notify()
            return
        self.pairing_available = True
        saved = await self.preferences.get("relaydeck-endpoint")
        if isinstance(saved, str) and saved:
            await self.configure(saved)
        else:
            self.notify()

    async def configure(self, endpoint: str) -> None:
        self.pairing.invalidate()
        async with self._lock:
            if not self.alive:
                return
            self.generation += 1
            self.fence_input()
            base = private_endpoint(endpoint)
            origin = await self.credentials.get("identity_origin")
            if origin != base:
                # Credentials must never be sent to a newly selected origin.
                await self.credentials.clear_account()
                if self.notifications is not None:
                    await self.notifications.revoke()
            await self.credentials.set("identity_origin", base)
            await self._close_resources()
            self.endpoint = base
            await self.preferences.set("relaydeck-endpoint", base)
            self.auth = AuthService(
                GatewayConfig(base),
                self.credentials,
                HttpxTransport(self.client).post_json,
                self.open_browser,
            )
            auth = self.auth

            def auth_changed(snapshot: Any) -> None:
                if not self.alive or self.auth is not auth:
                    return
                if snapshot.state in {AuthState.ERROR, AuthState.SIGNED_OUT}:
                    self.fence_input()
                    self.phase = "login"
                    self.notice = snapshot.error or snapshot.notice
                    if self.resources is not None and self.notifications is not None:
                        self._spawn(self.notifications.revoke())
                self.notify()

            auth.subscribe(auth_changed)
            self.phase = "connecting"
            self.notify()
            snapshot = await self.auth.restore()
            if self.pending_callback:
                uri, self.pending_callback = self.pending_callback, ""
                snapshot = await self.auth.complete_callback(uri)
            if snapshot.state == AuthState.SIGNED_IN:
                await self._connect()
            else:
                self.phase = "login"
                self.notice = snapshot.error
                self.notify()

    async def sign_in(self) -> None:
        if not self.auth or not self.alive:
            return
        self.phase = "login"
        await self.credentials.set("identity_origin", self.endpoint)
        await self.auth.begin_login()
        self.notify()

    async def begin_pairing(self, endpoint: str, host_id: str, label: str) -> None:
        if not self.alive or not self.pairing_available or not self.foreground or not self.online:
            return
        message = ""
        try:
            await self.configure(endpoint)
        except EndpointError as exc:
            message = exc.reason  # fixed sentence about the address, never the address itself
        except Exception as exc:  # noqa: BLE001 - not the address: secure storage, network, ...
            logger.warning("Pairing setup failed: category=%s", failure_category(exc))
            message = PAIRING_SETUP_FAILED
        if message:
            from dataclasses import replace

            self.pairing.emit(
                replace(self.pairing.snapshot, phase="failed", busy=False, message=message)
            )
            return
        self.pairing_generation = self.generation
        await self.pairing.begin(endpoint, host_id, label)
        if (
            self.pairing.snapshot.phase == "ready"
            and self.alive
            and self.auth is not None
            and self.auth.snapshot.state == AuthState.SIGNED_IN
        ):
            await self._connect()

    async def callback(self, uri: str) -> None:
        if not self.alive:
            return
        if not self.auth:
            self.pending_callback = uri
            return
        async with self._lock:
            if not self.alive:
                return
            previous = self.resources
            if previous is not None and self.auth.snapshot.state == AuthState.SIGNED_IN:
                # A duplicate completed PKCE callback keeps VT/drafts mounted.
                snapshot = await self.auth.complete_callback(uri)
                if snapshot.state == AuthState.SIGNED_IN:
                    return
                await self._close_resources()
                self.phase = "login"
                self.notice = snapshot.error
                self.notify()
                return
            self.fence_input()
            self.phase = "connecting"
            self.notify()
            snapshot = await self.auth.complete_callback(uri)
            if snapshot.state == AuthState.SIGNED_IN:
                await self.credentials.set("identity_origin", self.endpoint)
                await self._connect()
            else:
                self.phase = "login"
                self.notice = snapshot.error
                self.notify()

    async def _connect(self) -> None:
        assert self.auth is not None  # noqa: S101
        generation = self.generation
        token = await self.auth.access_token()
        if not token:
            self.phase = "login"
            self.notify()
            return
        response = await self.client.get(
            self.endpoint + paths.ME, headers={"Authorization": f"Bearer {token}"}
        )
        response.raise_for_status()
        identity = response.json()
        validate("relaydeck/gateway/v1/me", identity)
        known = cast(Me, identity)
        if not self.alive or generation != self.generation:
            return
        await self._close_resources()
        route = parse_route(str(await self.preferences.get("relaydeck-route") or "/"))
        if not self.alive or generation != self.generation:
            return
        host = (
            route.selection
            if route.destination == "settings"
            else unquote(route.selection.partition(":")[0])
            if ":" in route.selection
            else ""
        )
        host = host if host in known["hosts"] else next(iter(known["hosts"]), "")
        auth = self.auth

        async def refresh_identity() -> bool:
            attempt_generation = self.generation
            snapshot = await auth.refresh(require_signed_in=True)
            if attempt_generation != self.generation:
                raise asyncio.CancelledError
            if snapshot.state == AuthState.SIGNED_IN:
                return True
            if await self.credentials.get(REFRESH_KEY):
                raise TransportError("identity_unavailable", "Refresh outcome unavailable", False)
            return False

        resources = build_resources(
            self.page,
            base_url=self.endpoint,
            get_token=self.auth.access_token,
            host_id=host,
            account=identity["account"],
            device_id=identity["device_id"],
            on_logout=self.request_logout,
            client=self.client,
            clipboard=self.clipboard,
            draft_store=ScopedDrafts(self.credentials, identity["account"]),
            native_picture_upload=True,
            command_store=self.credentials,
            command_current=lambda: self.alive
            and self.foreground
            and self.online
            and self.resources is resources
            and self.auth is auth
            and auth.snapshot.state == AuthState.SIGNED_IN,
            command_version=lambda: self.generation,
            refresh_identity=refresh_identity,
            recovery_current=lambda: (
                self.alive
                and self.foreground
                and self.online
                and self.resources is resources
                and self.auth is auth
            ),
            recovery_version=lambda: self.generation,
        )
        resources.bridge.set_route(format_route(route.destination, route.selection))
        resources.props["interaction_guard"] = self.input_allowed
        self.resources = resources
        try:
            hosts = await resources.transport.hosts()
            if not self.alive or generation != self.generation:
                await self._close_resources()
                return
            resources.controller.apply_grants(hosts["hosts"])
            await resources.commands.reconcile(
                resources.transport.command_status,
                resources.controller.state.granted_hosts or frozenset(),
            )
        except Exception:
            # Recovery failure fences relevant command mutations through the journal;
            # otherwise authenticated read-only navigation remains available.
            self.notice = (
                "Pending command recovery unavailable. Check status before another mutation."
            )
        self.interactive = self.foreground and self.online
        self.phase = "ready" if self.interactive else "paused"
        if self.interactive and self.lifecycle.phase in {AppPhase.BACKGROUND, AppPhase.PAUSED}:
            self.lifecycle.on_resume_complete()
        if not resources.commands.error:
            self.notice = ""
        self.notify()
        if self.notifications is not None:
            self._spawn(self.notifications.sync())
            self._spawn(self.notifications.flush())

    def request_logout(self) -> None:
        if not self.alive:
            return
        self.fence_input()
        self._spawn(self.logout())

    def fence_input(self) -> None:
        self.interactive = False
        if self.resources is not None:
            for entry in self.resources.retained.entries():
                entry.session.set_foreground(False)
                controller = entry.session.controller
                if controller is not None:
                    controller.pause_writer()

    async def logout(self) -> None:
        self.pairing.invalidate()
        async with self._lock:
            self.generation += 1
            self.interactive = False
            self.phase = "login"
            self.notify()
            await self._close_resources()
            if self.auth:
                snapshot = await self.auth.logout()
                self.notice = snapshot.notice
            if self.notifications is not None:
                await self.notifications.revoke()
            self.notify()

    async def _close_resources(self) -> None:
        resources, self.resources = self.resources, None
        if resources is not None:
            await resources.close()

    async def close(self) -> None:
        self.alive = False
        self.pairing.invalidate()
        self.fence_input()
        self.generation += 1
        for task in tuple(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        await self._close_resources()
        if self.owns_client:
            await self.client.aclose()
