"""Gateway operator CLI (no HTTP admin surface by design).

Runs on the gateway host: issues pubkey-bound enrollment invitations,
revokes hosts, and serves the app. `serve` binds loopback only and
wires the durable journal + push stores when given database paths; without them it runs the
in-memory demo store with a loud warning. `issue-enrollment --store` shares one invitation
file with a serving `serve --enrollment-store` (the documented ceremony);
`revoke-host --spool` queues a single-host revoke the serving process
drains. `list-hosts` still needs serving-process memory (no channel).
"""

from __future__ import annotations

import argparse
import os
import secrets
import sys
import time
from dataclasses import dataclass
from typing import Any


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="relaydeck-gateway")
    sub = parser.add_subparsers(dest="command", required=True)
    issue = sub.add_parser("issue-enrollment", help="Mint a pubkey-bound invitation")
    issue.add_argument("--host-label", required=True, help="Display label for the home host")
    issue.add_argument("--pubkey", required=True, help="Connector ssh-ed25519 public line")
    issue.add_argument("--account", default="owner", help="Account to bind the host to")
    issue.add_argument("--origin", default="", help="Public gateway origin for the QR")
    issue.add_argument("--ttl-seconds", type=int, default=600)
    issue.add_argument(
        "--store",
        default="",
        help="Shared invitation file (0600); must match serve --enrollment-store",
    )
    sub.add_parser("list-hosts", help="List enrolled hosts")
    revoke = sub.add_parser("revoke-host", help="Queue a single-host revoke")
    revoke.add_argument("--host", required=True)
    revoke.add_argument(
        "--spool",
        default="",
        help="Revoke spool dir drained by the serving process",
    )
    serve = sub.add_parser("serve", help="Run the gateway server (loopback only)")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--origin", default="", help="Public origin advertised in QR codes")
    serve.add_argument("--journal-path", default="", help="Durable command journal (sqlite, 0600)")
    serve.add_argument("--push-path", default="", help="Durable push store (sqlite, 0600)")
    serve.add_argument(
        "--fcm-credentials",
        default="",
        help="Owner-only FCM service-account JSON; disabled unless set",
    )
    serve.add_argument("--identity-config", default="", help="Owner-only reviewed OIDC JSON")
    serve.add_argument(
        "--cursor-secret", default="", help="Metadata cursor HMAC secret (>= 32 bytes)"
    )
    serve.add_argument("--cursor-secret-file", default="", help="File holding the cursor secret")
    serve.add_argument(
        "--log-config", default="", help="Uvicorn logging config (query-token redaction)"
    )
    serve.add_argument(
        "--enrollment-store",
        default="",
        help="Shared invitation file (0600) for operator-issued enrollments",
    )
    serve.add_argument("--revoke-spool", default="", help="Drain local host-revocation requests")
    serve.add_argument("--host-store", default="", help="Private durable host-token hash registry")
    serve.add_argument("--pairing-store", default="", help="Private local device pairing state")
    return parser


LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
MIN_SECRET_BYTES = 32


class ServeConfigError(ValueError):
    pass


@dataclass(frozen=True)
class ServeOptions:
    host: str
    port: int
    origin: str
    journal_path: str
    push_path: str
    cursor_secret: bytes
    log_config: str
    ephemeral_secret: bool
    enrollment_store: str = ""
    revoke_spool: str = ""
    host_store: str = ""
    fcm_credentials: str = ""
    identity_config: str = ""
    pairing_store: str = ""


def resolve_serve_options(args: argparse.Namespace) -> ServeOptions:
    """Validate `serve` flags. Raises ServeConfigError on usage errors."""
    if args.host not in LOOPBACK_HOSTS:
        raise ServeConfigError(f"refusing non-loopback bind {args.host!r} (loopback only)")
    if not 1 <= args.port <= 65535:
        raise ServeConfigError(f"bad port {args.port!r}")
    secret_text = args.cursor_secret
    if args.cursor_secret_file:
        try:
            with open(args.cursor_secret_file, encoding="utf-8") as handle:
                secret_text = handle.read().strip()
        except OSError as exc:
            raise ServeConfigError(f"cannot read --cursor-secret-file: {exc}") from exc
    if not secret_text:
        secret_text = os.environ.get("RELAYDECK_CURSOR_SECRET", "")
    ephemeral = not secret_text
    if ephemeral:
        secret = secrets.token_bytes(MIN_SECRET_BYTES)
    else:
        secret = secret_text.encode()
        if len(secret) < MIN_SECRET_BYTES:
            raise ServeConfigError("cursor secret must be >= 32 bytes")
    return ServeOptions(
        host=args.host,
        port=args.port,
        origin=args.origin,
        journal_path=args.journal_path,
        push_path=args.push_path,
        cursor_secret=secret,
        log_config=args.log_config,
        ephemeral_secret=ephemeral,
        enrollment_store=args.enrollment_store,
        revoke_spool=args.revoke_spool,
        host_store=args.host_store,
        fcm_credentials=getattr(args, "fcm_credentials", ""),
        identity_config=getattr(args, "identity_config", ""),
        pairing_store=getattr(args, "pairing_store", ""),
    )


