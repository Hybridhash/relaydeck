"""Durable stores, storage/privacy policy, and death simulation.

STORAGE POLICY (exact placement + justification):

- SECURE STORAGE ONLY (EncryptedSharedPreferences via the CredentialStore
  port): access tokens, refresh tokens, PKCE verifiers, device
  enrollment keys, push provider tokens, and COMPOSER DRAFTS.
- ORDINARY PREFERENCES (plaintext; route/theme only): `route` (opaque
  destination + selection keys, never tokens or titles), `theme`,
  `text_scale`, `keyboard_mode`.

Justification: drafts may contain commands, repository paths, pasted
terminal output, and pasted secrets. Ordinary preferences are plaintext
XML included in backups and readable on a rooted device, and
approves no plaintext store for sensitive data — so drafts go to secure
storage alongside tokens. ("keep drafts safe without storing
access tokens in ordinary preferences") is satisfied strictly: neither
drafts nor tokens ever touch ordinary preferences. If secure storage is
unavailable, drafts are kept in memory only and pause reports a
capability failure rather than downgrading to plaintext.

DEEP-LINK / PUSH REFERENCES: notification payloads and deep links carry
an opaque event reference only (no transcript, path, command,
secret, or decision). The opaque ref MAY be held in memory or ordinary
preferences transiently, but it is never trusted: tapping re-validates
login, fetches the event, and only then displays. A logged-out or
revoked device cannot use a stale ref to retrieve content.

PROCESS DEATH: `simulate_death` drops every in-memory holder (writers,
outboxes, queues, cursors, snapshots) and restores route-only state:
the stored route string plus secure-storage drafts/tokens. Unacked
input becomes UNKNOWN and is never auto-resent (no double input across
death). Secrets must be absent from ordinary preferences afterwards
(`audit_stores` asserts both stores).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

#: The exact storage policy. Quoted by tests and the device-suite script.
STORAGE_POLICY = (
    "Access/refresh tokens, PKCE verifiers, device keys, push tokens, and "
    "composer drafts live in SECURE storage only; ordinary preferences hold "
    "only the opaque route string, theme, text scale, and keyboard mode. "
    "Drafts go to secure storage because they may contain commands, paths, "
    "and pasted secrets, and no plaintext store is approved for sensitive "
    "data. Opaque push/deep-link refs may be held transiently but "
    "are always re-validated after login; a logged-out or revoked device "
    "cannot use a stale ref. Secure-storage outage keeps drafts in memory "
    "and reports a capability failure; it never downgrades to plaintext."
)

#: Keys that may appear in ordinary (plaintext) preferences.
ORDINARY_SAFE_KEYS = frozenset({"route", "theme", "text_scale", "keyboard_mode"})

#: Key prefixes that must NEVER appear in ordinary preferences.
SECURE_ONLY_KEYS = frozenset(
    {
        "access_token",
        "refresh_token",
        "pkce_verifier",
        "device_key",
        "push_token",
        "draft:",
    }
)

#: Documented force-stop limits (platform behavior). Source: Android background-start restrictions
#: https://developer.android.com/develop/background-work/services/fgs/restrictions-bg-start
#: Device proof is blocked (see scripts/check_device_scenarios.py --suite lifecycle).
FORCE_STOP_LIMITS = (
    "After a user force-stop the app is in the stopped state and NOTHING "
    "works until the user manually relaunches it: no Dart/Python execution, "
    "no broadcast receivers, no services, no jobs/alarms, and no FCM "
    "delivery (provider messages are held at best and subject to TTL; the "
    "app cannot wake itself). After process death WITHOUT force-stop the "
    "app still cannot start a foreground service from the background "
    "(Android 12+ raises for background starts outside narrow exemptions "
    "such as a user tapping a notification), so resume/sync only run when "
    "the user returns or taps. After reboot nothing runs until the user "
    "unlocks/launches (no BOOT receiver is claimed here). Doze/battery "
    "restriction defer network and jobs; push stays best-effort and the "
    "session inbox stays the source of truth."
)


@dataclass
class DurableStores:
    """The two durable stores, modeled as plain dicts for tests.

    `ordinary` simulates plaintext preferences; `secure` simulates
    EncryptedSharedPreferences. Production wires the real CredentialStore
    port for `secure`; nothing in this package touches Flet.
    """

    ordinary: dict[str, str] = field(default_factory=dict)
    secure: dict[str, str] = field(default_factory=dict)


@dataclass
class ProcessMemory:
    """Everything the process holds that death destroys.

    - `writer_bindings`: live writer binding ids (see `writer.WriterGate`).
    - `outbox`: queued-but-unsent input chunks (oldest first).
    - `unacked_input`: input-seq ids sent without an ACK boundary.
    - `cursors`: last stream cursors per host.
    - `snapshots`: last snapshots per host.
    - `pending_commands`: outstanding command ids awaiting status.
    """

    writer_bindings: list[int] = field(default_factory=list)
    outbox: list[bytes] = field(default_factory=list)
    unacked_input: list[str] = field(default_factory=list)
    cursors: dict[str, str] = field(default_factory=dict)
    snapshots: dict[str, dict[str, Any]] = field(default_factory=dict)
    pending_commands: list[str] = field(default_factory=list)

    def drop_all(self) -> None:
        """Forget every in-memory holder (process death)."""
        self.writer_bindings.clear()
        self.outbox.clear()
        self.unacked_input.clear()
        self.cursors.clear()
        self.snapshots.clear()
        self.pending_commands.clear()

    def is_empty(self) -> bool:
        return not (
            self.writer_bindings
            or self.outbox
            or self.unacked_input
            or self.cursors
            or self.snapshots
            or self.pending_commands
        )


@dataclass(frozen=True)
class DeathReport:
    """What process death did: unknown inputs, route-only restore."""

    unknown_inputs: tuple[str, ...] = ()
    dropped_outbox_chunks: int = 0
    route_restored: str = ""
    secrets_in_ordinary: bool = False


def simulate_death(memory: ProcessMemory, durable: DurableStores) -> DeathReport:
    """Drop all in-memory state; restore route-only durable state.

    Every unacked input id becomes UNKNOWN (returned in the report for
    the UI to disclose); the outbox is dropped WITHOUT resending (no
    double input across death). Only the stored route string is read
    back; snapshots, cursors, and writers are refetched/re-acquired by
    the resume flow, never restored from memory.
    """
    unknown = tuple(memory.unacked_input)
    dropped = len(memory.outbox)
    memory.drop_all()
    route = durable.ordinary.get("route", "")
    secrets = any(
        key == secure or key.startswith(secure)
        for key in durable.ordinary
        for secure in SECURE_ONLY_KEYS
    )
    return DeathReport(
        unknown_inputs=unknown,
        dropped_outbox_chunks=dropped,
        route_restored=route,
        secrets_in_ordinary=secrets,
    )


def simulate_config_change(memory: ProcessMemory) -> ProcessMemory:
    """Model rotation/keyboard-open: the process (and every holder) is
    retained by identity. Returns the SAME object: no writer duplication,
    no resend, no state reset."""
    return memory


def audit_stores(
    ordinary: dict[str, str],
    secure: dict[str, str],
    *,
    known_secrets: tuple[str, ...] = (),
) -> list[str]:
    """Audit both stores against the storage policy.

    Returns violation strings (empty when clean):
    - any secure-only key (or `draft:`-prefixed key) present in ordinary
      preferences;
    - any unknown key in ordinary preferences (allowlist is exact);
    - any known secret VALUE appearing anywhere in ordinary preferences
      (catches a token stored under an innocent key).
    `secure` is inspected (its keys enumerated) but never flagged: secure
    storage is the approved home for every sensitive value.
    """
    violations: list[str] = []
    _ = sorted(secure.items())
    for key in sorted(ordinary):
        if key in ORDINARY_SAFE_KEYS:
            continue
        if key in SECURE_ONLY_KEYS or any(
            key.startswith(prefix) for prefix in SECURE_ONLY_KEYS if prefix.endswith(":")
        ):
            violations.append(f"ordinary preferences hold secure-only key {key!r}")
        else:
            violations.append(f"ordinary preferences hold unknown key {key!r}")
    for key in sorted(ordinary):
        for secret in known_secrets:
            if secret and secret in ordinary[key]:
                violations.append(f"ordinary preferences leak a secret value in {key!r}")
    return violations
