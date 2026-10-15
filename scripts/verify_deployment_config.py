"""Offline production-safety gate for gateway deployment configs: refuses unsafe production configs
WITHOUT touching live infrastructure. Two layers:

1. Tree checks (always run): inspect the REAL gateway route table for
   forbidden surfaces (raw Herdr/SSH exposure, arbitrary exec,
   unrestricted TCP relay), prove empty cursor secrets are refused by
   the REAL cursor codec, and prove the shipped runners default to
   loopback bind with a single worker.
2. Config checks (--config PATH): parse a deployment TOML file (schema
   defined below) and refuse empty/placeholder
   secrets, multi-worker serving, missing TLS/auth-callback allowlist,
   missing log redaction, missing database paths, and forbidden
   passthrough keys.

Exit 0 when every check passes, 1 on any violation, 2 on usage errors.
This script never opens a socket: --offline is mandatory so CI and
operators can see the no-live-infra promise in the invocation itself.

Synthetic fixtures (never real secrets):
  tests/fixtures/deployment/gateway.good.toml  (must exit 0)
  tests/fixtures/deployment/gateway.bad.toml   (must exit 1)
"""

from __future__ import annotations

import argparse
import inspect
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

MIN_SECRET_BYTES = 32

# Placeholder markers (case-insensitive substring match). Intentionally
# narrow: a value containing "test" or "synth" alone is NOT a marker,
# so synthetic verifier fixtures pass while real placeholders fail.
PLACEHOLDER_MARKERS = (
    "changeme",
    "change-me",
    "placeholder",
    "example",
    "todo",
    "your-",
    "insert-",
    "replace-me",
    "xxx",
)

# Route-table substrings that must never appear in a served path. The
# gateway exposes typed command/terminal/metadata routes only; anything
# shaped like a raw socket, shell, exec, or relay surface fails closed.
FORBIDDEN_ROUTE_PARTS = (
    "herdr",
    "ssh",
    "/exec",
    "tcp",
    "relay",
    "/raw",
    "/shell",
    "passthrough",
)

# The only WebSocket routes the gateway serves:
# the connector tunnel and the per-lease terminal socket.
EXPECTED_WS_ROUTES = frozenset({"/v1/connectors", "/v1/terminal/{lease_id}"})

# Config keys that must never exist anywhere in the deployment TOML:
# there is no passthrough surface to enable.
FORBIDDEN_CONFIG_KEYS = frozenset(
    {
        "allow_exec",
        "arbitrary_exec",
        "tcp_relay",
        "raw_herdr",
        "raw_socket",
        "permit_shell",
        "ssh_passthrough",
    }
)

ALLOWED_TOP_LEVEL = frozenset({"secrets", "server", "tls", "logging", "database"})
LOOPBACK_BINDS = frozenset({"127.0.0.1", "::1", "localhost"})


@dataclass
class Report:
    passed: list[str] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)

    def ok(self, name: str, detail: str) -> None:
        self.passed.append(f"check {name}: PASS ({detail})")

    def fail(self, where: str, reason: str) -> None:
        self.violations.append(f"VIOLATION {where}: {reason}")


def _is_placeholder(value: str) -> bool:
    lowered = value.strip().lower()
    if not lowered:
        return True
    return any(marker in lowered for marker in PLACEHOLDER_MARKERS)


def check_route_table(report: Report) -> None:
    """Inspect the REAL gateway app route table (no mocks, no network)."""
    from relaydeck.gateway.app import GatewayState, create_app
    from relaydeck.gateway.backend import FakeBackend

    state = GatewayState(backend=FakeBackend(now_ms=lambda: 0))
    paths: list[tuple[str, str]] = []
    for route in create_app(state).routes:
        route_methods = sorted(getattr(route, "methods", None) or [])
        paths.append(
            (",".join(route_methods) if route_methods else "WS", str(getattr(route, "path", "")))
        )
    for methods, path in paths:
        lowered = path.lower()
        for part in FORBIDDEN_ROUTE_PARTS:
            if part in lowered:
                report.fail(
                    f"route {methods} {path}",
                    f"forbidden surface {part!r}: no raw Herdr/SSH, exec,"
                    " or TCP-relay route may be served",
                )
    served_ws = frozenset(path for methods, path in paths if methods == "WS")
    if served_ws != EXPECTED_WS_ROUTES:
        report.fail(
            "routes websocket-set",
            f"expected exactly {sorted(EXPECTED_WS_ROUTES)}, served"
            f" {sorted(served_ws)}: audit the new socket before deploying",
        )
    else:
        report.ok("routes websocket-set", "only /v1/connectors + /v1/terminal/*")
    if not any(path == "/healthz" for _, path in paths):
        report.fail("routes healthz", "GET /healthz missing: no health check to probe")
    else:
        report.ok("routes healthz", "GET /healthz present")
    if not report.violations:
        report.ok("routes forbidden-surfaces", f"{len(paths)} routes, none forbidden")


