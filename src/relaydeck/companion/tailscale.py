"""Automatic private (tailnet-only) route for the companion.

Opt-in with `tailscale_auto = true`. The companion asks the local Tailscale
client for this Mac's own DNS name, derives its private HTTPS origin, maps
that HTTPS port to the loopback backend with `tailscale serve`, and removes
the mapping on stop. Each user's companion therefore gets its own address;
nothing is hardcoded.

Never public: Funnel is refused, an HTTPS port that already serves something
else is refused (no silent takeover of another service's mapping), and only
a mapping this process created is ever removed.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Literal

from relaydeck.companion.trusted import probe_environment, trusted_executable

Runner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]

TIMEOUT_S = 15
CANDIDATES = ("tailscale", "/usr/local/bin/tailscale", "/opt/homebrew/bin/tailscale")
APP_BUNDLE_CLI = "/Applications/Tailscale.app/Contents/MacOS/Tailscale"


class TailscaleError(RuntimeError):
    """Tailscale is missing, logged out, or the route would be unsafe."""


def find_cli() -> str:
    for candidate in (*CANDIDATES, APP_BUNDLE_CLI):
        found = trusted_executable(candidate)
        if found:
            return found
    raise TailscaleError(
        "Tailscale CLI not found. Install Tailscale on this Mac and log in, "
        "or set advertised_origin manually."
    )


def default_runner(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed CLI, argument list, no shell
        [find_cli(), *args],
        capture_output=True,
        text=True,
        timeout=TIMEOUT_S,
        check=False,
        env=probe_environment(),
    )


def _json(runner: Runner, args: Sequence[str]) -> dict[str, object]:
    try:
        result = runner(args)
    except (OSError, subprocess.TimeoutExpired) as exc:
        # Redacted: the exception text can carry paths or stderr; only the subcommand is named.
        raise TailscaleError(f"tailscale {args[0]} did not answer") from exc
    if result.returncode != 0:
        raise TailscaleError(f"tailscale {args[0]} failed")
    try:
        doc = json.loads(result.stdout or "{}")
    except ValueError as exc:
        raise TailscaleError("unexpected tailscale output") from exc
    if not isinstance(doc, dict):
        raise TailscaleError("unexpected tailscale output")
    return doc


def serve_status(runner: Runner = default_runner) -> dict[str, object]:
    """`tailscale serve status --json`: the HTTPS mappings and Funnel flags (read-only)."""
    return _json(runner, ("serve", "status", "--json"))


def served_https_ports(status: dict[str, object]) -> set[int]:
    """HTTPS ports that already have a mapping, from a serve status document."""
    web = status.get("Web")
    ports: set[int] = set()
    if isinstance(web, dict):
        for hostport in web:
            tail = str(hostport).rsplit(":", 1)[-1]
            if tail.isdigit():
                ports.add(int(tail))
    return ports


ServeState = Literal["free", "same", "conflict", "public", "unknown"]


def _port(key: object, *, hostport: bool) -> int | None:
    if not isinstance(key, str):
        return None
    if hostport:
        host, separator, key = key.rpartition(":")
        if not separator or not host:
            return None
    return (
        int(key)
        if 1 <= len(key) <= 5 and key.isascii() and key.isdecimal() and 1 <= int(key) <= 65535
        else None
    )


def inspect_serve(
    doc: dict[str, object],
    https_port: int,
    expected_target: str | None,
    *,
    foreground: bool = False,
) -> ServeState:
    """Classify only this port; never expose other handlers/targets in diagnostics.

    ServeConfig TCP/Web/AllowFunnel and Foreground are the upstream JSON fields.
    Only an exact root proxy with HTTPS TCP (when present) is reusable. Extra
    mounts, forwarding and malformed status fail closed. Service virtual IPs are
    separate from this Mac's local Serve port.
    """
    tcp, web, funnel = (doc.get(key, {}) for key in ("TCP", "Web", "AllowFunnel"))
    if not isinstance(tcp, dict) or not isinstance(web, dict) or not isinstance(funnel, dict):
        return "unknown"
    for key, enabled in funnel.items():
        port = _port(key, hostport=True)
        if port is None or type(enabled) is not bool:
            return "unknown"
        if port == https_port and enabled:
            return "public"
    occupied = False
    same = False
    for key, handler in tcp.items():
        port = _port(key, hostport=False)
        if port is None:
            return "unknown"
        if port == https_port:
            if not isinstance(handler, dict):
                return "unknown"
            if handler != {"HTTPS": True} or type(handler.get("HTTPS")) is not bool:
                return "conflict"
            occupied = True
    for key, entry in web.items():
        port = _port(key, hostport=True)
        if port is None:
            return "unknown"
        if port == https_port:
            if not isinstance(entry, dict):
                return "unknown"
            if expected_target is None or entry != {"Handlers": {"/": {"Proxy": expected_target}}}:
                return "conflict"
            same = True
    state: ServeState = "same" if same else "conflict" if occupied else "free"
    if state == "conflict":
        return state
    children = doc.get("Foreground", {})
    if not isinstance(children, dict) or len(children) > 64 or (foreground and children):
        return "unknown"
    for child in children.values():
        if not isinstance(child, dict):
            return "unknown"
        child_state = inspect_serve(child, https_port, expected_target, foreground=True)
        if child_state not in ("free", "same"):
            return child_state
        if child_state == "same":
            state = "same"
    return state


def machine_dns_name(runner: Runner = default_runner) -> str:
    """This Mac's MagicDNS name, e.g. `your-mac.example-tailnet.ts.net`."""
    status = _json(runner, ("status", "--json"))
    if status.get("BackendState") != "Running":
        raise TailscaleError("Tailscale is not connected. Open Tailscale and log in.")
    self_node = status.get("Self")
    name = self_node.get("DNSName") if isinstance(self_node, dict) else None
    if not isinstance(name, str) or not name.strip(".").endswith(".ts.net"):
        raise TailscaleError(
            "MagicDNS name unavailable. Enable MagicDNS and HTTPS for the tailnet."
        )
    return name.strip(".").lower()


