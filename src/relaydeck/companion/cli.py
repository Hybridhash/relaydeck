"""Mac companion foreground lifecycle and opt-in private-route diagnostics."""

from __future__ import annotations

import argparse
import asyncio
import faulthandler
import json
import signal
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from relaydeck.companion.config import DEFAULT_CONFIG, CompanionConfig, load_config
from relaydeck.companion.lifecycle import AlreadyRunning, request_stop, status, supervise
from relaydeck.companion.runtime import CompanionRuntime
from relaydeck.companion.setup import preflight
from relaydeck.usage.hook_cli import add_usage_parser, run_configured_usage
from relaydeck.version import __version__


@contextmanager
def thread_dumps() -> Iterator[None]:
    """Operator-only stack dump to the existing foreground log; never includes locals."""
    previous = signal.getsignal(signal.SIGUSR1)
    faulthandler.register(signal.SIGUSR1, file=sys.stderr, all_threads=True)
    try:
        yield
    finally:
        faulthandler.unregister(signal.SIGUSR1)
        signal.signal(signal.SIGUSR1, previous)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="relaydeck-companion")
    parser.add_argument("--config", default=DEFAULT_CONFIG, help="Owner-only companion TOML")
    parser.add_argument("--version", action="version", version=f"relaydeck {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)
    add_usage_parser(sub)
    start = sub.add_parser(
        "start", help="Foreground gateway + dynamic Flet; never install a service"
    )
    source = start.add_mutually_exclusive_group()
    source.add_argument(
        "--connect-herdr",
        action="store_true",
        help="Authorize this run's local Herdr attachment resources",
    )
    source.add_argument(
        "--fixture", action="store_true", help="Explicit synthetic source; never production proof"
    )
    sub.add_parser("status", help="Read readiness of this state directory's exclusive owner")
    stop = sub.add_parser("stop", help="Request this instance to stop; never signal a PID or Herdr")
    stop.add_argument("--timeout", type=float, default=12, help="Bounded wait; no kill fallback")
    doctor = sub.add_parser(
        "doctor", help="Check local prerequisites without provisioning anything"
    )
    doctor.add_argument(
        "--probe-private",
        action="store_true",
        help="Check configured HTTPS route with certificate verification",
    )
    pairing = sub.add_parser("pairing", help="Explicit Mac-local device pairing authorization")
    actions = pairing.add_subparsers(dest="pairing_action", required=True)
    window = actions.add_parser("open", help="Open a bounded window for an enrolled Mac")
    window.add_argument("--ttl-seconds", type=int, default=120)
    actions.add_parser("list", help="Inspect requests and compare matching codes")
    for action in ("approve", "deny"):
        decision = actions.add_parser(action, help=f"Explicitly {action} a matching device request")
        decision.add_argument(
            "--request",
            default=None,
            help=(
                "Request id; approve may omit it when exactly one pending request has this code"
                if action == "approve"
                else "Request id (required for deny)"
            ),
        )
        decision.add_argument("--code", required=True, help="Matching XXX-XXX code from the device")
    actions.add_parser("close", help="Discard the window, requests and unused approvals")
    push = sub.add_parser("push", help="Web push maintenance; every action asks for confirmation")
    push_actions = push.add_subparsers(dest="push_action", required=True)
    push_actions.add_parser(
        "web-rotate",
        help="Replace the web push key; disables sending and removes all web subscriptions",
    )
    return parser


WEB_ROTATE_PHRASE = "rotate web push"


def _web_rotate(config: CompanionConfig) -> int:
    """Replace the VAPID key after a typed confirmation (no --yes). Refuses without a terminal, and
    while the companion runs, because a running process still holds the old key."""
    from relaydeck.gateway.webpush_store import WebPushStore, rotate

    if not sys.stdin.isatty():
        print("push web-rotate needs an interactive terminal; nothing was changed.")
        return 2
    if status(config.state_dir).get("state") != "stopped":
        print("Stop the companion first (relaydeck-companion stop); nothing was changed.")
        return 1
    print("This turns web push sending off, deletes every web push subscription and queued")
    print("message, and replaces the VAPID key. Each browser or home-screen app must resubscribe.")
    typed = input(f'Type "{WEB_ROTATE_PHRASE}" to continue: ').strip()
    if typed != WEB_ROTATE_PHRASE:
        print("Not confirmed; nothing was changed.")
        return 1
    store = WebPushStore(config.state_dir)
    try:
        result = rotate(config.state_dir, store)
    finally:
        store.close()
    print(f"Web push key replaced; {result['subscriptions_removed']} subscription(s) removed.")
    return 0


async def probe_private(url: str) -> dict[str, str]:
    import httpx

    try:
        async with httpx.AsyncClient(timeout=5, trust_env=False, follow_redirects=False) as client:
            response = await client.get(url + "/healthz")
            body = response.json()
            if (
                response.status_code != 200
                or not isinstance(body, dict)
                or body.get("ok") is not True
            ):
                return {
                    "state": "failed",
                    "message": "HTTPS route did not reach the companion health endpoint.",
                }
    except (httpx.HTTPError, ValueError):
        return {
            "state": "failed",
            "message": (
                "Private route unavailable. Check Mac power/network, Tailscale login, "
                "HTTPS Serve and device access policy."
            ),
        }
    return {
        "state": "reachable",
        "message": (
            "HTTPS health reachable; application login and Android/mobile access "
            "still need separate verification."
        ),
    }


def _select_web_bundle() -> None:
    """with FLET_WEB_PATH unset, serve the newest verified installed bundle.
    An explicit FLET_WEB_PATH always wins; with nothing installed, say how to install."""
    import os

    from relaydeck import webbundle

    if os.environ.get("FLET_WEB_PATH"):
        return
    current = webbundle.current_bundle()
    if current is None:
        print("No installed web bundle; run `relaydeck web install <build dir>` to install one.")
        return
    os.environ["FLET_WEB_PATH"] = str(current)
    print(f"Web bundle: {current}")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "usage":
        return run_configured_usage(args)  # Before full config, secrets and prerequisite probes.
    try:
        config = load_config(Path(args.config).expanduser().absolute())
        if args.command == "pairing":
            from relaydeck.companion.pairing import run_pairing

            return run_pairing(config, args)
        if args.command == "push":
            return _web_rotate(config)
        if args.command == "status":
            print(json.dumps(status(config.state_dir), indent=2))
            return 0
        if args.command == "stop":
            if not 0 < args.timeout <= 60:
                raise ValueError("stop timeout must be greater than zero and at most 60 seconds")
            stopped = asyncio.run(request_stop(config.state_dir, timeout=args.timeout))
            print(
                (
                    "stop acknowledged; companion stopping"
                    if status(config.state_dir)["state"] == "stopping"
                    else "companion stopped"
                )
                if stopped
                else "stop not acknowledged; inspect the foreground process; no PID was signalled"
            )
            return 0 if stopped else 1
        if args.command == "doctor":
            config.secrets()
            report: dict[str, Any] = {
                "version": __version__,
                # the same read-only results as `relaydeck setup`.
                "prerequisites": preflight(Path(args.config).expanduser().absolute()).as_doc(),
                "advertised_origin": config.advertised_origin,
                "local_endpoint": config.local_endpoint,
                "secrets": "Provided private files validated; no credentials created.",
                "pairing": "Credentials present; start validates their enrollment binding."
                if config.connector_credentials.exists()
                else "Enroll this Mac's connector with a public-key-bound invitation.",
                "herdr": "Socket present; startup checks protocol 22."
                if Path(config.herdr_socket).exists()
                else "Herdr socket unavailable; start Herdr separately and check its path.",
                "device_authorization": (
                    "Approve a device on this Mac with a matching code (relaydeck pairing)."
                ),
                "identity": (
                    "No separate account login; a device signs in by pairing with this Mac."
                ),
                "push": "Native push/provider integration and device proof remain required.",
                "private_route": asyncio.run(probe_private(config.advertised_origin))
                if args.probe_private
                else {
                    "state": "unverified",
                    "message": "Run doctor --probe-private; repeat access on the phone.",
                },
            }
            print(json.dumps(report, indent=2))
            return 1 if report["private_route"]["state"] == "failed" else 0

        async def run() -> int:
            stop = asyncio.Event()
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(sig, stop.set)
            try:
                return await supervise(
                    config.state_dir,
                    lambda: CompanionRuntime(
                        config,
                        connect_herdr=args.connect_herdr,
                        fixture=args.fixture,
                    ),
                    stop,
                )
            finally:
                for sig in (signal.SIGINT, signal.SIGTERM):
                    loop.remove_signal_handler(sig)

        if config.web_ui:
            _select_web_bundle()
        print(f"Foreground companion: {config.advertised_origin}; backend {config.local_endpoint}")
        with thread_dumps():
            return asyncio.run(run())
    except (ValueError, OSError, RuntimeError, AlreadyRunning) as exc:
        print(f"companion: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
