"""Read-only setup prerequisites.

Run before `relaydeck setup` writes anything, and by `relaydeck-companion doctor`, so both show the
same results. A check only reads: it never installs, logs in, changes Serve, requests a certificate,
starts a server or updates a tool. Every line is built from fixed guidance text, a version string or
a port number, so no auth URL, user, tailnet name, host name or raw stderr can reach the output.

States: ok | missing | not_running | not_logged_in | needs_action | unknown. Anything other than ok
blocks setup (`--yes` never bypasses this). Usage-root problems are warnings only.
"""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
import sys
from collections.abc import Callable, Coroutine, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from relaydeck.companion import tailscale
from relaydeck.companion.trusted import probe_environment, trusted_executable
from relaydeck.connector.herdr import DEFAULT_SOCKET, HerdrError, HerdrProtocolError, resolve_socket

STATE_OK = "ok"
STATE_MISSING = "missing"
STATE_NOT_RUNNING = "not_running"
STATE_NOT_LOGGED_IN = "not_logged_in"
STATE_NEEDS_ACTION = "needs_action"
STATE_UNKNOWN = "unknown"

TIMEOUT_S = 15
UV_VERSION = re.compile(r"\buv (\d+\.\d+\.\d+)")
HERDR_VERSION = re.compile(r"\b(\d+\.\d+\.\d+)\b")
PYTHON_MIN = (3, 13)
PYTHON_BELOW = (3, 14)

# One fixed guidance line per cause. Nothing here is built from runtime data.
GUIDANCE: dict[str, str] = {
    "tailscale_missing": "Install Tailscale from https://tailscale.com/download/mac, then open it.",
    "tailscale_stopped": "Start Tailscale on this Mac, then re-run setup.",
    "tailscale_login": "Open Tailscale and log in.",
    "tailscale_approval": "Ask the tailnet admin to approve this Mac.",
    "tailscale_dns": "Enable MagicDNS and HTTPS certificates for the tailnet.",
    "tailscale_unknown": (
        "Tailscale did not answer clearly. Check it is running, then re-run setup."
    ),
    "serve_funnel": (
        "Public Funnel is on for the target HTTPS port. Turn it off, then re-run setup."
    ),
    "serve_unknown": "Tailscale Serve did not answer clearly. Re-run setup.",
    "serve_after_tailscale": "Fix the Tailscale check first, then re-run setup.",
    "serve_conflict": (
        "The configured HTTPS port is occupied. Explicitly change tailscale_https_port in the "
        "config to a free private port, then re-run setup; no alternative port was selected."
    ),
    "herdr_missing": "Install Herdr, start it, then re-run setup.",
    "herdr_down": "Start Herdr separately, then re-run setup.",
    "herdr_protocol": "Update Herdr or the companion so both speak the same Herdr protocol.",
    "herdr_unknown": "Herdr did not answer clearly. Check its socket, then re-run setup.",
    "uv_missing": (
        "Install uv (https://docs.astral.sh/uv/getting-started/installation/), then re-run."
    ),
    "uv_unknown": "uv did not report a version. Check uv on PATH.",
    "python": (
        "Use Python 3.13 in a uv tool environment: uv tool install --managed-python --python 3.13."
    ),
    "ok": "",
}


class _Unknown(Exception):
    """Internal: a probe answered in a way that cannot be classified."""


@dataclass(frozen=True)
class Result:
    check: str
    state: str
    guidance: str = ""
    detail: str = ""  # content-free: a version or a port number

    def line(self) -> str:
        text = f"{self.check}: {self.state}"
        if self.detail:
            text += f" ({self.detail})"
        if self.state != STATE_OK and self.guidance:
            text += f" - {self.guidance}"
        return text

    def as_doc(self) -> dict[str, str]:
        return {
            "check": self.check,
            "state": self.state,
            "guidance": "" if self.state == STATE_OK else self.guidance,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class Report:
    results: tuple[Result, ...]
    warnings: tuple[str, ...] = ()

    @property
    def ready(self) -> bool:
        return all(result.state == STATE_OK for result in self.results)

    def lines(self) -> tuple[str, ...]:
        return (
            *(result.line() for result in self.results),
            *(f"warning: {w}" for w in self.warnings),
        )

    def as_doc(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "checks": [result.as_doc() for result in self.results],
            "warnings": list(self.warnings),
        }


Runner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]
Ping = Callable[[str], Coroutine[Any, Any, None]]


def _normal(name: object) -> str:
    return str(name).strip().rstrip(".").lower()


def _run_fixed(program: str, args: Sequence[str]) -> subprocess.CompletedProcess[str]:
    binary = trusted_executable(program)
    if binary is None:
        raise FileNotFoundError("No trusted executable is available")
    return subprocess.run(  # noqa: S603 - fixed program, argument list, no shell
        [binary, *args],
        capture_output=True,
        text=True,
        timeout=TIMEOUT_S,
        check=False,
        env=probe_environment(),
    )


