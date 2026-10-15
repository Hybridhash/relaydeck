"""Deterministic fake port implementations for tests and development.

Scriptable permission/token/tap behavior with a fake clock. Never ships
as a production backend: the app wires real adapters in AppRoot.
"""

from __future__ import annotations

from collections.abc import Callable

from relaydeck.client.adapters.ports import (
    AuthCompletion,
    ClipboardPort,
    ConnectivityPort,
    ConnectivityState,
    CredentialStore,
    IdentitySession,
    LifecyclePort,
    LifecycleState,
    NotificationPort,
    PushPermission,
    PushTap,
)


class FakeCredentialStore(CredentialStore):
    def __init__(self, *, available: bool = True) -> None:
        self._available = available
        self._data: dict[str, str] = {}

    async def available(self) -> bool:
        return self._available

    async def get(self, key: str) -> str | None:
        return self._data.get(key)

    async def set(self, key: str, value: str) -> None:
        if not self._available:
            raise RuntimeError("secure storage unavailable")
        self._data[key] = value

    async def delete(self, key: str) -> None:
        self._data.pop(key, None)

    async def clear_account(self) -> None:
        self._data.clear()


class FakeClipboard(ClipboardPort):
    def __init__(self, text: str | None = None) -> None:
        self.text = text
        self.reads = 0
        self.writes: list[str] = []

    async def get_text(self) -> str | None:
        self.reads += 1
        return self.text

    async def set_text(self, value: str) -> None:
        self.writes.append(value)
        self.text = value


class FakeLifecycle(LifecyclePort):
    def __init__(self) -> None:
        self._state = LifecycleState.FOREGROUND
        self._listeners: list[Callable[[LifecycleState], None]] = []

    def current(self) -> LifecycleState:
        return self._state

    def on_change(self, listener: Callable[[LifecycleState], None]) -> None:
        self._listeners.append(listener)

    def move_to(self, state: LifecycleState) -> None:
        self._state = state
        for listener in list(self._listeners):
            listener(state)


class FakeConnectivity(ConnectivityPort):
    """Scriptable link signal. `switch()` models Wi-Fi/mobile/offline
    changes, including a mid-stream switch."""

    def __init__(self, state: ConnectivityState = ConnectivityState.WIFI) -> None:
        self._state = state
        self._listeners: list[Callable[[ConnectivityState], None]] = []

    def current(self) -> ConnectivityState:
        return self._state

    def on_change(self, listener: Callable[[ConnectivityState], None]) -> None:
        self._listeners.append(listener)

    def switch(self, state: ConnectivityState) -> None:
        self._state = state
        for listener in list(self._listeners):
            listener(state)


class FakeNotifications(NotificationPort):
    def __init__(
        self,
        *,
        permission: PushPermission = PushPermission.UNKNOWN,
        token: str | None = "fake-fcm-token-1",
    ) -> None:
        self._permission = permission
        self._token = token
        self._token_listeners: list[Callable[[str], None]] = []
        self._tap_listeners: list[Callable[[PushTap], None]] = []
        self._app_link_listeners: list[Callable[[str], None]] = []
        self._initial_tap: PushTap | None = None
        self.permission_requests = 0
        self.revokes = 0

    async def permission(self) -> PushPermission:
        return self._permission

    async def request_permission(self) -> PushPermission:
        self.permission_requests += 1
        if self._permission == PushPermission.UNKNOWN:
            self._permission = PushPermission.GRANTED
        return self._permission

    async def token(self) -> str | None:
        if self._permission != PushPermission.GRANTED:
            return None
        return self._token

    def on_token_refresh(self, listener: Callable[[str], None]) -> Callable[[], None]:
        self._token_listeners.append(listener)
        return (
            lambda: self._token_listeners.remove(listener)
            if listener in self._token_listeners
            else None
        )

    def on_tap(self, listener: Callable[[PushTap], None]) -> Callable[[], None]:
        self._tap_listeners.append(listener)
        return (
            lambda: self._tap_listeners.remove(listener)
            if listener in self._tap_listeners
            else None
        )

    def on_app_link(self, listener: Callable[[str], None]) -> Callable[[], None]:
        self._app_link_listeners.append(listener)
        return (
            lambda: self._app_link_listeners.remove(listener)
            if listener in self._app_link_listeners
            else None
        )

    async def initial_tap(self) -> PushTap | None:
        """Tap that launched a killed app (getInitialMessage)."""
        return self._initial_tap

    async def revoke(self) -> None:
        """Logout cleanup: drop the local token association (the server
        row is revoked separately via DELETE /v1/devices).

        Call-site obligation: the logout path must also send the stored
        device_ref as {"device_ref": ...} in POST /v1/logout so the
        server revokes the install subscription even when its id differs
        from the session device id. This fake models no HTTP logout, so
        that plumbing lives entirely at the logout call site.
        """
        self.revokes += 1
        self._token = None

    def rotate_token(self, token: str) -> None:
        self._token = token
        for listener in list(self._token_listeners):
            listener(token)

    def tap(self, tap: PushTap) -> None:
        for listener in list(self._tap_listeners):
            listener(tap)

    def open_link(self, uri: str) -> None:
        for listener in list(self._app_link_listeners):
            listener(uri)

    def set_initial_tap(self, tap: PushTap | None) -> None:
        self._initial_tap = tap


class FakeIdentitySession(IdentitySession):
    def __init__(self) -> None:
        self._token: str | None = None
        self.completions: list[AuthCompletion] = []
        self.logouts = 0

    async def authorization_url(self) -> str:
        return "https://gateway.example/authorize?client=relaydeck-android"

    async def complete_callback(self, completion: AuthCompletion) -> None:
        self.completions.append(completion)
        if completion.code and not completion.error:
            self._token = f"token-for-{completion.code}"

    async def access_token(self) -> str | None:
        return self._token

    async def logout(self) -> None:
        self.logouts += 1
        self._token = None
