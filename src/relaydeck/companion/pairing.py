"""Protected Mac-local approval; no HTTP owner/admin endpoint."""

from __future__ import annotations

import argparse
import time

from relaydeck.companion.config import CompanionConfig
from relaydeck.contracts.pairing import DEVICE_CAPABILITIES
from relaydeck.gateway.hosts import HostStore
from relaydeck.gateway.pairing import PairingError, PairingStore, known_host


def run_pairing(config: CompanionConfig, args: argparse.Namespace) -> int:
    store = PairingStore(config.state_dir / "pairing.json")
    if args.pairing_action == "close":
        store.close_window()
        print("Device pairing closed; pending requests and approvals discarded.")
        return 0
    host = known_host(
        HostStore(path=config.state_dir / "hosts.json").record_for_host(config.host_id)
    )
    now = int(time.time() * 1000)
    if args.pairing_action == "open":
        store.open_window(host, config.advertised_origin, now, args.ttl_seconds)
        print(f"Device pairing open for {args.ttl_seconds}s at {config.advertised_origin}")
        print(f"Host {ascii(host.host_id)}; fingerprint {ascii(host.fingerprint)}")
        print("Scope: " + ", ".join(DEVICE_CAPABILITIES) + "; selected host only")
        print("Compare the request's matching code on both devices before approving.")
        return 0
    rows = store.pending(host, config.advertised_origin, now)
    if args.pairing_action == "approve" and not args.request:
        # Approve only an unambiguous match: exactly one pending request with this code.
        matches = [row for row in rows if row["matching_code"] == args.code]
        if len(matches) != 1:
            print(
                f"No single pending request matches code {args.code}; "
                f"{len(matches)} matched. Nothing was approved. Candidates:"
            )
            for row in rows:
                print(
                    f"  request {row['request_id']}; code {row['matching_code']}; "
                    f"label (untrusted) {ascii(row['device_label'])}; expires {row['expires_at']}"
                )
            if not rows:
                print("  (no pending device requests)")
            return 1
        args.request = matches[0]["request_id"]
    if args.pairing_action == "deny" and not args.request:
        raise ValueError("deny needs --request <id>; nothing was denied")
    if args.pairing_action != "list":
        rows = [row for row in rows if row["request_id"] == args.request]
        if not rows:
            raise PairingError()
    for row in rows:
        # Labels are untrusted; ascii() escapes terminal controls/bidi/non-ASCII.
        print(f"Request {row['request_id']}; matching code {row['matching_code']}")
        print(
            f"Device label (untrusted): {ascii(row['device_label'])}; identity {row['device_id']}"
        )
        print(f"Host {ascii(row['host_id'])}; fingerprint {ascii(row['host_fingerprint'])}")
        print("Scope: " + ", ".join(row["capabilities"]) + "; selected host only")
        print(f"State {row['state']}; expires {row['expires_at']}")
    if args.pairing_action == "list":
        if not rows:
            print("No pending device requests.")
        return 0
    store.decide(
        args.request,
        args.code,
        host,
        config.advertised_origin,
        now,
        approve=args.pairing_action == "approve",
    )
    print(
        "Device request approved." if args.pairing_action == "approve" else "Device request denied."
    )
    return 0
