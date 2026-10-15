"""Platform port contracts.

Fake behavior, secure-storage adapter gating, clipboard adapter wiring,
notification guard rails, and identity session flow. Native permission,
token, background-delivery, and deep-link proofs need a real device and
are recorded as blocked.
"""

from __future__ import annotations

import asyncio
from typing import Any

import flet as ft
import pytest
from flet_secure_storage.secure_storage import SecureStorage
from relaydeck_notifications.notifications import RelayDeckNotifications
from relaydeck_notifications.payload import parse_push_payload

from relaydeck.client.adapters.clipboard import FletClipboard
from relaydeck.client.adapters.fake import (
    FakeClipboard,
    FakeCredentialStore,
    FakeIdentitySession,
    FakeLifecycle,
    FakeNotifications,
)
from relaydeck.client.adapters.notifications import ExtensionNotifications
from relaydeck.client.adapters.ports import (
    AuthCompletion,
    LifecycleState,
    PushPermission,
    PushTap,
)
from relaydeck.client.adapters.secure_storage import (
    FletSecureCredentialStore,
    SecureStorageUnavailable,
)


def test_fake_credential_store_round_trip() -> None:
    async def scenario() -> None:
        store = FakeCredentialStore()
        assert await store.available() is True
        assert await store.get("a") is None
        await store.set("a", "1")
        await store.set("b", "2")
        assert await store.get("a") == "1"
        await store.delete("a")
        assert await store.get("a") is None
        await store.clear_account()
        assert await store.get("b") is None

    asyncio.run(scenario())


def test_fake_credential_store_unavailable_raises() -> None:
    async def scenario() -> None:
        store = FakeCredentialStore(available=False)
        assert await store.available() is False
        with pytest.raises(RuntimeError):
            await store.set("a", "1")

    asyncio.run(scenario())


class _ScriptedSecureService:
    """Minimal SecureStorage double (availability + key/value)."""

    def __init__(self, *, available: bool = True) -> None:
        self._available = available
        self.data: dict[str, str] = {}

    async def get_availability(self) -> bool | None:
        return self._available

    async def get(self, key: str, **_: Any) -> str | None:
        return self.data.get(key)

    async def set(self, key: str, value: str, **_: Any) -> None:
        self.data[key] = value

    async def remove(self, key: str, **_: Any) -> None:
        self.data.pop(key, None)

    async def get_all(self, **_: Any) -> dict[str, str]:
        return dict(self.data)


def test_secure_adapter_namespaces_and_clears() -> None:
    async def scenario() -> None:
        service = _ScriptedSecureService()
        store = FletSecureCredentialStore(service)
        assert await store.available() is True
        await store.set("refresh_token", "rt-1")
        assert service.data == {"relaydeck.refresh_token": "rt-1"}
        assert await store.get("refresh_token") == "rt-1"
        service.data["other.key"] = "untouched"
        await store.clear_account()
        assert service.data == {"other.key": "untouched"}
        with pytest.raises(ValueError):
            await store.set("../../etc", "x")

    asyncio.run(scenario())


def test_secure_adapter_fails_closed_when_unavailable() -> None:
    async def scenario() -> None:
        service = _ScriptedSecureService(available=False)
        store = FletSecureCredentialStore(service)
        assert await store.available() is False
        with pytest.raises(SecureStorageUnavailable):
            await store.get("a")
        with pytest.raises(SecureStorageUnavailable):
            await store.set("a", "1")
        with pytest.raises(SecureStorageUnavailable):
            await store.clear_account()

    asyncio.run(scenario())


def test_secure_storage_control_constructs() -> None:
    service = SecureStorage()
    assert service is not None


def test_fake_clipboard_counts_access() -> None:
    async def scenario() -> None:
        clip = FakeClipboard("hello")
        assert await clip.get_text() == "hello"
        assert clip.reads == 1
        await clip.set_text("world")
        assert clip.writes == ["world"]

    asyncio.run(scenario())


def test_flet_clipboard_adapter_delegates() -> None:
    async def scenario() -> None:
        calls: list[tuple[str, Any]] = []

        class _Service:
            async def _invoke_method(self, name: str, args: Any = None, timeout: Any = None) -> str:
                calls.append((name, None))
                return "v"

            async def set(self, value: str) -> None:
                calls.append(("set", value))

        port = FletClipboard(_Service())  # type: ignore[arg-type]
        assert await port.get_text() == "v"
        await port.set_text("w")
        assert calls == [("get", None), ("set", "w")]

    asyncio.run(scenario())