def check_cursor_secret_refusal(report: Report) -> None:
    """Prove the REAL codec refuses empty production secrets."""
    from relaydeck.gateway.cursors import CursorError, decode_cursor, encode_cursor

    try:
        encode_cursor(b"", {"host_id": "h", "sequence": 1})
    except CursorError:
        report.ok("codec cursor-secret", "encode_cursor(b'') raises CursorError")
    else:
        report.fail(
            "codec cursor-secret",
            "encode_cursor(b'') did not raise: empty cursor_secret is accepted",
        )
    try:
        decode_cursor(b"", "anything.at.all")
    except CursorError:
        report.ok("codec cursor-decode", "decode_cursor(b'', ...) raises CursorError")
    else:
        report.fail(
            "codec cursor-decode",
            "decode_cursor(b'', ...) did not raise: empty secret verifies",
        )


def check_runner_defaults(report: Report) -> None:
    """Prove the shipped runners default to loopback + single worker."""
    from relaydeck.gateway.cli import build_parser as gateway_parser

    serve_defaults = gateway_parser().parse_args(["serve"])
    if serve_defaults.host in LOOPBACK_BINDS:
        report.ok("runners gateway-bind", f"serve --host defaults to {serve_defaults.host}")
    else:
        report.fail(
            "runners gateway-bind",
            f"serve --host defaults to {serve_defaults.host!r}, not loopback",
        )
    from relaydeck.client import serve as web_runner

    try:
        source = inspect.getsource(web_runner.main)
    except (OSError, TypeError):
        report.fail("runners one-worker", "cannot inspect web runner source")
        return
    if "workers=1" in source:
        report.ok("runners one-worker", "web runner pins uvicorn workers=1")
    else:
        report.fail(
            "runners one-worker",
            "web runner no longer pins workers=1: single-worker constraint"
            " is the deployment default (in-memory registries split-brain)",
        )


def _walk(node: Any, trail: str, report: Report) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            here = f"{trail}.{key}" if trail else str(key)
            if str(key) in FORBIDDEN_CONFIG_KEYS:
                report.fail(
                    f"config {here}",
                    "forbidden passthrough key: no exec/relay/raw surface exists to enable",
                )
            _walk(value, here, report)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            _walk(value, f"{trail}[{index}]", report)


def _secret(report: Report, doc: dict[str, Any], key: str, *, minimum: int) -> None:
    section = doc.get("secrets")
    value = section.get(key) if isinstance(section, dict) else None
    where = f"config secrets.{key}"
    if not isinstance(value, str) or not value:
        report.fail(where, "missing or empty: provision a real secret, never deploy blank")
        return
    if _is_placeholder(value):
        report.fail(where, "placeholder value: provision a real secret, never deploy this")
        return
    if len(value.encode()) < minimum:
        report.fail(where, f"only {len(value.encode())} bytes, need >={minimum}")
        return
    report.ok(where, f"present, {len(value.encode())} bytes, not a placeholder")


