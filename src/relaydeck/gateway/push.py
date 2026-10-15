"""Provider outbox, device subscriptions, and deep links. Device and
build proofs are environment-blocked (no Flutter/Dart, JDK, Android SDK,
or Firebase project here); this module is the verifiable contract core.

Rules (acceptance-documented):

* Eligibility: the server decides from authenticated host state.
  `notify()` fans out only to the account's ACTIVE devices and only
  when the host is in the caller's authorized host set. There is NO
  client-callable enqueue route: pushes originate server-side from
  verified host events, never from a device request.
* Payloads: `hint_payload()` emits EXACTLY `HINT_KEYS` (mirrored by the
  extension guard; the audit test asserts equality). Values are opaque
  random refs plus category/timestamps -- never terminal text, paths,
  commands, secrets, or decisions. Adversarial event/host/session ids
  cannot leak through because payloads carry refs, not ids.
* Dedupe: UNIQUE(account, device, event) -- the server never enqueues
  the same event twice per device. The dedupe window is TTL + terminal
  retention; a re-enqueue after purge is a NEW notification (same
  documented-window shape as the command journal).
* TTL/backoff: pending rows past expiry are dropped, never sent.
  Provider failures retry with exponential backoff inside TTL
  (`backoff_ms`); best effort, documented. Per-device FIFO order.
* Rotation: token rotation updates the SAME device record (never a new
  subscription). Logout DELETES the device row (subscription + token
  association gone). Reinstall (new install id) tombstones the old row
  (`replaced_by`); replaced rows never revive.
* Token-invalid (provider permanent failure) tombstones the row WITHOUT
  `replaced_by`: a later register/rotate for the same install revives
  it, because the install proved liveness with a fresh token.
* Deep links: minted alongside each notification, opaque (`rdl_...`),
  single-purpose (`open-event`), expiring. Resolution requires the
  owning account and an ACTIVE device; logged-out (no auth, enforced at
  the route), revoked, replaced, forged, expired, foreign-account, and
  wrong-purpose links fail closed with no content. Single-purpose is
  NOT single-use: re-opening re-resolves and re-fetches current state.
* Tapping never sends input: the tap path is resolve (refs) -> fetch
  current authorized state -> route. No command, key, or approval is
  emitted anywhere on this path; the e2e test asserts empty backend
  input/command logs.
* Retention: terminal outbox rows (sent/dropped) purged after
  TERMINAL_KEEP_MS; expired links after LINK_KEEP_MS past expiry;
  device tombstones after DEVICE_TOMBSTONE_MS; orphan refs with the
  terminal sweep. Durability mirrors the journal: 0600 file, WAL +
  synchronous=FULL, one commit choke point, sidecar re-chmod.

FCM mapping (per the Firebase Flutter SDK receive-messages documentation):
onTokenRefresh -> rotate_token; deleteToken on logout -> revoke_device;
getInitialMessage/onMessageOpenedApp -> tap -> resolve_hint; the
top-level @pragma('vm:entry-point') background handler already exists in
the Dart service and executes nothing. Data payloads carry only the
allowlisted keys; display text stays generic.
"""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
import stat
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

# Exact wire key set. Asserted equal to the extension guard's
# ALLOWED_KEYS by the lock-screen audit test (gateway stays stdlib-only
# so it never inherits the extension's flet dependency).
HINT_KEYS = frozenset(
    {
        "notification_id",
        "device_ref",
        "host_ref",
        "session_ref",
        "event_ref",
        "category",
        "created_at",
        "expires_at",
    }
)
HINT_CATEGORIES = frozenset({"attention", "completion"})
# the choices a device can make, mapped onto the stored category names. The default
# for a device without a row is every category.
PREF_UI_TO_CATEGORY = {"blocked": "attention", "done": "completion"}
PREF_CATEGORY_TO_UI = {value: key for key, value in PREF_UI_TO_CATEGORY.items()}
PLATFORMS = frozenset({"android"})

LINK_PURPOSE_OPEN = "open-event"
LINK_PATH_PREFIX = "/app/n/"
LINK_TOKEN_PREFIX = "rdl_"

DEFAULT_PUSH_TTL_MS = 24 * 60 * 60 * 1000
TERMINAL_KEEP_MS = 24 * 60 * 60 * 1000
LINK_KEEP_MS = 24 * 60 * 60 * 1000
DEVICE_TOMBSTONE_MS = 30 * 24 * 60 * 60 * 1000
BACKOFF_BASE_MS = 30_000
BACKOFF_CAP_MS = 15 * 60_000

MAX_DEVICE_ID_CHARS = 128
MAX_TOKEN_CHARS = 4096
MAX_EVENT_ID_CHARS = 256
MAX_HOST_ID_CHARS = 128
MAX_SESSION_ID_CHARS = 128
MAX_VALUE_LEN = 512
MAX_ERROR_CHARS = 256

TERMINAL_EVENT_STATUSES = frozenset({"completed", "resolved", "closed", "archived", "failed"})

