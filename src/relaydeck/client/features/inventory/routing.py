"""Route-stored navigation. Pure and framework-free.

Destinations and session selections live in the route so browser
back/forward restores them. Routes carry opaque identity keys only,
never tokens or titles.

Identity rules: agents/terminals selections are host-scoped
keys (domain scope_key form: quoted host + ":" + quoted bare id),
which never contain "/" and round-trip as one route segment. Hosts
selections stay bare host ids (gateway-unique). A bare legacy segment
on agents/terminals simply matches no scoped map entry and resolves
to the safe missing-state — never to another host's session.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from relaydeck.client.theme import DESKTOP, layout_mode

DESTINATIONS = ("agents", "terminals", "usage", "settings")
# Legacy: the Hosts destination is now a Settings section. Its links still resolve here.
SETTINGS_SECTIONS = ("hosts",)
DEFAULT_HEIGHT = 800


@dataclass(frozen=True)
class RouteState:
    destination: str = "agents"
    selection: str = ""
    width: float = 1280
    height: float = DEFAULT_HEIGHT
    section: str = ""  # a Settings section ("hosts"); the selection is then the host id

    @property
    def is_wide(self) -> bool:
        return layout_mode(self.width) == DESKTOP


def parse_route(route: str) -> RouteState:
    parts = [part for part in route.split("/") if part]
    if parts and parts[0] == "hosts":
        # Legacy Hosts links redirect into Settings › Hosts, keeping the host selection.
        host = parts[1] if len(parts) > 1 else ""
        return RouteState(destination="settings", selection=host, section="hosts")
    if not parts or parts[0] not in DESTINATIONS:
        return RouteState()
    destination = parts[0]
    if destination == "settings":
        section = parts[1] if len(parts) > 1 and parts[1] in SETTINGS_SECTIONS else ""
        selection = parts[2] if section and len(parts) > 2 else ""
        return RouteState(destination=destination, selection=selection, section=section)
    selection = parts[1] if len(parts) > 1 else ""
    return RouteState(destination=destination, selection=selection)


def format_route(destination: str, selection: str = "", section: str = "") -> str:
    if destination not in DESTINATIONS:
        destination = "agents"
    if destination == "settings":
        # The only Settings selection is a host, which lives in the Hosts section.
        if selection:
            return f"/settings/hosts/{selection}"
        return f"/settings/{section}" if section in SETTINGS_SECTIONS else "/settings"
    if selection:
        return f"/{destination}/{selection}"
    return f"/{destination}"


class RouteBridge:
    """Mutable route mirror bridging page events into the tree.

    `main()` feeds page.on_route_change/on_resize here; the root
    component subscribes and mirrors the state into use_state. One
    notification per actual change; no loops.
    """

    def __init__(
        self, route: str = "/", width: float = 1280, height: float = DEFAULT_HEIGHT
    ) -> None:
        parsed = parse_route(route)
        self._state = RouteState(
            destination=parsed.destination,
            selection=parsed.selection,
            width=width,
            height=height,
        )
        self._listeners: list[Callable[[RouteState], None]] = []

    @property
    def state(self) -> RouteState:
        return self._state

    def subscribe(self, listener: Callable[[RouteState], None]) -> Callable[[], None]:
        self._listeners.append(listener)

        def unsubscribe() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return unsubscribe

    def _publish(self, state: RouteState) -> bool:
        if state == self._state:
            return False
        self._state = state
        for listener in list(self._listeners):
            listener(state)
        return True

    def set_route(self, route: str) -> bool:
        parsed = parse_route(route)
        return self._publish(
            RouteState(
                destination=parsed.destination,
                selection=parsed.selection,
                width=self._state.width,
                height=self._state.height,
            )
        )

    def set_width(self, width: float) -> bool:
        return self.set_size(width, self._state.height)

    def set_size(self, width: float, height: float) -> bool:
        return self._publish(
            RouteState(
                destination=self._state.destination,
                selection=self._state.selection,
                width=width,
                height=height,
            )
        )

    def set_local(self, destination: str, selection: str, section: str = "") -> bool:
        """Mirror a tree-initiated navigation (already pushed). A Settings selection is a host in
        the Hosts section."""
        if destination not in DESTINATIONS:
            destination = "agents"
        if destination == "settings" and selection:
            section = "hosts"
        return self._publish(
            RouteState(
                destination=destination,
                selection=selection,
                section=section if destination == "settings" else "",
                width=self._state.width,
                height=self._state.height,
            )
        )
