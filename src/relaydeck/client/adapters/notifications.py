"""NotificationPort backed by the relaydeck-notifications extension."""

from __future__ import annotations

from collections.abc import Callable

from relaydeck_notifications.notifications import (
    AppLinkEvent,
    PushTapEvent,
    PushTokenEvent,
    RelayDeckNotifications,
)
from relaydeck_notifications.notifications import tap_payload_dict as _extension_tap_dict
from relaydeck_notifications.payload import parse_push_payload

from relaydeck.client.adapters.ports import (
    NotificationPort,
    PushPermission,
    PushTap,
)


class ExtensionNotifications(NotificationPort):
    def __init__(self, service: RelayDeckNotifications) -> None:
        self._service = service
        self._token_listeners: list[Callable[[str], None]] = []
        self._tap_listeners: list[Callable[[PushTap], None]] = []
        self._app_link_listeners: list[Callable[[str], None]] = []
        service.on_push_token = self._on_token_event
        service.on_push_tap = self._on_tap_event
        service.on_app_link = self._on_app_link_event

    async def permission(self) -> PushPermission:
        return PushPermission(await self._service.permission())

    async def request_permission(self) -> PushPermission:
        granted = await self._service.request_permission()
        return PushPermission.GRANTED if granted else PushPermission.DENIED

    async def token(self) -> str | None:
        return await self._service.get_token()

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
        payload = await self._service.initial_tap()
        if payload is None:
            return None
        return PushTap(
            notification_id=payload.notification_id,
            event_ref=payload.event_ref,
            category=payload.category,
        )

    async def revoke(self) -> None:
        # Call-site obligation (mirrors FakeNotifications.revoke): the
        # logout path must also send the stored device_ref in POST
        # /v1/logout so the server revokes the install subscription.
        await self._service.delete_token()

    def _on_token_event(self, event: PushTokenEvent) -> None:
        for listener in list(self._token_listeners):
            listener(event.token)

    def _on_tap_event(self, event: PushTapEvent) -> None:
        try:
            payload = parse_push_payload(tap_payload_dict(event))
        except ValueError:
            return
        tap = PushTap(
            notification_id=payload.notification_id,
            event_ref=payload.event_ref,
            category=payload.category,
        )
        for listener in list(self._tap_listeners):
            listener(tap)

    def _on_app_link_event(self, event: AppLinkEvent) -> None:
        for listener in list(self._app_link_listeners):
            listener(event.uri)


def tap_payload_dict(event: PushTapEvent) -> dict[str, str]:
    """The extension's guard input for a tap: required keys plus any opaque refs sent."""
    return _extension_tap_dict(event)
