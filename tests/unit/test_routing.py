"""route parsing, formatting and the route bridge."""

from __future__ import annotations

from relaydeck.client.features.inventory.routing import (
    RouteBridge,
    RouteState,
    format_route,
    parse_route,
)


def test_parse_route() -> None:
    assert parse_route("/") == RouteState("agents", "", 1280)
    assert parse_route("/agents") == RouteState("agents", "", 1280)
    assert parse_route("/agents/term-1") == RouteState("agents", "term-1", 1280)
    assert parse_route("/terminals/tab-2") == RouteState("terminals", "tab-2", 1280)
    assert parse_route("/usage") == RouteState("usage", "", 1280)
    # Legacy Hosts links redirect into Settings › Hosts and keep the host selection.
    assert parse_route("/hosts/home-mac") == RouteState(
        "settings", "home-mac", 1280, section="hosts"
    )
    assert parse_route("/hosts") == RouteState("settings", "", 1280, section="hosts")
    assert parse_route("/settings/hosts/home-mac") == RouteState(
        "settings", "home-mac", 1280, section="hosts"
    )
    assert parse_route("/settings/x") == RouteState("settings", "", 1280)
    assert parse_route("/nope/term-1") == RouteState("agents", "", 1280)
    assert parse_route("") == RouteState("agents", "", 1280)


def test_format_route() -> None:
    assert format_route("agents", "term-1") == "/agents/term-1"
    assert format_route("agents") == "/agents"
    assert format_route("settings", "x") == "/settings/hosts/x"  # a Settings selection is a host
    assert format_route("nope", "x") == "/agents/x"


def test_wide_breakpoint() -> None:
    assert RouteState("agents", "", 360).is_wide is False
    assert RouteState("agents", "", 600).is_wide is False
    assert RouteState("agents", "", 601).is_wide is False
    assert RouteState("agents", "", 767).is_wide is False
    assert RouteState("agents", "", 768).is_wide is False
    assert RouteState("agents", "", 1099).is_wide is False
    assert RouteState("agents", "", 1100).is_wide is True
    assert RouteState("agents", "", 1280).is_wide is True


def test_bridge_notifies_once_per_change() -> None:
    bridge = RouteBridge(route="/agents", width=360)
    seen: list[RouteState] = []
    bridge.subscribe(seen.append)
    assert bridge.set_route("/agents") is False
    assert seen == []
    assert bridge.set_route("/agents/term-1") is True
    assert bridge.set_width(360) is False
    assert bridge.set_width(1280) is True
    assert [(s.destination, s.selection, s.width) for s in seen] == [
        ("agents", "term-1", 360),
        ("agents", "term-1", 1280),
    ]
    assert bridge.set_local("settings", "home-mac", "hosts") is True
    assert bridge.state.selection == "home-mac"


def test_bridge_tracks_viewport_size() -> None:
    bridge = RouteBridge(route="/agents", width=1280)
    assert bridge.state.height == 800
    assert bridge.set_size(500, 700) is True
    assert (bridge.state.width, bridge.state.height) == (500, 700)
    assert bridge.set_size(500, 700) is False
    assert bridge.set_width(1280) is True
    assert bridge.state.height == 700


def test_hosts_is_a_settings_section_and_usage_is_a_destination() -> None:
    from relaydeck.client.features.inventory.routing import DESTINATIONS

    assert DESTINATIONS == ("agents", "terminals", "usage", "settings")
    assert format_route("settings", "home-mac", "hosts") == "/settings/hosts/home-mac"
    assert format_route("settings", "", "hosts") == "/settings/hosts"
    assert format_route("usage") == "/usage"
    assert format_route("hosts", "x") == "/agents/x"  # no longer a destination
