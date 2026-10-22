"""Connector CLI (runs on the home Mac alongside Herdr)."""

from __future__ import annotations

import argparse
import asyncio
from typing import Any


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="relaydeck-connector")
    sub = parser.add_subparsers(dest="command", required=True)
    keygen = sub.add_parser("keygen", help="Provision a fresh connector identity")
    keygen.add_argument("--force", action="store_true", help="Replace existing keys")
    sub.add_parser("fingerprint", help="Print the local key fingerprint")
    enroll = sub.add_parser("enroll", help="Redeem an enrollment invitation")
    enroll.add_argument("--gateway", default="", help="Gateway base URL (or from --qr)")
    enroll.add_argument("--host", required=True, help="Host id")
    enroll.add_argument("--id", default="", help="Invitation id (or use --qr)")
    enroll.add_argument("--code", default="", help="Claim code (or use --qr)")
    enroll.add_argument("--qr", default="", help="Scanned invitation reference")
    sub.add_parser("status", help="Show enrollment status")
    sub.add_parser("revoke", help="Wipe local credentials")
    run = sub.add_parser("run", help="Run the connector link (fixture or live Herdr)")
    run.add_argument("--herdr-socket", default="~/.config/herdr/herdr.sock")
    run.add_argument(
        "--journal-path",
        default="~/.config/relaydeck/connector-journal.db",
        help="Connector intent journal (sqlite, 0600)",
    )
    run.add_argument(
        "--fixture",
        action="store_true",
        help="Dispatch against the synthetic fixture (no Herdr contact)",
    )
    run.add_argument(
        "--live-herdr",
        action="store_true",
        help="Dispatch against the real Herdr daemon at --herdr-socket "
        "(REQUIRES-OWNER-APPROVAL: also needs RELAYDECK_ALLOW_LIVE_HERDR=1)",
    )
    run.add_argument(
        "--once",
        action="store_true",
        help="Single session: push state, serve one dispatch, exit 0",
    )
    run.add_argument(
        "--once-timeout", type=float, default=10.0, help="Seconds --once waits for a dispatch"
    )
    run.add_argument(
        "--max-retries",
        type=int,
        default=None,
        help="Reconnect attempts before exit 1 (default: forever)",
    )
    run.add_argument(
        "--log-config", default="", help="Logging config file (.json dictConfig, else fileConfig)"
    )
    run.add_argument("--qr", default="", help="First-run redeem: scanned invitation reference")
    run.add_argument(
        "--gateway", default="", help="First-run redeem: gateway base URL (or from --qr)"
    )
    run.add_argument("--host", default="", help="First-run redeem: host id")
    run.add_argument(
        "--local-gateway",
        default="",
        help="Explicit same-Mac loopback endpoint; does not change enrolled private HTTPS origin",
    )
    run.add_argument("--id", default="", help="First-run redeem: invitation id (or use --qr)")
    run.add_argument("--code", default="", help="First-run redeem: claim code (or use --qr)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "keygen":
        from relaydeck.connector.enrollment import generate_keys, load_keys, save_keys

        if load_keys() is not None and not args.force:
            print("keys exist (use --force to replace)")
            return 1
        fresh = generate_keys()
        path = save_keys(fresh)
        print(f"keys at {path}")
        print(f"fingerprint {fresh.fingerprint}")
        print(f"pubkey {fresh.public_openssh}")
        return 0
    if args.command == "fingerprint":
        from relaydeck.connector.enrollment import load_keys

        found = load_keys()
        if found is None:
            print("no keys (run keygen first)")
            return 1
        print(found.fingerprint)
        return 0
    if args.command == "enroll":
        from relaydeck.connector.enrollment import (
            ConnectorCreds,
            claim_enrollment,
            load_keys,
            parse_invitation,
            save_creds,
        )

        gateway, invitation_id, code = args.gateway, args.id, args.code
        if args.qr:
            try:
                origin, invitation_id, code, _ = parse_invitation(args.qr)
            except Exception as exc:  # noqa: BLE001 - CLI reports any failure
                print(f"enroll failed: bad invitation: {exc}")
                return 1
            gateway = gateway or origin
        if not gateway:
            print("enroll failed: gateway URL required (--gateway or QR origin)")
            return 1
        present = load_keys()
        if present is None:
            print("enroll failed: no keys (run keygen first)")
            return 1

        async def post_json(url: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
            import httpx

            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(url, json=body)
                try:
                    return response.status_code, response.json()
                except ValueError:
                    return response.status_code, {}

        try:
            token, fingerprint = asyncio.run(
                claim_enrollment(post_json, gateway, args.host, invitation_id, code, present)
            )
        except Exception as exc:  # noqa: BLE001 - CLI reports any failure
            print(f"enroll failed: {exc}")
            return 1
        path = save_creds(ConnectorCreds(gateway, args.host, token))
        print(f"enrolled {args.host} [{fingerprint}]; credentials at {path}")
        return 0
    if args.command == "status":
        from relaydeck.connector.enrollment import load_creds, load_keys

        creds = load_creds()
        if creds is None:
            print("not enrolled")
            return 1
        maybe_keys = load_keys()
        fingerprint = maybe_keys.fingerprint if maybe_keys else "unknown"
        print(f"host={creds.host_id} gateway={creds.gateway_url} enrolled=yes fp={fingerprint}")
        return 0
    if args.command == "revoke":
        from relaydeck.connector.enrollment import wipe_creds

        if wipe_creds():
            print("local credentials wiped (also revoke at the gateway)")
        else:
            print("not enrolled")
        return 0
    if args.command == "run":
        return _run_link(args)
    return 2


LIVE_HERDR_ENV = "RELAYDECK_ALLOW_LIVE_HERDR"


def _run_link(args: Any) -> int:
    """Run the connector link. Exit 0 clean (shutdown / --once done),
    1 runtime failure (not enrolled, credential dead, retries out,
    source failure), 2 usage error (no source, both sources, or live
    Herdr without owner authorization)."""
    import logging
    import logging.config
    import os
    import signal

    from relaydeck.logging_setup import logging_config

    if bool(args.fixture) == bool(args.live_herdr):
        print("refusing to run: pass exactly one of --fixture or --live-herdr")
        return 2
    if args.live_herdr and os.environ.get(LIVE_HERDR_ENV) != "1":
        print(
            "refusing live Herdr: owner authorization missing "
            f"(set {LIVE_HERDR_ENV}=1 with --live-herdr)"
        )
        return 2
    if args.log_config:
        try:
            if args.log_config.endswith(".json"):
                import json as _json

                with open(args.log_config, encoding="utf-8") as handle:
                    logging.config.dictConfig(_json.load(handle))
            else:
                logging.config.fileConfig(args.log_config, disable_existing_loggers=False)
        except Exception as exc:  # noqa: BLE001 - CLI reports any failure
            print(f"bad --log-config: {exc}")
            return 2
    else:
        config = logging_config()
        config["loggers"] = {}  # the connector only needs the shared handler and format
        config["root"] = {"level": "INFO", "handlers": ["default"]}
        logging.config.dictConfig(config)

    from relaydeck.connector.enrollment import (
        ConnectorCreds,
        claim_enrollment,
        load_creds,
        load_keys,
        parse_invitation,
        save_creds,
    )

    creds = load_creds()
    if creds is None and (args.qr or args.id or args.code):
        gateway, invitation_id, code = args.gateway, args.id, args.code
        if args.qr:
            try:
                origin, invitation_id, code, _ = parse_invitation(args.qr)
            except Exception as exc:  # noqa: BLE001 - CLI reports any failure
                print(f"enroll failed: bad invitation: {exc}")
                return 1
            gateway = gateway or origin
        if not gateway or not args.host:
            print("enroll failed: --gateway (or --qr) and --host are required for first-run redeem")
            return 1
        present = load_keys()
        if present is None:
            print("enroll failed: no keys (run keygen first)")
            return 1

        async def post_json(url: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
            import httpx

            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(url, json=body)
                try:
                    return response.status_code, response.json()
                except ValueError:
                    return response.status_code, {}

        try:
            token, _ = asyncio.run(
                claim_enrollment(post_json, gateway, args.host, invitation_id, code, present)
            )
        except Exception as exc:  # noqa: BLE001 - CLI reports any failure
            print(f"enroll failed: {exc}")
            return 1
        creds = ConnectorCreds(gateway, args.host, token)
        save_creds(creds)
        print(f"enrolled {args.host}; starting link")
    if creds is None:
        print("not enrolled (run enroll first, or pass --qr/--id/--code for first-run redeem)")
        return 1

    from relaydeck.contracts.endpoints import origin as validate_origin

    try:
        gateway_endpoint = (
            validate_origin(args.local_gateway, loopback=True)
            if args.local_gateway
            else creds.gateway_url
        )
    except ValueError as exc:
        print(f"bad --local-gateway: {exc}")
        return 2

    from relaydeck.connector.fixture import FixtureAdapter
    from relaydeck.connector.link import ConnectorLink, LinkConfig

    live_source = None
    if args.live_herdr:
        from relaydeck.connector.herdr import HerdrAdapter
        from relaydeck.connector.source import HerdrSource

        live_source = HerdrSource(HerdrAdapter(socket_path=args.herdr_socket))
        source: Any = live_source
    else:
        source = FixtureAdapter()
    link = ConnectorLink(
        LinkConfig(
            gateway_url=gateway_endpoint,
            host_id=creds.host_id,
            host_token=creds.host_token,
            journal_path=args.journal_path,
            once=args.once,
            once_timeout_s=args.once_timeout,
            max_retries=args.max_retries,
        ),
        source,
    )

    async def serve() -> int:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop.set)
            except (NotImplementedError, RuntimeError, ValueError):
                # Non-main thread or unsupported loop: KeyboardInterrupt
                # handling below still shuts down cleanly.
                pass
        if live_source is not None:
            from relaydeck.connector.herdr import HerdrError

            try:
                bootstrapped = await live_source.bootstrap()
            except (HerdrError, OSError) as exc:
                print(f"live Herdr bootstrap failed: {exc}")
                await live_source.close()
                return 1
            print(
                f"live Herdr bootstrap epoch={bootstrapped.epoch} "
                f"seeds={len(bootstrapped.seed_events)} "
                f"panes={len(live_source.pane_subscribed)}"
            )
        try:
            outcome = await link.run(stop)
        finally:
            if live_source is not None:
                await live_source.close()
        print(
            f"link {outcome.reason} "
            f"(sessions={outcome.sessions} dispatches={outcome.dispatches} "
            f"results={outcome.results} events={outcome.pushed_events} "
            f"outputs={outcome.pushed_outputs})"
        )
        return 0 if outcome.reason in ("shutdown", "once-done") else 1

    try:
        return asyncio.run(serve())
    except KeyboardInterrupt:
        print("link shutdown (interrupt)")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