def test_clipboard_service_exists_in_core() -> None:
    assert hasattr(ft, "Clipboard")


def test_fake_lifecycle_transitions() -> None:
    port = FakeLifecycle()
    seen: list[LifecycleState] = []
    port.on_change(seen.append)
    assert port.current() == LifecycleState.FOREGROUND
    port.move_to(LifecycleState.BACKGROUND)
    port.move_to(LifecycleState.FOREGROUND)
    assert seen == [LifecycleState.BACKGROUND, LifecycleState.FOREGROUND]


def test_push_payload_guard() -> None:
    good = parse_push_payload(
        {
            "notification_id": "n-1",
            "event_ref": "opaque-abc",
            "category": "attention",
            "created_at": "2026-10-03T00:00:00Z",
        }
    )
    assert good.event_ref == "opaque-abc"
    with pytest.raises(ValueError):
        parse_push_payload(
            {
                "notification_id": "n",
                "event_ref": "e",
                "category": "attention",
                "transcript": "rm -rf",
            }
        )
    with pytest.raises(ValueError):
        parse_push_payload({"notification_id": "n", "event_ref": "e", "category": "approve"})
    with pytest.raises(ValueError):
        parse_push_payload({"notification_id": "n", "category": "attention"})
    with pytest.raises(ValueError):
        parse_push_payload(
            {"notification_id": "n", "event_ref": "x" * 600, "category": "attention"}
        )


def test_fake_notifications_permission_denied_stays_usable() -> None:
    async def scenario() -> None:
        port = FakeNotifications(permission=PushPermission.DENIED)
        assert await port.request_permission() == PushPermission.DENIED
        assert await port.token() is None
        taps: list[PushTap] = []
        port.on_tap(taps.append)
        port.tap(PushTap("n", "e", "attention"))
        assert taps == [PushTap("n", "e", "attention")]

    asyncio.run(scenario())


def test_fake_notifications_token_rotation() -> None:
    async def scenario() -> None:
        port = FakeNotifications()
        assert await port.request_permission() == PushPermission.GRANTED
        assert await port.token() == "fake-fcm-token-1"
        seen: list[str] = []
        port.on_token_refresh(seen.append)
        port.rotate_token("fake-fcm-token-2")
        assert seen == ["fake-fcm-token-2"]
        assert await port.token() == "fake-fcm-token-2"

    asyncio.run(scenario())


def test_extension_notifications_guards_taps() -> None:
    service = RelayDeckNotifications()
    port = ExtensionNotifications(service)
    taps: list[PushTap] = []
    port.on_tap(taps.append)
    service._test_event(
        "push_tap",
        {"notification_id": "n-9", "event_ref": "opaque-9", "category": "completion"},
    )
    assert taps == [PushTap("n-9", "opaque-9", "completion")]
    service._test_event(
        "push_tap", {"notification_id": "n-x", "event_ref": "", "category": "attention"}
    )
    assert len(taps) == 1
    links: list[str] = []
    port.on_app_link(links.append)
    service._test_event("app_link", {"uri": "dev.relaydeck.app://oauth?code=c1"})
    assert links == ["dev.relaydeck.app://oauth?code=c1"]


def test_notification_event_annotations_resolve_live() -> None:
    """Flet 1.0.3 resolves event annotations from raw `__annotations__`
    at live render; a PEP 563 string annotation crashes with
    `[resolve error] ... tuple index out of range` (same shape as the
    terminal extension's `on_data_channel_open` fix). Proven red with
    the future import present, green without."""
    from flet.controls.control_event import get_event_field_type

    service = RelayDeckNotifications()
    assert get_event_field_type(service, "on_push_tap").__name__ == "PushTapEvent"
    assert get_event_field_type(service, "on_push_token").__name__ == "PushTokenEvent"
    assert get_event_field_type(service, "on_app_link").__name__ == "AppLinkEvent"


def test_fake_identity_session_flow() -> None:
    async def scenario() -> None:
        session = FakeIdentitySession()
        url = await session.authorization_url()
        assert url.startswith("https://")
        assert await session.access_token() is None
        await session.complete_callback(AuthCompletion(state="s", code="c1"))
        assert await session.access_token() == "token-for-c1"
        await session.complete_callback(AuthCompletion(state="s", error="access_denied"))
        assert await session.access_token() == "token-for-c1"
        await session.logout()
        assert await session.access_token() is None

    asyncio.run(scenario())