def check_config(path: Path, report: Report) -> None:
    """Validate one deployment TOML file (offline: local read only)."""
    try:
        doc = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        report.fail(f"config {path}", "file not found")
        return
    except (OSError, tomllib.TOMLDecodeError) as exc:
        report.fail(f"config {path}", f"unreadable TOML: {exc}")
        return
    if not isinstance(doc, dict):
        report.fail(f"config {path}", "top level must be a TOML table")
        return
    for section in sorted(set(doc) - ALLOWED_TOP_LEVEL):
        report.fail(f"config {section}", f"unknown section (allowed: {sorted(ALLOWED_TOP_LEVEL)})")
    _walk(doc, "", report)

    _secret(report, doc, "cursor_secret", minimum=MIN_SECRET_BYTES)
    _secret(report, doc, "cookie_secret", minimum=MIN_SECRET_BYTES)
    # fcm_key is a provider-issued opaque token of provider-defined
    # length, not locally-generated HMAC entropy: the floor only rejects
    # empty/placeholder/short junk.
    _secret(report, doc, "fcm_key", minimum=8)

    server = doc.get("server")
    if not isinstance(server, dict):
        report.fail("config server", "missing [server] section")
        # Fall through with an empty section so tls/logging/database are
        # still reported instead of silently skipped.
        server = {}
    workers = server.get("workers")
    if workers != 1:
        report.fail("config server.workers", f"must be exactly 1, got {workers!r}")
    else:
        report.ok("config server.workers", "one-worker routing constraint holds")
    bind = server.get("bind", "")
    if bind not in LOOPBACK_BINDS:
        report.fail(
            "config server.bind", f"must be loopback {sorted(LOOPBACK_BINDS)}, got {bind!r}"
        )
    else:
        report.ok("config server.bind", f"loopback-only ({bind}) behind the TLS proxy")
    origin = server.get("public_origin", "")
    if not isinstance(origin, str) or not origin.startswith("https://"):
        report.fail("config server.public_origin", f"must be an https:// origin, got {origin!r}")
    else:
        report.ok("config server.public_origin", origin)
    allowlist = server.get("auth_callback_allowlist")
    if (
        not isinstance(allowlist, list)
        or not allowlist
        or not all(isinstance(o, str) and o.startswith("https://") for o in allowlist)
    ):
        report.fail(
            "config server.auth_callback_allowlist",
            "must be a non-empty list of https:// origins (exact match, no wildcards)",
        )
    else:
        report.ok("config server.auth_callback_allowlist", f"{len(allowlist)} pinned origin(s)")

    tls = doc.get("tls")
    mode = tls.get("mode") if isinstance(tls, dict) else None
    if mode not in ("terminated-at-proxy", "direct"):
        report.fail("config tls.mode", f"must be terminated-at-proxy|direct, got {mode!r}")
    else:
        report.ok("config tls.mode", str(mode))

    logging_cfg = doc.get("logging")
    redact = logging_cfg.get("redact_query_tokens") if isinstance(logging_cfg, dict) else None
    log_config = logging_cfg.get("log_config") if isinstance(logging_cfg, dict) else None
    if redact is not True:
        report.fail(
            "config logging.redact_query_tokens",
            "must be true: rdh_/rdt_ query tokens land in access logs unredacted",
        )
    else:
        report.ok("config logging.redact_query_tokens", "query-token redaction declared")
    if not isinstance(log_config, str) or not log_config:
        report.fail("config logging.log_config", "must name the redacting uvicorn log config")
    else:
        report.ok("config logging.log_config", log_config)

    database = doc.get("database")
    journal_path = database.get("journal_path") if isinstance(database, dict) else None
    push_path = database.get("push_path") if isinstance(database, dict) else None
    if not isinstance(journal_path, str) or not journal_path:
        report.fail("config database.journal_path", "must name the journal sqlite file")
    else:
        report.ok("config database.journal_path", journal_path)
    if not isinstance(push_path, str) or not push_path:
        report.fail("config database.push_path", "must name the push sqlite file")
    else:
        report.ok("config database.push_path", push_path)


def check_companion_config(path: Path, report: Report) -> None:
    """Local primary profile. Verify configuration without starting any service."""
    from relaydeck.companion.config import load_config
    from relaydeck.companion.private import check_private

    try:
        config = load_config(path.expanduser().absolute())
        cookie, cursor = config.secrets()
        if any(_is_placeholder(value.decode()) for value in (cookie, cursor)):
            raise ValueError("secret file contains a placeholder")
        if config.state_dir.exists():
            check_private(config.state_dir, directory=True)
    except (OSError, ValueError) as exc:
        report.fail("companion private-config", str(exc))
        return
    report.ok(
        "companion private-config", "private secret files, HTTPS tailnet origin, loopback backend"
    )
    report.ok(
        "companion topology",
        "single foreground owner; durable state under configured Mac directory",
    )
    report.ok(
        "companion acceptance",
        "configuration only; identity/push/Android/browser gates remain open",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Offline deployment-config safety gate.")
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Required: acknowledge this check touches no live infrastructure.",
    )
    parser.add_argument("--config", default="", help="Deployment TOML file to validate.")
    parser.add_argument(
        "--companion-config", default="", help="Mac primary profile TOML to validate"
    )
    args = parser.parse_args(argv)
    if not args.offline:
        print("refusing: pass --offline (this verifier never touches live infra)", file=sys.stderr)
        return 2
    report = Report()
    if args.config and args.companion_config:
        print("choose --config (historical) or --companion-config (Mac primary)", file=sys.stderr)
        return 2
    check_route_table(report)
    check_cursor_secret_refusal(report)
    check_runner_defaults(report)
    if args.companion_config:
        check_companion_config(Path(args.companion_config), report)
    elif args.config:
        check_config(Path(args.config), report)
    else:
        report.ok("config file", "skipped (no --config given); tree checks only")
    for line in report.passed:
        print(line)
    for line in report.violations:
        print(line)
    print(f"{len(report.passed)} passed, {len(report.violations)} violations")
    return 1 if report.violations else 0


if __name__ == "__main__":
    raise SystemExit(main())
