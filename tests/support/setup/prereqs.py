"""Shared prereqs-probe test helpers (moved out of test modules).

Every probe is injected and synthetic. No real tailnet, user, host name, socket or binary is read.
"""

from __future__ import annotations

import dataclasses
import json
import subprocess
from typing import Any

from relaydeck.companion import prereqs

SYNTHETIC_DNS = "mac-a.synthetic-tailnet.example.ts.net."


def _done(
    args: Any, code: int = 0, out: str = "", err: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(list(args), code, out, err)


def _status(**changes: Any) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "BackendState": "Running",
        "Self": {"DNSName": SYNTHETIC_DNS},
        "CurrentTailnet": {"MagicDNSEnabled": True},
        "CertDomains": [SYNTHETIC_DNS.upper()],
    }
    doc.update(changes)
    return doc


class Serve:
    """Record every Tailscale call; mutating calls are kept apart."""

    def __init__(self, status: Any = None, serve: Any = None, status_code: int = 0) -> None:
        self.status = _status() if status is None else status
        self.serve = {"Web": {}, "AllowFunnel": {}} if serve is None else serve
        self.status_code = status_code
        self.calls: list[list[str]] = []

    def __call__(self, args: Any) -> subprocess.CompletedProcess[str]:
        argv = list(args)
        self.calls.append(argv)
        if argv[:2] == ["status", "--json"]:
            if self.status_code != 0:
                return _done(argv, self.status_code, "", "STDERR-SECRET")
            body = self.status if isinstance(self.status, str) else json.dumps(self.status)
            return _done(argv, 0, body)
        if argv[:3] == ["serve", "status", "--json"]:
            return _done(argv, 0, json.dumps(self.serve))
        return _done(argv, 0, "", "")

    @property
    def mutations(self) -> list[list[str]]:
        return [
            c
            for c in self.calls
            if c[:2] != ["status", "--json"] and c[:3] != ["serve", "status", "--json"]
        ]


def probes(serve: Serve | None = None, **changes: Any) -> prereqs.Probes:
    serve = serve or Serve()

    async def ping(socket: str) -> None:
        return None

    base = prereqs.Probes(
        tailscale=serve,
        find_tailscale=lambda: "/synthetic/tailscale",
        which=lambda name: f"/synthetic/{name}",
        herdr=lambda args: _done(
            args,
            0,
            "running"
            if False
            else (
                json.dumps({"running": True})
                if list(args)[:2] == ["status", "server"]
                else "herdr 0.9.1"
            ),
        ),
        uv=lambda args: _done(args, 0, "uv 0.12.15 (synthetic)\n"),
        ping=ping,
        socket_exists=lambda path: True,
        python_version=(3, 13),
    )
    return dataclasses.replace(base, **changes)


def only(report: prereqs.Report, check: str) -> prereqs.Result:
    return next(r for r in report.results if r.check == check)