_SCHEMA = """
CREATE TABLE IF NOT EXISTS push_meta (k TEXT PRIMARY KEY, v TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS push_devices (
  account TEXT NOT NULL,
  device_id TEXT NOT NULL,
  platform TEXT NOT NULL,
  token TEXT NOT NULL DEFAULT '',
  device_ref TEXT NOT NULL,
  status TEXT NOT NULL,
  replaced_by TEXT NOT NULL DEFAULT '',
  registered_ms INTEGER NOT NULL,
  updated_ms INTEGER NOT NULL,
  PRIMARY KEY (account, device_id)
);
CREATE TABLE IF NOT EXISTS push_refs (
  ref TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  value TEXT NOT NULL,
  created_ms INTEGER NOT NULL,
  UNIQUE (kind, value)
);
CREATE TABLE IF NOT EXISTS push_outbox (
  notification_id TEXT PRIMARY KEY,
  account TEXT NOT NULL,
  device_id TEXT NOT NULL,
  device_ref TEXT NOT NULL,
  host_ref TEXT NOT NULL,
  session_ref TEXT NOT NULL,
  event_id TEXT NOT NULL,
  event_ref TEXT NOT NULL,
  category TEXT NOT NULL,
  created_ms INTEGER NOT NULL,
  expires_ms INTEGER NOT NULL,
  status TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0,
  next_attempt_ms INTEGER NOT NULL DEFAULT 0,
  last_error TEXT NOT NULL DEFAULT '',
  link_token TEXT NOT NULL,
  UNIQUE (account, device_id, event_id)
);
CREATE INDEX IF NOT EXISTS idx_outbox_due
  ON push_outbox(status, next_attempt_ms, device_id, created_ms);
CREATE TABLE IF NOT EXISTS push_prefs (
  account TEXT NOT NULL,
  device_id TEXT NOT NULL,
  categories TEXT NOT NULL,
  updated_ms INTEGER NOT NULL,
  PRIMARY KEY (account, device_id)
);
CREATE TABLE IF NOT EXISTS push_links (
  token TEXT PRIMARY KEY,
  account TEXT NOT NULL,
  device_id TEXT NOT NULL,
  notification_id TEXT NOT NULL,
  host_ref TEXT NOT NULL,
  session_ref TEXT NOT NULL,
  event_ref TEXT NOT NULL,
  category TEXT NOT NULL,
  purpose TEXT NOT NULL,
  created_ms INTEGER NOT NULL,
  expires_ms INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_links_notification
  ON push_links(notification_id);
"""