async def _default_ping(socket: str) -> None:
    from relaydeck.connector.herdr import HerdrAdapter

    await HerdrAdapter(socket, timeout=5.0).ping()


@dataclass
class Probes:
    """Every external read the preflight makes. Tests inject fakes; defaults are real."""

    tailscale: Runner
    find_tailscale: Callable[[], str]
    which: Callable[[str], str | None] = trusted_executable
    herdr: Callable[[Sequence[str]], subprocess.CompletedProcess[str]] = field(
        default=lambda args: _run_fixed("herdr", args)
    )
    uv: Callable[[Sequence[str]], subprocess.CompletedProcess[str]] = field(
        default=lambda args: _run_fixed("uv", args)
    )
    ping: Ping = _default_ping
    socket_exists: Callable[[str], bool] = lambda path: Path(path).exists()  # noqa: E731
    python_version: tuple[int, int] = (sys.version_info[0], sys.version_info[1])


def default_probes(tailscale_runner: Runner | None = None) -> Probes:
    runner = tailscale_runner or tailscale.default_runner
    return Probes(tailscale=runner, find_tailscale=tailscale.find_cli)


# -- Tailscale ---------------------------------------------------------------------------------


def _status_doc(run: Runner) -> dict[str, Any]:
    try:
        result = run(["status", "--json", "--peers=false"])
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise _Unknown from exc
    if result.returncode != 0:
        raise _Unknown
    try:
        doc = json.loads(result.stdout or "{}")
    except ValueError as exc:
        raise _Unknown from exc
    if not isinstance(doc, dict):
        raise _Unknown
    return doc


def check_tailscale(probes: Probes) -> tuple[Result, str | None]:
    """(result, this Mac's normalised MagicDNS name when ready)."""
    try:
        probes.find_tailscale()
    except tailscale.TailscaleError:
        return Result("tailscale", STATE_MISSING, GUIDANCE["tailscale_missing"]), None
    try:
        doc = _status_doc(probes.tailscale)
    except _Unknown:
        return Result("tailscale", STATE_UNKNOWN, GUIDANCE["tailscale_unknown"]), None
    backend = doc.get("BackendState")
    if backend == "Stopped":
        return Result("tailscale", STATE_NOT_RUNNING, GUIDANCE["tailscale_stopped"]), None
    if backend == "NeedsLogin":
        return Result("tailscale", STATE_NOT_LOGGED_IN, GUIDANCE["tailscale_login"]), None
    if backend == "NeedsMachineAuth":
        return Result("tailscale", STATE_NEEDS_ACTION, GUIDANCE["tailscale_approval"]), None
    if backend != "Running":
        return Result("tailscale", STATE_UNKNOWN, GUIDANCE["tailscale_unknown"]), None
    self_node = doc.get("Self")
    tailnet = doc.get("CurrentTailnet")
    certs = doc.get("CertDomains", [])
    if (
        not isinstance(self_node, dict)
        or not isinstance(tailnet, dict)
        or not isinstance(certs, list)
    ):
        return Result("tailscale", STATE_UNKNOWN, GUIDANCE["tailscale_unknown"]), None
    dns = _normal(self_node.get("DNSName") or "")
    magic = tailnet.get("MagicDNSEnabled")
    if not isinstance(magic, bool):
        return Result("tailscale", STATE_UNKNOWN, GUIDANCE["tailscale_unknown"]), None
    if not magic or not dns.endswith(".ts.net"):
        return Result("tailscale", STATE_NEEDS_ACTION, GUIDANCE["tailscale_dns"]), None
    if dns not in {_normal(entry) for entry in certs if isinstance(entry, str)}:
        return Result("tailscale", STATE_NEEDS_ACTION, GUIDANCE["tailscale_dns"]), None
    return Result("tailscale", STATE_OK), dns


def check_serve(probes: Probes, https_port: int, expected_target: str | None = None) -> Result:
    """Read `serve status --json`: refuse public (Funnel) access on the target HTTPS port."""
    try:
        doc = tailscale.serve_status(probes.tailscale)
    except tailscale.TailscaleError:
        return Result("tailscale_serve", STATE_UNKNOWN, GUIDANCE["serve_unknown"])
    state = tailscale.inspect_serve(doc, https_port, expected_target)
    if state == "public":
        return Result("tailscale_serve", STATE_NEEDS_ACTION, GUIDANCE["serve_funnel"])
    if state == "conflict":
        return Result("tailscale_serve", STATE_NEEDS_ACTION, GUIDANCE["serve_conflict"])
    if state == "unknown":
        return Result("tailscale_serve", STATE_UNKNOWN, GUIDANCE["serve_unknown"])
    return Result("tailscale_serve", STATE_OK, detail=f"port {https_port}")


# -- Herdr -----------------------------------------------------------------------------------


