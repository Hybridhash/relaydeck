"""Packaged push registration and authenticated read-only tap handling."""

from __future__ import annotations

import asyncio
import re
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, parse_qsl, urlencode, urlsplit, urlunsplit

from relaydeck.client.adapters.ports import NotificationPort, PushPermission, PushTap
from relaydeck.client.features.auth.service import AuthState
from relaydeck.client.features.inventory.routing import format_route
from relaydeck.domain.models import scope_key

if TYPE_CHECKING:
    from relaydeck.client.packaged_runtime import PackagedRuntime


def tap_destination(inventory: Any, host: str, key: str) -> tuple[str, str]:
    """Where a verified notification tap opens: the session when it is known, else the host in
    Settings › Hosts (Hosts is a Settings section now)."""
    if key in inventory.agents:
        return "agents", key
    if key in inventory.tabs:
        return "terminals", key
    return "settings", host


# a web push tap opens /?rd_route=<token> once. The token is opaque, device-bound and
# checked by the server; anything malformed, expired or foreign routes nowhere.
WEB_ROUTE_PARAM = "rd_route"
_WEB_ROUTE_TOKEN = re.compile(r"wp_[A-Za-z0-9_-]{20,64}")


def web_route_token(route: str) -> str | None:
    """The web push token in an initial URL, or None when absent, repeated or malformed."""
    values = parse_qs(urlsplit(route).query, keep_blank_values=True).get(WEB_ROUTE_PARAM)
    if not values or len(values) != 1 or not _WEB_ROUTE_TOKEN.fullmatch(values[0]):
        return None
    return values[0]


def without_web_route(route: str) -> str:
    """The same URL with the rd_route parameter removed and every other parameter kept."""
    parts = urlsplit(route)
    kept = [
        (name, value)
        for name, value in parse_qsl(parts.query, keep_blank_values=True)
        if name != WEB_ROUTE_PARAM
    ]
    return urlunsplit(("", "", parts.path or "/", urlencode(kept), ""))


async def open_web_route(resources: Any, token: str, account: str, device_id: str) -> bool:
    """Opens the session a web push tap points at. The same checks as the native tap apply: this
    device's identity, the host grant, a current snapshot, then tap_destination. Any refusal returns
    False and leaves the user on Console; the reason is never shown."""
    try:
        me = await resources.transport.me()
        if me["account"] != account or me["device_id"] != device_id:
            return False
        doc = await resources.transport.resolve_web_route(token)
        host, session = doc["host_id"], doc["session_id"]
        if host not in me["hosts"]:
            return False
        hosts = await resources.transport.hosts()
        snapshot = await resources.transport.snapshot(host)
        resources.controller.apply_grants(hosts["hosts"])
        resources.controller.accept_snapshot(host, snapshot)
        if host not in (resources.controller.state.granted_hosts or ()):
            return False
        inventory = resources.controller.state.inventory
        destination, selection = tap_destination(inventory, host, scope_key(host, session))
        resources.bridge.set_route(format_route(destination, selection))
        return True
    except Exception:  # noqa: BLE001 - a refused or failed tap discloses nothing
        return False


def start_web_tap(page: Any, resources: Any, account: str, device_id: str) -> None:
    """Reads the rd_route token once, from the URL the page opened with, and strips the parameter
    whether or not the token is valid. The route opens after sign-in, through open_web_route."""
    initial = page.route or "/"
    stripped = without_web_route(initial)
    if stripped == initial:
        return
    resources.bridge.set_route(stripped)
    page.go(stripped)
    token = web_route_token(initial)
    if token is None:
        return

    async def open_tap() -> None:
        await open_web_route(resources, token, account, device_id)

    task = page.run_task(open_tap)
    resources.tasks.add(task)
    task.add_done_callback(resources.tasks.discard)


