"""A verified notification tap opens its session, or the host in Settings › Hosts."""

from __future__ import annotations

from types import SimpleNamespace

from relaydeck.client.features.inventory.routing import format_route, parse_route
from relaydeck.client.notifications_runtime import tap_destination

INVENTORY = SimpleNamespace(agents={"home-mac:a1": object()}, tabs={"home-mac:t1": object()})


def test_a_tap_on_an_unknown_session_opens_the_host_in_settings() -> None:
    destination, selection = tap_destination(INVENTORY, "home-mac", "home-mac:gone")
    assert (destination, selection) == ("settings", "home-mac")
    route = format_route(destination, selection)
    assert route == "/settings/hosts/home-mac"
    parsed = parse_route(route)
    assert parsed.destination == "settings" and parsed.section == "hosts"
    assert parsed.selection == "home-mac"


def test_a_tap_on_a_known_agent_or_terminal_keeps_its_destination() -> None:
    assert tap_destination(INVENTORY, "home-mac", "home-mac:a1") == ("agents", "home-mac:a1")
    assert tap_destination(INVENTORY, "home-mac", "home-mac:t1") == ("terminals", "home-mac:t1")