def _herdr_running(doc: Mapping[str, Any]) -> bool | None:
    if isinstance(doc.get("running"), bool):
        return bool(doc["running"])
    for key in ("status", "state"):
        value = doc.get(key)
        if isinstance(value, str):
            if value.lower() == "running":
                return True
            if value.lower() in ("stopped", "not_running", "absent"):
                return False
    return None


def check_herdr(probes: Probes, socket: str) -> Result:
    if probes.which("herdr") is None:
        return Result("herdr", STATE_MISSING, GUIDANCE["herdr_missing"])
    try:
        version = probes.herdr(["--version"])
    except (OSError, subprocess.TimeoutExpired):
        return Result("herdr", STATE_UNKNOWN, GUIDANCE["herdr_unknown"])
    match = HERDR_VERSION.search(version.stdout or "") if version.returncode == 0 else None
    if match is None:
        return Result("herdr", STATE_UNKNOWN, GUIDANCE["herdr_unknown"])
    detail = match.group(1)
    try:
        status = probes.herdr(["status", "server", "--json"])
    except (OSError, subprocess.TimeoutExpired):
        return Result("herdr", STATE_UNKNOWN, GUIDANCE["herdr_unknown"], detail)
    if status.returncode != 0:
        return Result("herdr", STATE_NOT_RUNNING, GUIDANCE["herdr_down"], detail)
    try:
        doc = json.loads(status.stdout or "{}")
    except ValueError:
        return Result("herdr", STATE_UNKNOWN, GUIDANCE["herdr_unknown"], detail)
    if not isinstance(doc, dict):
        return Result("herdr", STATE_UNKNOWN, GUIDANCE["herdr_unknown"], detail)
    running = _herdr_running(doc)
    if running is False:
        return Result("herdr", STATE_NOT_RUNNING, GUIDANCE["herdr_down"], detail)
    if running is None:
        return Result("herdr", STATE_UNKNOWN, GUIDANCE["herdr_unknown"], detail)
    if not probes.socket_exists(socket):
        return Result("herdr", STATE_NOT_RUNNING, GUIDANCE["herdr_down"], detail)
    try:
        asyncio.run(probes.ping(socket))
    except HerdrProtocolError as exc:
        if exc.code == "protocol_mismatch":
            return Result("herdr", STATE_NEEDS_ACTION, GUIDANCE["herdr_protocol"], detail)
        return Result("herdr", STATE_UNKNOWN, GUIDANCE["herdr_unknown"], detail)
    except HerdrError:
        return Result("herdr", STATE_UNKNOWN, GUIDANCE["herdr_unknown"], detail)
    except (TimeoutError, OSError):
        return Result("herdr", STATE_NOT_RUNNING, GUIDANCE["herdr_down"], detail)
    return Result("herdr", STATE_OK, detail=detail)


# -- uv and the runtime interpreter ----------------------------------------------------------


def check_uv(probes: Probes) -> Result:
    if probes.which("uv") is None:
        return Result("uv", STATE_MISSING, GUIDANCE["uv_missing"])
    try:
        result = probes.uv(["--version"])
    except (OSError, subprocess.TimeoutExpired):
        return Result("uv", STATE_UNKNOWN, GUIDANCE["uv_unknown"])
    match = UV_VERSION.search(result.stdout or "") if result.returncode == 0 else None
    if match is None:
        return Result("uv", STATE_UNKNOWN, GUIDANCE["uv_unknown"])
    return Result("uv", STATE_OK, detail=match.group(1))


def check_python(probes: Probes) -> Result:
    version = probes.python_version
    if PYTHON_MIN <= version < PYTHON_BELOW:
        return Result("python", STATE_OK, detail=f"{version[0]}.{version[1]}")
    return Result("python", STATE_NEEDS_ACTION, GUIDANCE["python"], f"{version[0]}.{version[1]}")


# -- the preflight ------------------------------------------------------------------------------


def run_prereqs(
    *,
    socket: str,
    https_port: int,
    probes: Probes,
    expected_target: str | None = None,
    usage_roots: Sequence[Path] = (),
) -> Report:
    tailscale_result, dns = check_tailscale(probes)
    if dns is not None:
        serve = check_serve(probes, https_port, expected_target)
    else:
        serve = Result("tailscale_serve", STATE_UNKNOWN, GUIDANCE["serve_after_tailscale"])
    results = (
        tailscale_result,
        serve,
        check_herdr(probes, socket),
        check_uv(probes),
        check_python(probes),
    )
    warnings = tuple(
        f"usage source {index + 1}: root unavailable"
        for index, root in enumerate(usage_roots)
        if not Path(root).is_dir()
    )
    return Report(results=results, warnings=warnings)


def probe_socket(configured: str | None) -> str:
    """The configured socket, or the default one when no config exists yet. An unresolvable path
    is returned unchanged; the Herdr check then reports it as unknown rather than raising."""
    try:
        return resolve_socket(configured or DEFAULT_SOCKET)
    except HerdrError:
        return configured or DEFAULT_SOCKET