def private_origin(https_port: int, runner: Runner = default_runner) -> str:
    host = machine_dns_name(runner)
    return f"https://{host}" if https_port == 443 else f"https://{host}:{https_port}"


@dataclass
class PrivateRoute:
    """Owns at most one tailnet-only HTTPS mapping for one companion run."""

    https_port: int
    local_port: int
    runner: Runner = default_runner
    created: bool = False

    @property
    def target(self) -> str:
        return f"http://127.0.0.1:{self.local_port}"

    def _existing(self) -> tuple[str | None, bool]:
        """(proxy target already mapped at this HTTPS port, funnel allowed)."""
        doc = _json(self.runner, ("serve", "status", "--json"))
        state = inspect_serve(doc, self.https_port, self.target)
        if state == "unknown":
            raise TailscaleError("Tailscale Serve status is invalid; nothing changed.")
        return (
            self.target if state == "same" else None if state == "free" else "<other handlers>",
            state == "public",
        )

    def inspect(self) -> tuple[str | None, bool]:
        """Read-only: (proxy already mapped at this HTTPS port, public via Funnel)."""
        return self._existing()

    def ensure(self) -> None:
        proxy, public = self._existing()
        if public:
            raise TailscaleError(
                f"HTTPS port {self.https_port} is public (Funnel). Refusing: RelayDeck stays "
                f"tailnet-only. Turn it off with: tailscale funnel --https={self.https_port} off"
            )
        if proxy == self.target:
            return  # Already mapped (for example by a previous run); not ours to remove.
        if proxy is not None:
            raise TailscaleError(
                f"HTTPS port {self.https_port} already serves {proxy}. Choose another "
                "tailscale_https_port; the existing mapping was left untouched."
            )
        _ = self._run(("serve", "--bg", f"--https={self.https_port}", self.target))
        self.created = True

    def remove(self) -> None:
        if not self.created:
            return
        self.created = False
        _ = self._run(("serve", f"--https={self.https_port}", "off"))

    def _run(self, args: Sequence[str]) -> str:
        try:
            result = self.runner(args)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise TailscaleError(f"tailscale {args[0]} did not answer") from exc
        if result.returncode != 0:
            raise TailscaleError(f"tailscale {args[0]} failed")
        return result.stdout


__all__ = [
    "PrivateRoute",
    "inspect_serve",
    "TailscaleError",
    "default_runner",
    "machine_dns_name",
    "private_origin",
    "served_https_ports",
    "serve_status",
]
