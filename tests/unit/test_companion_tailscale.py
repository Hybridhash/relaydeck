"""Automatic tailnet-only route (companion `tailscale_auto`), with a fake CLI."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from relaydeck.companion.config import load_config
from relaydeck.companion.tailscale import (
    PrivateRoute,
    TailscaleError,
    machine_dns_name,
    private_origin,
)
from tests.support.companion.asgi_companion import config_file


class FakeTailscale:
    def __init__(self, *, running: bool = True, web: Any = None, funnel: Any = None) -> None:
        self.running = running
        self.web = web or {}
        self.funnel = funnel or {}
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(tuple(args))
        if tuple(args[:2]) == ("status", "--json"):
            doc = {
                "BackendState": "Running" if self.running else "NeedsLogin",
                "Self": {"DNSName": "Your-Mac.example-tailnet.ts.net."},
            }
            return subprocess.CompletedProcess(args, 0, json.dumps(doc), "")
        if tuple(args[:3]) == ("serve", "status", "--json"):
            doc = {"Web": self.web, "AllowFunnel": self.funnel}
            return subprocess.CompletedProcess(args, 0, json.dumps(doc), "")
        return subprocess.CompletedProcess(args, 0, "", "")


def test_origin_is_derived_from_this_machine() -> None:
    fake = FakeTailscale()
    assert machine_dns_name(fake) == "your-mac.example-tailnet.ts.net"
    assert private_origin(8445, fake) == "https://your-mac.example-tailnet.ts.net:8445"
    assert private_origin(443, fake) == "https://your-mac.example-tailnet.ts.net"
    with pytest.raises(TailscaleError, match="log in"):
        machine_dns_name(FakeTailscale(running=False))


def test_route_maps_tailnet_only_and_removes_only_its_own() -> None:
    fake = FakeTailscale()
    route = PrivateRoute(8445, 8125, runner=fake)
    route.ensure()
    assert ("serve", "--bg", "--https=8445", "http://127.0.0.1:8125") in fake.calls
    assert not any(call[0] == "funnel" for call in fake.calls)
    route.remove()
    assert fake.calls[-1] == ("serve", "--https=8445", "off")
    # Already mapped to our backend: reused, never removed by this run.
    host = "your-mac.example-tailnet.ts.net:8445"
    reused = FakeTailscale(web={host: {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8125"}}}})
    route = PrivateRoute(8445, 8125, runner=reused)
    route.ensure()
    route.remove()
    assert not any("off" in call for call in reused.calls)


def test_route_refuses_funnel_and_foreign_mappings() -> None:
    host = "your-mac.example-tailnet.ts.net:8445"
    public = FakeTailscale(
        web={host: {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8125"}}}},
        funnel={host: True},
    )
    with pytest.raises(TailscaleError, match="Funnel"):
        PrivateRoute(8445, 8125, runner=public).ensure()
    taken = FakeTailscale(web={host: {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8787"}}}})
    with pytest.raises(TailscaleError, match="already serves"):
        PrivateRoute(8445, 8125, runner=taken).ensure()
    assert not any(call[:2] == ("serve", "--bg") for call in public.calls + taken.calls)


def test_config_derives_origin_only_when_auto(tmp_path: Path) -> None:
    path = config_file(tmp_path, tailscale_auto=True, tailscale_https_port=8445)
    raw = path.read_text().replace(
        'advertised_origin = "https://test-mac.synthetic-tailnet.ts.net"\n', ""
    )
    path.write_text(raw)
    config = load_config(path, resolve_origin=lambda port: f"https://box.tail1.ts.net:{port}")
    assert config.advertised_origin == "https://box.tail1.ts.net:8445"
    assert config.tailscale_auto is True
    # Manual origin still wins and needs no Tailscale.
    manual_dir = tmp_path / "manual"
    manual_dir.mkdir()
    manual = load_config(config_file(manual_dir))
    assert manual.tailscale_auto is False
    with pytest.raises(ValueError, match="tailscale_auto"):
        load_config(config_file(tmp_path, tailscale_auto="yes"))
