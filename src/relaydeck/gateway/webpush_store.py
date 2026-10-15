"""Durable web subscriptions, device-bound route tokens and the web outbox (security rules 3-4).

The store lives in ``<state_dir>/webpush/webpush.db`` (0600, WAL, synchronous=FULL), separate from
the Android ``push.db`` so the FCM pipeline is unchanged.

Every subscription has a generation. Queued messages and route tokens carry the generation they were
created for, so a late 404/410 can only remove the generation it attempted, and revoking a device or
rotating the key removes its queued work at once. The last generation issued to a device is kept in
``wp_generations``, which no revocation or key rotation clears, so a generation number is never
reused: a delete followed by a new registration cannot be hit by a late failure of the old one.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from relaydeck.gateway import webpush
from relaydeck.gateway.push import ProviderOutcome, ProviderPort

MAX_ROUTE_CHARS = 64
ROUTE_PREFIX = "wp_"
RETRY_LIFETIME_MS = webpush.MAX_RETRY_MS
TTL_MS_CAP = webpush.MAX_TTL_SECONDS * 1000
BACKOFF_BASE_MS = 30_000
BACKOFF_CAP_MS = 15 * 60_000
ROUTE_LIFETIME_MS = 24 * 60 * 60 * 1000
CATEGORY_TEXT = {
    "attention": "An agent needs attention",
    "completion": "An agent finished",
}
CATEGORY_URGENCY = {"attention": "high", "completion": "normal"}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS wp_meta (k TEXT PRIMARY KEY, v TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS wp_subscriptions (
  account TEXT NOT NULL,
  device_id TEXT NOT NULL,
  generation INTEGER NOT NULL,
  endpoint TEXT NOT NULL,
  p256dh TEXT NOT NULL,
  auth TEXT NOT NULL,
  created_ms INTEGER NOT NULL,
  PRIMARY KEY (account, device_id)
);
CREATE TABLE IF NOT EXISTS wp_generations (
  account TEXT NOT NULL,
  device_id TEXT NOT NULL,
  last INTEGER NOT NULL,
  PRIMARY KEY (account, device_id)
);
CREATE TABLE IF NOT EXISTS wp_routes (
  token TEXT PRIMARY KEY,
  account TEXT NOT NULL,
  device_id TEXT NOT NULL,
  generation INTEGER NOT NULL,
  category TEXT NOT NULL,
  host_id TEXT NOT NULL DEFAULT '',
  session_id TEXT NOT NULL DEFAULT '',
  created_ms INTEGER NOT NULL,
  expires_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS wp_outbox (
  id TEXT PRIMARY KEY,
  account TEXT NOT NULL,
  device_id TEXT NOT NULL,
  generation INTEGER NOT NULL,
  event_key TEXT NOT NULL,
  route_token TEXT NOT NULL,
  category TEXT NOT NULL,
  label TEXT NOT NULL DEFAULT '',
  created_ms INTEGER NOT NULL,
  expires_ms INTEGER NOT NULL,
  status TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0,
  next_attempt_ms INTEGER NOT NULL DEFAULT 0,
  UNIQUE (account, device_id, event_key)
);
CREATE INDEX IF NOT EXISTS idx_wp_outbox_due ON wp_outbox(status, next_attempt_ms);
"""


@dataclass(frozen=True)
class Subscription:
    account: str
    device_id: str
    generation: int
    endpoint: str
    p256dh: str
    auth: str


def _now_ms() -> int:
    return int(time.time() * 1000)


def _event_key(event_id: str) -> str:
    return hashlib.sha256(event_id.encode("utf-8")).hexdigest()


def outbox_counts(state_dir: Path) -> tuple[int, int] | None:
    """(queued, retrying) web push messages, read without creating or changing anything. None when
    the store does not exist yet. A message that failed and is retrying has attempts > 0."""
    path = state_dir / "webpush" / "webpush.db"
    if not path.is_file():
        return None
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            row = connection.execute(
                "SELECT COUNT(*), COALESCE(SUM(attempts > 0), 0) FROM wp_outbox"
                " WHERE status='pending'"
            ).fetchone()
        finally:
            connection.close()
    except sqlite3.Error:
        return None
    return int(row[0]), int(row[1])


def topic_for(topic_key: bytes, device_id: str, route: str) -> str:
    """Domain-separated topic (security rule 4). The device and route stay out of clear text."""
    digest = hmac.new(topic_key, f"topic-v1:{device_id}:{route}".encode(), hashlib.sha256).digest()
    return webpush.b64u(digest)[:32]