def build_serve_state(options: ServeOptions) -> Any:
    """Build the serving GatewayState: JournalBackend + file-backed
    PushStore when database paths are given, else the in-memory demo
    store (loudly documented by the caller, never silent). With
    --enrollment-store the invitation registry is the shared file the
    operator CLI writes (the documented ceremony); otherwise HTTP-
    created invitations stay in process memory."""
    from contextlib import ExitStack
    from pathlib import Path

    from relaydeck.gateway.app import GatewayState
    from relaydeck.gateway.hosts import HostStore
    from relaydeck.gateway.identity import EnrollmentStore

    with ExitStack() as cleanup:
        if options.journal_path:
            from relaydeck.gateway.production import JournalBackend
            from relaydeck.journal import CommandJournal

            journal = CommandJournal(options.journal_path)
            cleanup.callback(journal.close)
            backend: Any = JournalBackend(journal)
        else:
            from relaydeck.gateway.backend import FakeBackend

            backend = FakeBackend(now_ms=lambda: int(time.time() * 1000))
        if options.push_path:
            from relaydeck.gateway.push import PushStore

            push = PushStore(options.push_path)
        else:
            from relaydeck.gateway.push import PushStore

            push = PushStore()
        cleanup.callback(push.close)
        provider = None
        if options.fcm_credentials:
            if not options.push_path:
                raise ServeConfigError("FCM delivery requires a durable --push-path")
            from relaydeck.gateway.fcm import FcmProvider

            provider = FcmProvider.from_file(Path(options.fcm_credentials).expanduser())
            cleanup.callback(provider.close)
        enrollments = EnrollmentStore(
            path=options.enrollment_store or None,
        )
        identity_provider = None
        if options.identity_config:
            from relaydeck.gateway.authorization import OidcProvider

            identity_provider = OidcProvider.from_file(
                Path(options.identity_config).expanduser(), lambda: int(time.time() * 1000)
            )
        pairing = None
        if options.pairing_store:
            from relaydeck.gateway.pairing import PairingStore

            pairing = PairingStore(Path(options.pairing_store).expanduser())
            pairing.start(options.origin)
        state = GatewayState(
            backend=backend,
            cursor_secret=options.cursor_secret,
            now_ms=lambda: int(time.time() * 1000),
            public_origin=options.origin,
            push=push,
            push_provider=provider,
            identity_provider=identity_provider,
            pairing=pairing,
            enrollments=enrollments,
            revoke_spool=options.revoke_spool,
            hosts=HostStore(path=Path(options.host_store) if options.host_store else None),
        )

        cleanup.pop_all()
        return state


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "issue-enrollment":
        from relaydeck.connector.pubkey import (
            build_qr_payload,
            fingerprint_of_raw,
            openssh_to_raw,
            raw_to_openssh,
        )
        from relaydeck.gateway.identity import EnrollmentStore

        try:
            raw = openssh_to_raw(args.pubkey)
        except ValueError as exc:
            print(f"bad pubkey: {exc}")
            return 1
        now_ms = int(time.time() * 1000)
        invitation = EnrollmentStore(path=args.store or None).create(
            args.account,
            args.host_label,
            raw_to_openssh(raw),
            fingerprint_of_raw(raw),
            now_ms,
            args.ttl_seconds * 1000,
        )
        from datetime import UTC, datetime

        expires_at = datetime.fromtimestamp(invitation.expires_ms / 1000, UTC).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        qr = build_qr_payload(args.origin, invitation.id, invitation.code, expires_at)
        print(f"id {invitation.id}")
        print(f"code {invitation.code[:4]}-{invitation.code[4:8]}-{invitation.code[8:]}")
        print(f"fingerprint {invitation.fingerprint}")
        print(f"qr {qr}")
        try:
            import qrcode

            matrix = qrcode.QRCode(border=1)
            matrix.add_data(qr)
            matrix.print_ascii(invert=True)
        except ImportError:
            print("(install the gateway group for ASCII QR rendering)", file=sys.stderr)
        if args.store:
            print(f"persisted to {args.store} (0600 shared store)", file=sys.stderr)
        else:
            print(
                "WARNING: single-process demo store; pass --store <file> matching the "
                "serving gateway's --enrollment-store so the invitation redeems.",
                file=sys.stderr,
            )
        return 0
    if args.command == "list-hosts":
        print("list-hosts: requires the serving process store (no operator channel yet).")
        return 2
    if args.command == "revoke-host":
        if not args.spool:
            print("revoke-host: pass --spool <dir> drained by the serving process.")
            return 2
        from relaydeck.gateway.hosts import write_revoke_request

        try:
            request_path = write_revoke_request(args.spool, args.host)
        except ValueError as exc:
            print(f"revoke-host: {exc}")
            return 2
        print(f"revoke queued for {args.host} ({request_path})")
        return 0
    if args.command == "serve":
        import uvicorn

        from relaydeck.gateway.app import create_app

        try:
            options = resolve_serve_options(args)
        except ServeConfigError as exc:
            print(f"serve: {exc}")
            return 2
        if not options.journal_path:
            print(
                "WARNING: in-memory demo store (no --journal-path); "
                "sessions, hosts, journals, and leases die on restart.",
                file=sys.stderr,
            )
        if options.ephemeral_secret:
            print(
                "WARNING: ephemeral cursor secret; cursors die on restart "
                "(set --cursor-secret, --cursor-secret-file, or RELAYDECK_CURSOR_SECRET).",
                file=sys.stderr,
            )
        state = build_serve_state(options)
        print(f"serving gateway on http://{options.host}:{options.port} (loopback only)")
        uvicorn.run(
            create_app(state),
            host=options.host,
            port=options.port,
            log_config=options.log_config or None,
            access_log=False,
        )
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
