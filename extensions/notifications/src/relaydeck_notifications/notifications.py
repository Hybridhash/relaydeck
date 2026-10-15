"""Python side of the RelayDeck notifications extension service.

Native calls ride `_invoke_method`; native occurrences (token refresh,
notification tap, app-link open) arrive as control events that the app
subscribes to in AppRoot wiring. Payloads are guarded by
`parse_push_payload` before any app callback runs.

No `from __future__ import annotations` in this module: Flet 1.0.3's
`get_event_field_type` reads raw `__annotations__` and only unwraps
`ForwardRef`, so a PEP 563 string annotation on an `on_*` handler
crashes live rendering with `[resolve error] ... tuple index out of
range` (same shape as the terminal extension's `on_data_channel_open`
fix). Every annotation here must evaluate at def time (py>=3.13).
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import flet as ft
from flet.controls.services.service import Service

from relaydeck_notifications.payload import PushPayload, parse_push_payload


@dataclass
class PushTapEvent(ft.Event["RelayDeckNotifications"]):
    notification_id: str = ""
    event_ref: str = ""
    category: str = ""
    # full hint refs when the forwarder sends them
    # (older Dart builds send the 3-key subset; both validate).
    device_ref: str = ""
    host_ref: str = ""
    session_ref: str = ""


@dataclass
class PushTokenEvent(ft.Event["RelayDeckNotifications"]):
    token: str = ""


@dataclass
class AppLinkEvent(ft.Event["RelayDeckNotifications"]):
    uri: str = ""


def tap_payload_dict(event: PushTapEvent) -> dict[str, str]:
    """Full hint dict for the guard: required keys plus any
    opaque refs the forwarder sent (empty refs are omitted so older
    3-key forwarders validate unchanged)."""
    data = {
        "notification_id": event.notification_id,
        "event_ref": event.event_ref,
        "category": event.category,
    }
    if event.device_ref:
        data["device_ref"] = event.device_ref
    if event.host_ref:
        data["host_ref"] = event.host_ref
    if event.session_ref:
        data["session_ref"] = event.session_ref
    return data


@ft.control("RelayDeckNotifications")  # type: ignore[arg-type]
class RelayDeckNotifications(Service):
    on_push_tap: ft.EventHandler[PushTapEvent] | None = None
    on_push_token: ft.EventHandler[PushTokenEvent] | None = None
    on_app_link: ft.EventHandler[AppLinkEvent] | None = None

    async def request_permission(self) -> bool:
        return bool(await self._invoke_method("request_permission"))

    async def permission(self) -> str:
        value = await self._invoke_method("permission")
        return str(value) if value in {"granted", "denied", "unknown"} else "unknown"

    async def get_token(self) -> str | None:
        token = await self._invoke_method("get_token")
        return str(token) if token else None

    async def delete_token(self) -> None:
        await self._invoke_method("delete_token")

    async def initial_tap(self) -> PushPayload | None:
        """Tap that launched a killed app, if any (getInitialMessage)."""
        data = await self._invoke_method("initial_tap")
        if not data:
            return None
        return parse_push_payload({k: str(v) for k, v in dict(data).items()})

    def add_tap_listener(self, listener: Callable[[PushPayload], None]) -> None:
        def guarded(event: PushTapEvent) -> None:
            try:
                payload = parse_push_payload(tap_payload_dict(event))
            except ValueError:
                return
            listener(payload)

        self.on_push_tap = guarded

    def _test_event(self, name: str, data: dict[str, Any]) -> None:
        """Test seam: build the event object the Dart side would send."""
        if name == "push_tap":
            event: Any = PushTapEvent(
                name=name,
                control=self,
                notification_id=data.get("notification_id", ""),
                event_ref=data.get("event_ref", ""),
                category=data.get("category", ""),
                device_ref=data.get("device_ref", ""),
                host_ref=data.get("host_ref", ""),
                session_ref=data.get("session_ref", ""),
            )
        elif name == "push_token":
            event = PushTokenEvent(name=name, control=self, token=data.get("token", ""))
        else:
            event = AppLinkEvent(name=name, control=self, uri=data.get("uri", ""))
        handler = {
            "push_tap": self.on_push_tap,
            "push_token": self.on_push_token,
            "app_link": self.on_app_link,
        }[name]
        if handler is not None:
            any_handler: Any = handler
            any_handler(event)