class PushDenied(Exception):
    """Eligibility/validation refusal with a gateway envelope code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ProviderOutcome(Enum):
    SENT = "sent"
    RETRYABLE = "retryable"
    PERMANENT = "permanent"


class ProviderPort(ABC):
    """Push provider interface; production FCM is explicitly configured."""

    @abstractmethod
    def send(self, token: str, payload: dict[str, str]) -> ProviderOutcome:
        """Deliver one hint. RETRYABLE = try again within TTL;
        PERMANENT = token dead, tombstone the device."""


class StubProvider(ProviderPort):
    """Scripted provider for headless tests. Script entries may be
    outcomes or Exceptions (raised from send, treated as retryable)."""

    def __init__(
        self,
        script: list[ProviderOutcome | Exception] | None = None,
        default: ProviderOutcome = ProviderOutcome.SENT,
    ) -> None:
        self._script = list(script) if script else []
        self._default = default
        self.sends: list[tuple[str, dict[str, str]]] = []

    def send(self, token: str, payload: dict[str, str]) -> ProviderOutcome:
        self.sends.append((token, dict(payload)))
        if self._script:
            next_item = self._script.pop(0)
            if isinstance(next_item, Exception):
                raise next_item
            return next_item
        return self._default


@dataclass(frozen=True)
class DeviceRecord:
    account: str
    device_id: str
    platform: str
    token: str
    device_ref: str
    status: str  # "active" | "tombstoned"
    replaced_by: str
    registered_ms: int
    updated_ms: int


def backoff_ms(attempts: int) -> int:
    """Exponential backoff after `attempts` failures (attempts >= 1):
    30s, 60s, 120s, ... capped at 15 minutes."""
    if attempts < 1:
        return 0
    return min(BACKOFF_CAP_MS, BACKOFF_BASE_MS * int(2 ** (attempts - 1)))


def hint_relevance(category: str, current_status: str) -> str:
    """Stale-hint relevance check against CURRENT metadata.

    Opening always re-fetches; this classifies the display: an attention
    hint for a terminal event is stale (show current state, not the
    hint's implied urgency). Returns "relevant" or "stale".
    """
    if current_status in TERMINAL_EVENT_STATUSES:
        return "stale" if category == "attention" else "relevant"
    return "relevant"


def link_url(public_origin: str, token: str) -> str:
    """App-link URL for a minted token. Matches the manifest
    pathPrefix /app/ (android_manifest.md)."""
    return f"{public_origin.rstrip('/')}{LINK_PATH_PREFIX}{token}"


def token_from_url(url: str) -> str | None:
    """Extract a deep-link token from an app-link URL. Anything that is
    not exactly /app/n/{token} fails closed (None)."""
    try:
        path = urlparse(url).path
    except ValueError:
        return None
    if not path.startswith(LINK_PATH_PREFIX):
        return None
    token = path[len(LINK_PATH_PREFIX) :]
    if "/" in token or not token.startswith(LINK_TOKEN_PREFIX):
        return None
    if len(token) < len(LINK_TOKEN_PREFIX) + 16 or len(token) > 128:
        return None
    if any(
        c not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_" for c in token
    ):
        return None
    return token


def _utc_z(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _utcnow_ms() -> int:
    return int(time.time() * 1000)


def _error_text(error: str) -> str:
    return error[:MAX_ERROR_CHARS]


class PushStore:
    """Durable device subscriptions + outbox + deep links (one sqlite
    file; `:memory:` for tests that do not need file durability)."""

    def __init__(
        self, path: str | Path = ":memory:", *, clock: Callable[[], int] | None = None
    ) -> None:
        self._clock = clock or _utcnow_ms
        self._lock = threading.Lock()
        raw = str(path)
        self._memory = raw == ":memory:"
        if self._memory:
            self._path = Path(":memory:")
            self._db = sqlite3.connect(":memory:", check_same_thread=False)
        else:
            self._path = Path(os.path.expanduser(raw))
            self._path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self._path, os.O_CREAT | os.O_RDWR, stat.S_IRUSR | stat.S_IWUSR)
            os.close(fd)
            os.chmod(self._path, stat.S_IRUSR | stat.S_IWUSR)
            self._db = sqlite3.connect(str(self._path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.executescript(_SCHEMA)
            self._db.execute("PRAGMA journal_mode=WAL;")
            self._db.execute("PRAGMA synchronous=FULL;")
            row = self._db.execute("SELECT v FROM push_meta WHERE k='version'").fetchone()
            if row is None:
                self._db.execute("INSERT INTO push_meta (k, v) VALUES ('version', '1')")
            elif row["v"] != "1":
                raise RuntimeError(f"push schema v{row['v']} != v1")
            self._commit()
        self._chmod_sidecars()

    def _commit(self) -> None:
        self._db.commit()
        self._chmod_sidecars()

    def _chmod_sidecars(self) -> None:
        if self._memory:
            return
        for suffix in ("-wal", "-shm"):
            try:
                sidecar = Path(str(self._path) + suffix)
                if sidecar.exists():
                    os.chmod(sidecar, stat.S_IRUSR | stat.S_IWUSR)
            except OSError:
                pass

    @property
    def path(self) -> Path:
        return self._path

    def durability(self) -> dict[str, Any]:
        with self._lock:
            mode = self._db.execute("PRAGMA journal_mode;").fetchone()[0]
            sync = self._db.execute("PRAGMA synchronous;").fetchone()[0]
            return {"journal_mode": str(mode).lower(), "synchronous": int(sync)}

    def now(self) -> int:
        return self._clock()

    def close(self) -> None:
        with self._lock:
            try:
                self._db.execute("PRAGMA wal_checkpoint(TRUNCATE);")
                self._commit()
            finally:
                self._db.close()

    @staticmethod
    def _row(row: sqlite3.Row) -> dict[str, Any]:
        return {key: row[key] for key in row.keys()}

    # -- device subscriptions -------------------------------------------

    def _device_from(self, row: dict[str, Any]) -> DeviceRecord:
        return DeviceRecord(
            account=str(row["account"]),
            device_id=str(row["device_id"]),
            platform=str(row["platform"]),
            token=str(row["token"]),
            device_ref=str(row["device_ref"]),
            status=str(row["status"]),
            replaced_by=str(row["replaced_by"]),
            registered_ms=int(row["registered_ms"]),
            updated_ms=int(row["updated_ms"]),
        )

    @staticmethod
    def _check_device_fields(device_id: str, token: str, platform: str) -> None:
        if not device_id or len(device_id) > MAX_DEVICE_ID_CHARS:
            raise PushDenied("forbidden", "bad device id")
        if not token or len(token) > MAX_TOKEN_CHARS:
            raise PushDenied("forbidden", "bad device token")
        if platform not in PLATFORMS:
            raise PushDenied("forbidden", "unsupported platform")

    def register_device(
        self,
        account: str,
        device_id: str,
        token: str,
        platform: str,
        now: int | None = None,
    ) -> DeviceRecord:
        """Register or re-register an install. Same (account, device)
        updates the SAME record (rotation path); a token-invalid
        tombstone revives, a replaced (reinstall) tombstone never does."""
        self._check_device_fields(device_id, token, platform)
        at = self._clock() if now is None else now
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM push_devices WHERE account=? AND device_id=?",
                (account, device_id),
            ).fetchone()
            if row is not None:
                current = self._row(row)
                if current["status"] == "tombstoned" and current["replaced_by"]:
                    raise PushDenied("not_found", "device replaced by reinstall")
                self._db.execute(
                    "UPDATE push_devices SET platform=?, token=?, status='active',"
                    " replaced_by='', updated_ms=? WHERE account=? AND device_id=?",
                    (platform, token, at, account, device_id),
                )
            else:
                self._db.execute(
                    "INSERT INTO push_devices (account, device_id, platform, token,"
                    " device_ref, status, registered_ms, updated_ms)"
                    " VALUES (?,?,?,?,?,'active',?,?)",
                    (
                        account,
                        device_id,
                        platform,
                        token,
                        f"dr_{secrets.token_urlsafe(16)}",
                        at,
                        at,
                    ),
                )
            self._commit()
            fresh = self._db.execute(
                "SELECT * FROM push_devices WHERE account=? AND device_id=?",
                (account, device_id),
            ).fetchone()
            return self._device_from(self._row(fresh))

    def rotate_token(
        self, account: str, device_id: str, token: str, now: int | None = None
    ) -> DeviceRecord | None:
        """FCM onTokenRefresh: update the SAME record. Unknown, revoked
        (deleted), or reinstall-replaced rows stay dead (None); a
        token-invalid tombstone revives on proof of liveness."""
        if not token or len(token) > MAX_TOKEN_CHARS:
            raise PushDenied("forbidden", "bad device token")
        at = self._clock() if now is None else now
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM push_devices WHERE account=? AND device_id=?",
                (account, device_id),
            ).fetchone()
            if row is None:
                return None
            current = self._row(row)
            if current["status"] == "tombstoned" and current["replaced_by"]:
                return None
            self._db.execute(
                "UPDATE push_devices SET token=?, status='active', updated_ms=?"
                " WHERE account=? AND device_id=?",
                (token, at, account, device_id),
            )
            self._commit()
            fresh = self._db.execute(
                "SELECT * FROM push_devices WHERE account=? AND device_id=?",
                (account, device_id),
            ).fetchone()
            return self._device_from(self._row(fresh))

    def device_metadata(self, account: str) -> tuple[dict[str, Any], ...]:
        """Bounded account-owned management metadata; no delivery tokens or refs."""
        with self._lock:
            rows = self._db.execute(
                "SELECT device_id,platform,status,registered_ms,updated_ms FROM push_devices"
                " WHERE account=? ORDER BY updated_ms DESC LIMIT 201",
                (account,),
            ).fetchall()
            return tuple(self._row(row) for row in rows)

    def _prefs_locked(self, account: str, device_id: str) -> frozenset[str]:
        """Caller holds the lock. A device without a row receives every category."""
        row = self._db.execute(
            "SELECT categories FROM push_prefs WHERE account=? AND device_id=?",
            (account, device_id),
        ).fetchone()
        if row is None:
            return HINT_CATEGORIES
        stored = json.loads(str(row["categories"]))
        return frozenset(str(item) for item in stored) & HINT_CATEGORIES

    def preferences(self, account: str, device_id: str) -> frozenset[str]:
        """The categories this device receives (internal names: attention, completion)."""
        with self._lock:
            return self._prefs_locked(account, device_id)

    def set_preferences(
        self, account: str, device_id: str, categories: Any, now: int | None = None
    ) -> frozenset[str]:
        """Store the device's choice. Unknown categories are refused; the empty set means none."""
        chosen = frozenset(categories)
        if not chosen <= HINT_CATEGORIES:
            raise PushDenied("forbidden", "unknown notification category")
        at = self._clock() if now is None else now
        with self._lock:
            self._db.execute(
                "INSERT INTO push_prefs (account, device_id, categories, updated_ms)"
                " VALUES (?, ?, ?, ?)"
                " ON CONFLICT(account, device_id) DO UPDATE SET"
                " categories=excluded.categories, updated_ms=excluded.updated_ms",
                (account, device_id, json.dumps(sorted(chosen)), at),
            )
            self._commit()
        return chosen

    def category_allowed(self, account: str, device_id: str, category: str) -> bool:
        """Checked at enqueue and again at send time, so a change in between is honoured."""
        with self._lock:
            return category in self._prefs_locked(account, device_id)

    def revoke_device(self, account: str, device_id: str, now: int | None = None) -> bool:
        """Logout: DELETE the subscription row (token association gone)
        and drop its pending hints. Idempotent-by-result: False when no
        such subscription."""
        del now  # revocation is immediate; no timestamp is recorded
        with self._lock:
            self._db.execute(
                "DELETE FROM push_prefs WHERE account=? AND device_id=?", (account, device_id)
            )
            row = self._db.execute(
                "SELECT device_id FROM push_devices WHERE account=? AND device_id=?",
                (account, device_id),
            ).fetchone()
            if row is None:
                self._commit()
                return False
            self._db.execute(
                "DELETE FROM push_devices WHERE account=? AND device_id=?",
                (account, device_id),
            )
            self._db.execute(
                "UPDATE push_outbox SET status='dropped', last_error='revoked'"
                " WHERE account=? AND device_id=? AND status='pending'",
                (account, device_id),
            )
            self._commit()
            return True

    def revoke_device_by_ref(self, account: str, device_ref: str, now: int | None = None) -> bool:
        """Logout-with-ref: revoke the push record identified by its opaque
        device_ref, scoped to the session account (row delete + pending
        drop). Unknown or foreign refs are a silent no-op (False) -- never
        an error, never cross-account, never disclosing. Killed-app
        residual: a killed app cannot call logout, so its subscription
        lingers until token invalidation; its hints stay content-free and
        unresolvable after logout."""
        del now  # revocation is immediate; no timestamp is recorded
        with self._lock:
            rows = self._db.execute(
                "SELECT device_id FROM push_devices WHERE account=? AND device_ref=?",
                (account, device_ref),
            ).fetchall()
            if not rows:
                return False
            for row in rows:
                device_id = str(row["device_id"])
                self._db.execute(
                    "DELETE FROM push_prefs WHERE account=? AND device_id=?", (account, device_id)
                )
                self._db.execute(
                    "DELETE FROM push_devices WHERE account=? AND device_id=?",
                    (account, device_id),
                )
                self._db.execute(
                    "UPDATE push_outbox SET status='dropped', last_error='revoked'"
                    " WHERE account=? AND device_id=? AND status='pending'",
                    (account, device_id),
                )
            self._commit()
            return True

    def replace_device(
        self,
        account: str,
        old_device_id: str,
        new_device_id: str,
        token: str,
        platform: str,
        now: int | None = None,
    ) -> DeviceRecord:
        """Reinstall identity rule: a new install id is a NEW record; the
        old install row is tombstoned with `replaced_by` (token
        cleared -- routing fields only) and can never revive. Unknown
        old ids are tolerated (already gone)."""
        self._check_device_fields(new_device_id, token, platform)
        at = self._clock() if now is None else now
        with self._lock:
            old = self._db.execute(
                "SELECT * FROM push_devices WHERE account=? AND device_id=?",
                (account, old_device_id),
            ).fetchone()
            if old is not None and old_device_id != new_device_id:
                self._db.execute(
                    "UPDATE push_devices SET status='tombstoned', token='',"
                    " replaced_by=?, updated_ms=? WHERE account=? AND device_id=?",
                    (new_device_id, at, account, old_device_id),
                )
                self._db.execute(
                    "UPDATE push_outbox SET status='dropped', last_error='replaced'"
                    " WHERE account=? AND device_id=? AND status='pending'",
                    (account, old_device_id),
                )
            self._commit()
        return self.register_device(account, new_device_id, token, platform, at)

    def mark_token_invalid(self, account: str, device_id: str, now: int | None = None) -> bool:
        """Provider permanent failure: tombstone WITHOUT `replaced_by`
        (token cleared); a later register/rotate revives. Pending hints
        for the device are dropped -- the token is dead."""
        at = self._clock() if now is None else now
        with self._lock:
            row = self._db.execute(
                "SELECT status FROM push_devices WHERE account=? AND device_id=?",
                (account, device_id),
            ).fetchone()
            if row is None or row["status"] != "active":
                return False
            self._db.execute(
                "UPDATE push_devices SET status='tombstoned', token='', updated_ms=?"
                " WHERE account=? AND device_id=?",
                (at, account, device_id),
            )
            self._db.execute(
                "UPDATE push_outbox SET status='dropped', last_error='token_invalid'"
                " WHERE account=? AND device_id=? AND status='pending'",
                (account, device_id),
            )
            self._commit()
            return True

    def active_devices(self, account: str) -> list[DeviceRecord]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM push_devices WHERE account=? AND status='active' ORDER BY device_id",
                (account,),
            ).fetchall()
            return [self._device_from(self._row(row)) for row in rows]

    def subscription_count(self, account: str) -> int:
        """All subscription rows incl. tombstones (rotation/reinstall audit)."""
        with self._lock:
            row = self._db.execute(
                "SELECT COUNT(*) AS n FROM push_devices WHERE account=?", (account,)
            ).fetchone()
            return int(row["n"])

    def token_for(self, account: str, device_id: str) -> str | None:
        """Live provider token for an ACTIVE device, else None."""
        with self._lock:
            row = self._db.execute(
                "SELECT token, status FROM push_devices WHERE account=? AND device_id=?",
                (account, device_id),
            ).fetchone()
            if row is None or row["status"] != "active" or not row["token"]:
                return None
            return str(row["token"])

    def device_active(self, account: str, device_id: str) -> bool:
        return self.token_for(account, device_id) is not None

    # -- opaque refs ----------------------------------------------------

    _REF_PREFIX = {"host": "hr_", "session": "sr_", "event": "er_"}

    def ref_for(self, kind: str, value: str, now: int | None = None) -> str:
        """Get-or-mint the opaque ref for a (kind, value) pair. Payloads
        carry refs; real ids never leave the server except over the
        authenticated resolve path."""
        if kind not in self._REF_PREFIX or not value:
            raise PushDenied("forbidden", "bad ref subject")
        at = self._clock() if now is None else now
        with self._lock:
            row = self._db.execute(
                "SELECT ref FROM push_refs WHERE kind=? AND value=?", (kind, value)
            ).fetchone()
            if row is not None:
                return str(row["ref"])
            for _ in range(8):
                ref = f"{self._REF_PREFIX[kind]}{secrets.token_urlsafe(16)}"
                try:
                    self._db.execute(
                        "INSERT INTO push_refs (ref, kind, value, created_ms) VALUES (?,?,?,?)",
                        (ref, kind, value, at),
                    )
                    self._commit()
                    return ref
                except sqlite3.IntegrityError:
                    continue
            raise PushDenied("rate_limited", "ref mint collision")

    def resolve_ref(self, ref: str) -> tuple[str, str] | None:
        with self._lock:
            row = self._db.execute(
                "SELECT kind, value FROM push_refs WHERE ref=?", (ref,)
            ).fetchone()
            if row is None:
                return None
            return (str(row["kind"]), str(row["value"]))

    # -- outbox ---------------------------------------------------------

    def notify(
        self,
        *,
        account: str,
        account_hosts: frozenset[str] | set[str],
        host_id: str,
        session_id: str,
        event_id: str,
        category: str,
        now: int | None = None,
        ttl_ms: int = DEFAULT_PUSH_TTL_MS,
        device_ids: frozenset[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Server-side fanout for one verified host event. Eligibility:
        host in the account's authorized set, non-empty session/event,
        known category. Returns the NEWLY enqueued rows (dedupe skips
        are silent -- same event is never enqueued twice per device)."""
        if category not in HINT_CATEGORIES:
            raise PushDenied("forbidden", "unknown notification category")
        if not host_id or len(host_id) > MAX_HOST_ID_CHARS or host_id not in account_hosts:
            raise PushDenied("not_found", "host not authorized for account")
        if not session_id or len(session_id) > MAX_SESSION_ID_CHARS:
            raise PushDenied("forbidden", "bad session")
        if not event_id or len(event_id) > MAX_EVENT_ID_CHARS:
            raise PushDenied("forbidden", "bad event")
        if ttl_ms <= 0 or ttl_ms > 30 * 24 * 60 * 60 * 1000:
            raise PushDenied("forbidden", "bad ttl")
        at = self._clock() if now is None else now
        host_ref = self.ref_for("host", host_id, at)
        session_ref = self.ref_for("session", session_id, at)
        event_ref = self.ref_for("event", event_id, at)
        fresh: list[dict[str, Any]] = []
        with self._lock:
            devices = self._db.execute(
                "SELECT device_id, device_ref FROM push_devices"
                " WHERE account=? AND status='active' ORDER BY device_id",
                (account,),
            ).fetchall()
            for device in devices:
                device_id = str(device["device_id"])
                if device_ids is not None and device_id not in device_ids:
                    continue
                if category not in self._prefs_locked(account, device_id):
                    continue  # this device chose not to receive this category
                seen = self._db.execute(
                    "SELECT notification_id FROM push_outbox"
                    " WHERE account=? AND device_id=? AND event_id=?",
                    (account, device_id, event_id),
                ).fetchone()
                if seen is not None:
                    continue
                notification_id = f"n_{secrets.token_urlsafe(16)}"
                link_token = f"{LINK_TOKEN_PREFIX}{secrets.token_urlsafe(32)}"
                expires = at + ttl_ms
                try:
                    self._db.execute(
                        "INSERT INTO push_outbox (notification_id, account, device_id,"
                        " device_ref, host_ref, session_ref, event_id, event_ref,"
                        " category, created_ms, expires_ms, status, next_attempt_ms,"
                        " link_token) VALUES (?,?,?,?,?,?,?,?,?,?,?,'pending',?,?)",
                        (
                            notification_id,
                            account,
                            device_id,
                            str(device["device_ref"]),
                            host_ref,
                            session_ref,
                            event_id,
                            event_ref,
                            category,
                            at,
                            expires,
                            at,
                            link_token,
                        ),
                    )
                except sqlite3.IntegrityError:
                    # Cross-process double-enqueue: another writer won the
                    # UNIQUE(account, device, event) race after our dedupe
                    # check. Dedupe semantics: silent skip, never an error.
                    continue
                self._db.execute(
                    "INSERT INTO push_links (token, account, device_id,"
                    " notification_id, host_ref, session_ref, event_ref, category,"
                    " purpose, created_ms, expires_ms)"
                    " VALUES (?,?,?,?,?,?,?,?,'open-event',?,?)",
                    (
                        link_token,
                        account,
                        device_id,
                        notification_id,
                        host_ref,
                        session_ref,
                        event_ref,
                        category,
                        at,
                        expires,
                    ),
                )
                row = self._db.execute(
                    "SELECT * FROM push_outbox WHERE notification_id=?",
                    (notification_id,),
                ).fetchone()
                fresh.append(self._row(row))
            self._commit()
        return fresh

    def get_notification(self, notification_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM push_outbox WHERE notification_id=?", (notification_id,)
            ).fetchone()
            if row is None:
                return None
            return self._row(row)

    def pending_count(self, account: str | None = None) -> int:
        with self._lock:
            if account is None:
                row = self._db.execute(
                    "SELECT COUNT(*) AS n FROM push_outbox WHERE status='pending'"
                ).fetchone()
            else:
                row = self._db.execute(
                    "SELECT COUNT(*) AS n FROM push_outbox WHERE status='pending' AND account=?",
                    (account,),
                ).fetchone()
            return int(row["n"])

    def due(self, now: int | None = None, limit: int = 100) -> list[dict[str, Any]]:
        """Pending rows ready to send, per-device FIFO (device, created).
        Expired rows are excluded (the pump sweeps them to dropped)."""
        at = self._clock() if now is None else now
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM push_outbox WHERE status='pending'"
                " AND next_attempt_ms <= ? AND expires_ms > ?"
                " ORDER BY device_id, created_ms, rowid LIMIT ?",
                (at, at, max(limit, 1)),
            ).fetchall()
            return [self._row(row) for row in rows]

    def mark_sent(self, notification_id: str, now: int | None = None) -> bool:
        at = self._clock() if now is None else now
        with self._lock:
            row = self._db.execute(
                "SELECT status, expires_ms FROM push_outbox WHERE notification_id=?",
                (notification_id,),
            ).fetchone()
            if row is None or row["status"] != "pending":
                return False
            if int(row["expires_ms"]) <= at:
                self._db.execute(
                    "UPDATE push_outbox SET status='dropped', last_error='expired'"
                    " WHERE notification_id=?",
                    (notification_id,),
                )
                self._commit()
                return False
            self._db.execute(
                "UPDATE push_outbox SET status='sent', last_error='' WHERE notification_id=?",
                (notification_id,),
            )
            self._commit()
            return True

    def mark_failed(self, notification_id: str, error: str, now: int | None = None) -> str:
        """Record a provider failure. Returns the new status: still
        'pending' (retry scheduled inside TTL) or 'dropped' (TTL cannot
        fit another attempt); 'unknown' when no such notification exists
        (never fabricated as dropped)."""
        at = self._clock() if now is None else now
        with self._lock:
            row = self._db.execute(
                "SELECT status, attempts, expires_ms FROM push_outbox WHERE notification_id=?",
                (notification_id,),
            ).fetchone()
            if row is None:
                return "unknown"
            if row["status"] != "pending":
                return str(row["status"])
            expires = int(row["expires_ms"])
            attempts = int(row["attempts"]) + 1
            if expires <= at:
                status = "dropped"
                self._db.execute(
                    "UPDATE push_outbox SET status='dropped', attempts=?,"
                    " last_error='expired' WHERE notification_id=?",
                    (attempts, notification_id),
                )
                self._commit()
                return status
            wait = backoff_ms(attempts)
            if at + wait >= expires:
                self._db.execute(
                    "UPDATE push_outbox SET status='dropped', attempts=?,"
                    " last_error=? WHERE notification_id=?",
                    (attempts, _error_text(error or "ttl"), notification_id),
                )
                self._commit()
                return "dropped"
            self._db.execute(
                "UPDATE push_outbox SET attempts=?, next_attempt_ms=?, last_error=?"
                " WHERE notification_id=?",
                (attempts, at + wait, _error_text(error), notification_id),
            )
            self._commit()
            return "pending"

    def drop_notification(self, notification_id: str, error: str, now: int | None = None) -> bool:
        at = self._clock() if now is None else now
        del at  # drops are timeless; expiry/purge use created/expires_ms
        with self._lock:
            row = self._db.execute(
                "SELECT status FROM push_outbox WHERE notification_id=?",
                (notification_id,),
            ).fetchone()
            if row is None or row["status"] != "pending":
                return False
            self._db.execute(
                "UPDATE push_outbox SET status='dropped', last_error=? WHERE notification_id=?",
                (_error_text(error), notification_id),
            )
            self._commit()
            return True

    def sweep(self, now: int | None = None) -> dict[str, int]:
        """Drop pending rows past TTL (never sent after expiry)."""
        at = self._clock() if now is None else now
        with self._lock:
            cursor = self._db.execute(
                "UPDATE push_outbox SET status='dropped', last_error='expired'"
                " WHERE status='pending' AND expires_ms <= ?",
                (at,),
            )
            self._commit()
            return {"dropped": cursor.rowcount}

    def purge(self, now: int | None = None) -> dict[str, int]:
        """Retention sweep: terminal outbox rows older than
        TERMINAL_KEEP_MS (by expiry), expired links older than
        LINK_KEEP_MS past expiry, device tombstones older than
        DEVICE_TOMBSTONE_MS, and refs no live row references."""
        at = self._clock() if now is None else now
        with self._lock:
            outbox = self._db.execute(
                "DELETE FROM push_outbox WHERE status != 'pending' AND expires_ms + ? < ?",
                (TERMINAL_KEEP_MS, at),
            )
            links = self._db.execute(
                "DELETE FROM push_links WHERE expires_ms + ? < ?", (LINK_KEEP_MS, at)
            )
            devices = self._db.execute(
                "DELETE FROM push_devices WHERE status='tombstoned' AND updated_ms + ? < ?",
                (DEVICE_TOMBSTONE_MS, at),
            )
            refs = self._db.execute(
                "DELETE FROM push_refs WHERE created_ms + ? < ?"
                " AND ref NOT IN (SELECT host_ref FROM push_outbox)"
                " AND ref NOT IN (SELECT session_ref FROM push_outbox)"
                " AND ref NOT IN (SELECT event_ref FROM push_outbox)"
                " AND ref NOT IN (SELECT host_ref FROM push_links)"
                " AND ref NOT IN (SELECT session_ref FROM push_links)"
                " AND ref NOT IN (SELECT event_ref FROM push_links)",
                (TERMINAL_KEEP_MS, at),
            )
            self._commit()
            return {
                "outbox": outbox.rowcount,
                "links": links.rowcount,
                "devices": devices.rowcount,
                "refs": refs.rowcount,
            }

    def hint_payload(self, notification_id: str) -> dict[str, str]:
        """Build the EXACT wire payload for a queued notification. Key
        set is exactly HINT_KEYS; values are opaque refs (never ids,
        text, paths, commands, secrets, or decisions)."""
        row = self.get_notification(notification_id)
        if row is None:
            raise PushDenied("not_found", "no such notification")
        payload = {
            "notification_id": str(row["notification_id"]),
            "device_ref": str(row["device_ref"]),
            "host_ref": str(row["host_ref"]),
            "session_ref": str(row["session_ref"]),
            "event_ref": str(row["event_ref"]),
            "category": str(row["category"]),
            "created_at": _utc_z(int(row["created_ms"])),
            "expires_at": _utc_z(int(row["expires_ms"])),
        }
        if set(payload) != set(HINT_KEYS):
            raise PushDenied("upstream_incompatible", "hint key drift")
        for key, value in payload.items():
            if not value or len(value) > MAX_VALUE_LEN:
                raise PushDenied("forbidden", f"bad hint value for {key}")
        if payload["category"] not in HINT_CATEGORIES:
            raise PushDenied("forbidden", "unknown notification category")
        return payload

    # -- deep-link resolution -------------------------------------------

    def _resolve_shape(
        self,
        *,
        notification_id: str,
        host_ref: str,
        session_ref: str,
        event_ref: str,
        category: str,
    ) -> dict[str, Any] | None:
        """Map refs to the ids the app needs for its authenticated
        follow-up fetch. None when retention already purged the refs."""
        host = self.resolve_ref(host_ref)
        session = self.resolve_ref(session_ref)
        event = self.resolve_ref(event_ref)
        if host is None or session is None or event is None:
            return None
        if host[0] != "host" or session[0] != "session" or event[0] != "event":
            return None
        return {
            "notification_id": notification_id,
            "event_ref": event_ref,
            "host_ref": host_ref,
            "session_ref": session_ref,
            "category": category,
            "host_id": host[1],
            "session_id": session[1],
            "route": {"host_id": host[1], "session_id": session[1]},
        }

    def resolve_link(
        self, token: str, account: str, now: int | None = None
    ) -> dict[str, Any] | None:
        """Resolve an opaque deep-link token. Fails closed (None) for
        forged/dead/expired/foreign-device-revoked/wrong-purpose links.
        Re-resolving is allowed: links are single-purpose, not
        single-use."""
        at = self._clock() if now is None else now
        with self._lock:
            row = self._db.execute("SELECT * FROM push_links WHERE token=?", (token,)).fetchone()
            if row is None:
                return None
            link = self._row(row)
        if str(link["account"]) != account:
            return None
        if int(link["expires_ms"]) <= at:
            return None
        if str(link["purpose"]) != LINK_PURPOSE_OPEN:
            return None
        if not self.device_active(account, str(link["device_id"])):
            return None
        return self._resolve_shape(
            notification_id=str(link["notification_id"]),
            host_ref=str(link["host_ref"]),
            session_ref=str(link["session_ref"]),
            event_ref=str(link["event_ref"]),
            category=str(link["category"]),
        )

    def resolve_hint(
        self, account: str, notification_id: str, now: int | None = None
    ) -> dict[str, Any] | None:
        """Resolve a tapped notification id (the tap carries only the
        hint). Status/expiry are IGNORED -- a late or duplicate hint
        still opens current state; only account + live device gate."""
        row = self.get_notification(notification_id)
        if row is None or str(row["account"]) != account:
            return None
        if not self.device_active(account, str(row["device_id"])):
            return None
        return self._resolve_shape(
            notification_id=str(row["notification_id"]),
            host_ref=str(row["host_ref"]),
            session_ref=str(row["session_ref"]),
            event_ref=str(row["event_ref"]),
            category=str(row["category"]),
        )


def pump_outbox(
    store: PushStore,
    provider: ProviderPort,
    now: int | None = None,
    limit: int = 100,
    *,
    stop: Callable[[], bool] = lambda: False,
    authorized: Callable[[dict[str, Any]], bool] = lambda row: True,
) -> dict[str, int]:
    """Send due hints: sweep expired first, then per-device FIFO.
    Provider exceptions count as retryable (recorded, never raised).
    PERMANENT tombstones the device and drops its pending hints."""
    at = store.now() if now is None else now
    report = {"sent": 0, "retried": 0, "dropped": 0}
    report["dropped"] += store.sweep(at)["dropped"]
    for row in store.due(at, limit):
        if stop():
            break
        notification_id = str(row["notification_id"])
        account = str(row["account"])
        device_id = str(row["device_id"])
        token = store.token_for(account, device_id)
        if token is None or not authorized(row):
            # Every due row observed with a dead token ends this pump
            # dropped: either just now, or by an invalid-token cascade
            # from an earlier row of the same device in this loop.
            store.drop_notification(notification_id, "revoked", at)
            report["dropped"] += 1
            continue
        try:
            payload = store.hint_payload(notification_id)
        except PushDenied as exc:
            if store.drop_notification(notification_id, exc.code, at):
                report["dropped"] += 1
            continue
        try:
            outcome = provider.send(token, payload)
        except Exception as exc:  # noqa: BLE001 - provider faults retry
            outcome = ProviderOutcome.RETRYABLE
            error = f"provider raised: {type(exc).__name__}"
        else:
            error = "provider retryable"
        if outcome is ProviderOutcome.SENT:
            if store.mark_sent(notification_id, at):
                report["sent"] += 1
            else:
                report["dropped"] += 1
        elif outcome is ProviderOutcome.PERMANENT:
            store.mark_token_invalid(account, device_id, at)
            report["dropped"] += 1
        else:
            status = store.mark_failed(notification_id, error, at)
            report["retried" if status == "pending" else "dropped"] += 1
    return report
