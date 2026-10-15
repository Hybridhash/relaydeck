"""Platform port interfaces.

One interface per platform capability with web, Android, and fake
implementations. Domain code never touches these; UI code depends only on
the ports. Notifications/clipboard/lifecycle/credentials stay behind the
same seam on every platform.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum


class CredentialStore(ABC):
    """Secure key-value storage. No plaintext fallback: when secure
    storage is unavailable the adapter raises instead of downgrading."""

    @abstractmethod
    async def available(self) -> bool: ...

    @abstractmethod
    async def get(self, key: str) -> str | None: ...

    @abstractmethod
    async def set(self, key: str, value: str) -> None: ...

    @abstractmethod
    async def delete(self, key: str) -> None: ...

    @abstractmethod
    async def clear_account(self) -> None:
        """Remove every key owned by this app (logout/revoke cleanup)."""


class ClipboardPort(ABC):
    @abstractmethod
    async def get_text(self) -> str | None:
        """Read clipboard text. Called only from explicit user actions;
        never polled in the background."""

    @abstractmethod
    async def set_text(self, value: str) -> None: ...


class LifecycleState(Enum):
    FOREGROUND = "foreground"
    BACKGROUND = "background"


class LifecyclePort(ABC):
    @abstractmethod
    def current(self) -> LifecycleState: ...

    @abstractmethod
    def on_change(self, listener: Callable[[LifecycleState], None]) -> None: ...


class ConnectivityState(Enum):
    """Link-layer connectivity. A change is a signal to resync,
    never a state to display as healthy sync."""

    WIFI = "wifi"
    MOBILE = "mobile"
    OFFLINE = "offline"


class ConnectivityPort(ABC):
    """Network-link signal. The lifecycle layer subscribes and requests an
    explicit transport resync on every change (half-open sockets would
    otherwise not error promptly on their own)."""

    @abstractmethod
    def current(self) -> ConnectivityState: ...

    @abstractmethod
    def on_change(self, listener: Callable[[ConnectivityState], None]) -> None: ...


@dataclass(frozen=True)
class PushTap:
    """Opaque notification tap reference. Contains no transcript, path,
    command, secret, or decision."""

    notification_id: str
    event_ref: str
    category: str


class PushPermission(Enum):
    GRANTED = "granted"
    DENIED = "denied"
    UNKNOWN = "unknown"


class NotificationPort(ABC):
    @abstractmethod
    async def permission(self) -> PushPermission: ...

    @abstractmethod
    async def request_permission(self) -> PushPermission:
        """Ask once; denial leaves the app usable."""

    @abstractmethod
    async def token(self) -> str | None:
        """Current provider token, or None when unavailable/denied."""

    @abstractmethod
    async def revoke(self) -> None:
        """Delete the local provider token; server logout revokes its subscription."""

    @abstractmethod
    def on_token_refresh(self, listener: Callable[[str], None]) -> Callable[[], None]: ...

    @abstractmethod
    def on_tap(self, listener: Callable[[PushTap], None]) -> Callable[[], None]:
        """Foreground/background tap delivery. Tapping never executes a
        command; the app resolves the opaque ref after login."""

    @abstractmethod
    def on_app_link(self, listener: Callable[[str], None]) -> Callable[[], None]: ...

    @abstractmethod
    async def initial_tap(self) -> PushTap | None: ...


@dataclass(frozen=True)
class AuthCompletion:
    state: str
    code: str | None = None
    error: str | None = None
    error_description: str | None = None


class IdentitySession(ABC):
    """Gateway-mediated OAuth (RFC 8252): system browser opens the gateway
    authorize URL; the provider redirects to the gateway; the gateway
    redirects to the app link carrying a one-time code; the app exchanges
    the code with PKCE verifier from secure storage."""

    @abstractmethod
    async def authorization_url(self) -> str: ...

    @abstractmethod
    async def complete_callback(self, completion: AuthCompletion) -> None: ...

    @abstractmethod
    async def access_token(self) -> str | None: ...

    @abstractmethod
    async def logout(self) -> None: ...


@dataclass
class FakeClock:
    now_ms: int = 0

    def advance(self, ms: int) -> None:
        self.now_ms += ms