class WebPushStore:
    def __init__(self, state_dir: Path, *, clock: Callable[[], int] = _now_ms) -> None:
        directory = webpush.webpush_dir(state_dir)
        self.path = directory / "webpush.db"
        self._clock = clock
        self._lock = threading.Lock()
        created = not self.path.exists()
        if created:
            fd = os.open(self.path, os.O_CREAT | os.O_WRONLY, 0o600)
            os.close(fd)
        self._db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.executescript(_SCHEMA)
        self._db.execute("INSERT OR IGNORE INTO wp_meta(k, v) VALUES ('sending', '1')")
        self._chmod_sidecars()

    def _chmod_sidecars(self) -> None:
        for suffix in ("", "-wal", "-shm"):
            path = Path(str(self.path) + suffix)
            if path.exists():
                os.chmod(path, 0o600)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def now(self) -> int:
        return self._clock()

    # -- switches -----------------------------------------------------------------------------

    def sending_enabled(self) -> bool:
        with self._lock:
            row = self._db.execute("SELECT v FROM wp_meta WHERE k='sending'").fetchone()
        return row is not None and row["v"] == "1"

    def set_sending(self, enabled: bool) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO wp_meta(k, v) VALUES ('sending', ?) "
                "ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                ("1" if enabled else "0",),
            )

    # -- subscriptions -----------------------------------------------------------------------------

    def put_subscription(
        self, account: str, device_id: str, endpoint: str, p256dh: str, auth: str, now: int
    ) -> int:
        """Validates the endpoint and keys, then replaces the subscription with a new generation."""
        webpush.check_endpoint(endpoint)
        if len(webpush.b64u_decode(p256dh)) != 65 or len(webpush.b64u_decode(auth)) != 16:
            raise ValueError("bad subscription keys")
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                row = self._db.execute(
                    "SELECT generation FROM wp_subscriptions WHERE account=? AND device_id=?",
                    (account, device_id),
                ).fetchone()
                issued = self._db.execute(
                    "SELECT last FROM wp_generations WHERE account=? AND device_id=?",
                    (account, device_id),
                ).fetchone()
                # Never reuse a number: the counter outlives the subscription row.
                previous = max(
                    int(row["generation"]) if row else 0, int(issued["last"]) if issued else 0
                )
                generation = previous + 1
                self._db.execute(
                    "INSERT INTO wp_generations(account, device_id, last) VALUES (?, ?, ?)"
                    " ON CONFLICT(account, device_id) DO UPDATE SET last=excluded.last",
                    (account, device_id, generation),
                )
                self._db.execute(
                    "INSERT INTO wp_subscriptions(account, device_id, generation, endpoint, p256dh,"
                    " auth, created_ms) VALUES (?, ?, ?, ?, ?, ?, ?)"
                    " ON CONFLICT(account, device_id) DO UPDATE SET generation=excluded.generation,"
                    " endpoint=excluded.endpoint, p256dh=excluded.p256dh, auth=excluded.auth,"
                    " created_ms=excluded.created_ms",
                    (account, device_id, generation, endpoint, p256dh, auth, now),
                )
                # Work queued for an older generation can no longer be delivered.
                self._db.execute(
                    "DELETE FROM wp_outbox WHERE account=? AND device_id=? AND generation<?",
                    (account, device_id, generation),
                )
                self._db.execute(
                    "DELETE FROM wp_routes WHERE account=? AND device_id=? AND generation<?",
                    (account, device_id, generation),
                )
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
        return generation

    def subscription(self, account: str, device_id: str) -> Subscription | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM wp_subscriptions WHERE account=? AND device_id=?",
                (account, device_id),
            ).fetchone()
        if row is None:
            return None
        return Subscription(
            account=row["account"],
            device_id=row["device_id"],
            generation=int(row["generation"]),
            endpoint=row["endpoint"],
            p256dh=row["p256dh"],
            auth=row["auth"],
        )

    def delete_subscription(self, account: str, device_id: str) -> bool:
        """Removes the subscription, its queued work and its route tokens (revocation)."""
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                removed = self._db.execute(
                    "DELETE FROM wp_subscriptions WHERE account=? AND device_id=?",
                    (account, device_id),
                ).rowcount
                self._db.execute(
                    "DELETE FROM wp_outbox WHERE account=? AND device_id=?", (account, device_id)
                )
                self._db.execute(
                    "DELETE FROM wp_routes WHERE account=? AND device_id=?", (account, device_id)
                )
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
        return removed > 0

    def delete_generation(self, account: str, device_id: str, generation: int) -> bool:
        """A delayed 404/410 removes only the generation that was attempted (security rule 3)."""
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                current = self._db.execute(
                    "SELECT generation FROM wp_subscriptions WHERE account=? AND device_id=?",
                    (account, device_id),
                ).fetchone()
                if current is None or int(current["generation"]) != generation:
                    self._db.execute("COMMIT")
                    return False
                self._db.execute(
                    "DELETE FROM wp_subscriptions WHERE account=? AND device_id=?",
                    (account, device_id),
                )
                self._db.execute(
                    "DELETE FROM wp_outbox WHERE account=? AND device_id=? AND generation=?",
                    (account, device_id, generation),
                )
                self._db.execute(
                    "DELETE FROM wp_routes WHERE account=? AND device_id=? AND generation=?",
                    (account, device_id, generation),
                )
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
        return True

    def delete_all(self) -> int:
        """Used by key rotation: every subscription and all queued work goes."""
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                removed = self._db.execute("DELETE FROM wp_subscriptions").rowcount
                self._db.execute("DELETE FROM wp_outbox")
                self._db.execute("DELETE FROM wp_routes")
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
        return removed

    # -- routes ----------------------------------------------------------------------------------

    def resolve_route(
        self, account: str, device_id: str, token: str, now: int
    ) -> dict[str, str] | None:
        """The category, host and session when the token belongs to this device, is current and
        unexpired. Only the owning device gets these; the tap still re-checks host grants."""
        if not token.startswith(ROUTE_PREFIX) or len(token) > MAX_ROUTE_CHARS:
            return None
        with self._lock:
            row = self._db.execute(
                "SELECT r.category, r.host_id, r.session_id, r.expires_ms,"
                " s.generation AS current, r.generation"
                " FROM wp_routes r JOIN wp_subscriptions s"
                " ON s.account=r.account AND s.device_id=r.device_id"
                " WHERE r.token=? AND r.account=? AND r.device_id=?",
                (token, account, device_id),
            ).fetchone()
        if row is None or int(row["expires_ms"]) <= now:
            return None
        if int(row["current"]) != int(row["generation"]):
            return None
        return {
            "category": str(row["category"]),
            "host_id": str(row["host_id"]),
            "session_id": str(row["session_id"]),
        }

    # -- outbox ----------------------------------------------------------------------------------

    def enqueue(
        self,
        account: str,
        device_id: str,
        event_id: str,
        category: str,
        *,
        now: int,
        ttl_ms: int = RETRY_LIFETIME_MS,
        label: str = "",
        host: str = "",
        session: str = "",
    ) -> str | None:
        """Queues one message for the device's current generation. Returns the route token, or None
        when the device has no subscription or this event is already queued (dedupe)."""
        if category not in CATEGORY_TEXT:
            raise ValueError("unknown category")
        if not event_id or len(event_id) > 256 or ttl_ms <= 0 or len(label) > 64:
            raise ValueError("bad notification")
        key = _event_key(event_id)
        route = ROUTE_PREFIX + secrets.token_urlsafe(24)
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                sub = self._db.execute(
                    "SELECT generation FROM wp_subscriptions WHERE account=? AND device_id=?",
                    (account, device_id),
                ).fetchone()
                if sub is None:
                    self._db.execute("COMMIT")
                    return None
                generation = int(sub["generation"])
                inserted = self._db.execute(
                    "INSERT OR IGNORE INTO wp_outbox(id, account, device_id, generation, event_key,"
                    " route_token, category, label, created_ms, expires_ms, status, attempts,"
                    " next_attempt_ms) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?)",
                    (
                        secrets.token_hex(16),
                        account,
                        device_id,
                        generation,
                        key,
                        route,
                        category,
                        label,
                        now,
                        now + min(ttl_ms, RETRY_LIFETIME_MS),
                        now,
                    ),
                ).rowcount
                if not inserted:
                    self._db.execute("COMMIT")
                    return None
                self._db.execute(
                    "INSERT INTO wp_routes(token, account, device_id, generation, category,"
                    " host_id, session_id, created_ms, expires_ms)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        route,
                        account,
                        device_id,
                        generation,
                        category,
                        host[:128],
                        session[:128],
                        now,
                        now + ROUTE_LIFETIME_MS,
                    ),
                )
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
        return route

    def due(self, now: int, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM wp_outbox WHERE status='pending' AND next_attempt_ms<=?"
                " ORDER BY created_ms, id LIMIT ?",
                (now, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def drop(self, outbox_id: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM wp_outbox WHERE id=?", (outbox_id,))

    def mark_sent(self, outbox_id: str) -> None:
        self.drop(outbox_id)

    def retry_or_drop(self, row: dict[str, Any], now: int) -> str:
        attempts = int(row["attempts"]) + 1
        if now >= int(row["expires_ms"]):
            self.drop(str(row["id"]))
            return "dropped"
        delay = min(BACKOFF_BASE_MS * (2 ** (attempts - 1)), BACKOFF_CAP_MS)
        with self._lock:
            self._db.execute(
                "UPDATE wp_outbox SET attempts=?, next_attempt_ms=? WHERE id=?",
                (attempts, now + delay, str(row["id"])),
            )
        return "retried"

    def purge(self, now: int) -> int:
        with self._lock:
            removed = self._db.execute("DELETE FROM wp_outbox WHERE expires_ms<=?", (now,)).rowcount
            self._db.execute("DELETE FROM wp_routes WHERE expires_ms<=?", (now,))
        return removed


def pump(
    store: WebPushStore,
    provider: ProviderPort,
    *,
    topic_key: bytes,
    now: int,
    lock_screen_detail: bool = False,
    authorized: Callable[[str, str], bool] = lambda account, device_id: True,
    allowed: Callable[[str, str, str], bool] = lambda account, device_id, category: True,
    limit: int = 20,
) -> dict[str, int]:
    """Sends due web messages one at a time. Each one is re-checked for generation, revocation and
    the device's category choice immediately before the provider is called. Never logs subscriptions
    or payloads."""
    report = {"sent": 0, "retried": 0, "dropped": 0}
    if not store.sending_enabled():
        return report
    for row in store.due(now, limit):
        account, device_id = str(row["account"]), str(row["device_id"])
        generation = int(row["generation"])
        sub = store.subscription(account, device_id)
        if sub is None or sub.generation != generation:
            store.drop(str(row["id"]))
            report["dropped"] += 1
            continue
        if not authorized(account, device_id):
            store.delete_subscription(account, device_id)
            report["dropped"] += 1
            continue
        category = str(row["category"])
        if not allowed(account, device_id, category):
            store.drop(str(row["id"]))  # the device turned this category off after it was queued
            report["dropped"] += 1
            continue
        route = str(row["route_token"])
        body = CATEGORY_TEXT[category]
        if lock_screen_detail and row["label"]:
            body = f"{body} · {row['label']}"
        token = json.dumps(
            {
                "endpoint": sub.endpoint,
                "p256dh": sub.p256dh,
                "auth": sub.auth,
                "ttl": max(1, min(int((int(row["expires_ms"]) - now) // 1000), 3600)),
                "urgency": CATEGORY_URGENCY[category],
                "topic": topic_for(topic_key, device_id, route),
            },
            separators=(",", ":"),
        )
        payload = {"t": "RelayDeck", "b": body, "r": route}
        try:
            outcome = provider.send(token, payload)
        except Exception:  # noqa: BLE001 - provider faults retry; nothing about them is logged
            outcome = ProviderOutcome.RETRYABLE
        if outcome is ProviderOutcome.SENT:
            store.mark_sent(str(row["id"]))
            report["sent"] += 1
        elif outcome is ProviderOutcome.PERMANENT:
            store.delete_generation(account, device_id, generation)
            report["dropped"] += 1
        else:
            report["retried" if store.retry_or_drop(row, now) == "retried" else "dropped"] += 1
    return report


def rotate(state_dir: Path, store: WebPushStore) -> dict[str, int]:
    """One step: stop sending, delete every subscription and queued message, replace the VAPID key.

    Clients must subscribe again. The topic key is left as it is so existing topics stay valid.
    Sending is switched back on last: with no subscriptions it sends nothing until clients
    resubscribe
    against the new key."""
    store.set_sending(False)
    removed = store.delete_all()
    webpush.rotate_vapid(state_dir)
    store.set_sending(True)
    return {"subscriptions_removed": removed}