class NotificationRuntime:
    def __init__(self, port: NotificationPort, runtime: PackagedRuntime) -> None:
        self.port = port
        self.runtime = runtime
        self.permission = PushPermission.UNKNOWN
        self.notice = ""
        self.busy = False
        self.generation = 0
        self._token = ""
        self._registered: tuple[str, str, str, str] | None = None
        self._pending: tuple[str, bool, PushTap | None] | None = None
        self._lock = asyncio.Lock()

    def _notify(self) -> None:
        if self.runtime.alive:
            self.runtime.notify()

    def _scope(self) -> Any:
        runtime = self.runtime
        if (
            not runtime.input_allowed()
            or runtime.auth is None
            or runtime.auth.snapshot.state != AuthState.SIGNED_IN
        ):
            return None
        return runtime.resources

    def _live(self, resources: Any, generation: int) -> bool:
        return generation == self.generation and resources is self._scope()

    async def sync(self) -> None:
        async with self._lock:
            resources, generation = self._scope(), self.generation
            if resources is None:
                return
            try:
                self.permission = await self.port.permission()
                if not self._live(resources, generation):
                    return
                if self.permission != PushPermission.GRANTED:
                    self.notice = (
                        "Notifications are off. Open sessions to see current activity."
                        if self.permission == PushPermission.DENIED
                        else "Notifications are not enabled."
                    )
                    self._notify()
                    return
                token = self._token or await self.port.token()
                if not self._live(resources, generation):
                    return
                if not token:
                    self.notice = "Notification provider unavailable; sessions remain usable."
                    self._notify()
                    return
                scope = (
                    self.runtime.endpoint,
                    resources.props["account"],
                    resources.props["session_factory"].device_id,
                    token,
                )
                if self._registered != scope:
                    await resources.transport.register_device(scope[2], token)
                    if not self._live(resources, generation):
                        return
                    self._registered = scope
                    self._token = token
                self.notice = "Notifications enabled; delivery depends on your private gateway."
            except Exception:  # noqa: BLE001 - private token/provider errors never enter UI
                if self._live(resources, generation):
                    self.notice = "Notification registration unavailable; sessions remain usable."
            self._notify()

    async def enable(self) -> None:
        resources, generation = self._scope(), self.generation
        if resources is None or self.busy:
            return
        self.busy = True
        self._notify()
        try:
            permission = await self.port.request_permission()
            if resources is not self.runtime.resources or generation != self.generation:
                return
            self.permission = permission
            if permission == PushPermission.DENIED:
                self.notice = "Notifications are off. Open sessions to see current activity."
            else:
                await self.sync()  # A platform dialog may pause the app; resume syncs later.
        except Exception:  # noqa: BLE001 - capability failure does not disable terminal use
            self.notice = "Notification permission unavailable; sessions remain usable."
        finally:
            self.busy = False
            self._notify()

    async def token_changed(self, token: str, *, generation: int | None = None) -> None:
        if (
            not self.runtime.alive
            or not isinstance(token, str)
            or not 1 <= len(token) <= 4096
            or (generation is not None and generation != self.generation)
            or self.runtime.auth is None
            or self.runtime.auth.snapshot.state != AuthState.SIGNED_IN
        ):
            return
        self._token = token
        await self.sync()

    async def tap(self, tap: PushTap) -> None:
        if (
            not self.runtime.alive
            or re.fullmatch(r"n_[A-Za-z0-9_-]{10,100}", tap.notification_id) is None
            or tap.category not in {"attention", "completion"}
        ):
            return
        self._pending = (tap.notification_id, False, tap)
        await self.flush()

    async def app_link(self, uri: str) -> None:
        parsed = urlsplit(uri)
        if (
            parsed.scheme == "dev.relaydeck.app"
            and parsed.netloc == "oauth"
            or parsed.scheme == "https"
            and parsed.netloc == urlsplit(self.runtime.endpoint).netloc
            and parsed.path == "/app/oauth"
        ):
            await self.runtime.callback(uri)
            return
        origin = urlsplit(self.runtime.endpoint)
        valid_origin = (
            parsed.scheme == "https"
            and parsed.netloc == origin.netloc
            or parsed.scheme == "relaydeck"
            and parsed.netloc == "app"
        )
        token = (
            parsed.path.removeprefix("/app/n/")
            if parsed.scheme == "https"
            else parsed.path.removeprefix("/n/")
        )
        if (
            valid_origin
            and not parsed.query
            and not parsed.fragment
            and re.fullmatch(r"rdl_[A-Za-z0-9_-]{10,100}", token)
        ):
            self._pending = (token, True, None)
            await self.flush()

    async def flush(self) -> None:
        async with self._lock:
            resources, generation = self._scope(), self.generation
            if resources is None or self._pending is None:
                return
            pending = self._pending
            value, link, hint = pending
            try:
                me = await resources.transport.me()
                if not self._live(resources, generation):
                    return
                if (
                    me["account"] != resources.props["account"]
                    or me["device_id"] != resources.props["session_factory"].device_id
                ):
                    raise ValueError("Identity changed")
                doc = await resources.transport.resolve_notification(value, link=link)
                if not self._live(resources, generation):
                    return
                if hint is not None and (
                    doc["notification_id"] != hint.notification_id
                    or doc["event_ref"] != hint.event_ref
                    or doc["category"] != hint.category
                ):
                    raise ValueError("Hint scope mismatch")
                host, session = doc["host_id"], doc["session_id"]
                if host not in me["hosts"]:
                    raise ValueError("Host no longer granted")
                hosts = await resources.transport.hosts()
                snapshot = await resources.transport.snapshot(host)
                if not self._live(resources, generation):
                    return
                resources.controller.apply_grants(hosts["hosts"])
                resources.controller.accept_snapshot(host, snapshot)
                if host not in (resources.controller.state.granted_hosts or ()):
                    raise ValueError("Host no longer granted")
                key = scope_key(host, session)
                inventory = resources.controller.state.inventory
                destination, selection = tap_destination(inventory, host, key)
                resources.bridge.set_route(format_route(destination, selection))
                self.notice = "Opened current session state."
            except Exception:  # noqa: BLE001 - stale/forged/revoked hints disclose no content
                if self._live(resources, generation):
                    self.notice = (
                        "This notification is no longer available. Open sessions for current state."
                    )
            if self._live(resources, generation) and self._pending is pending:
                self._pending = None
            self._notify()

    async def initial(self) -> None:
        generation = self.generation
        try:
            tap = await self.port.initial_tap()
            if tap is not None and self.runtime.alive and generation == self.generation:
                await self.tap(tap)
        except Exception:  # noqa: S110 - missing native capability stays quiet
            pass

    async def revoke(self) -> None:
        self.generation += 1
        self._token = ""
        self._registered = None
        self._pending = None
        try:
            await self.port.revoke()
        except Exception:  # noqa: BLE001 - server logout remains authoritative
            self.notice = "Local notification cleanup unavailable."
