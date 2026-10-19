"""Scoped command identities only. Persistence precedes dispatch; recovery only reads."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
import weakref
from collections.abc import Awaitable, Callable
from typing import Any

from relaydeck.client.adapters.ports import CredentialStore
from relaydeck.client.features.sync.transport import TransportError
from relaydeck.contracts.schemas import SchemaError, validate

MAX_RECORDS = 128
MAX_BYTES = 131072
KINDS = {"agent.prompt", "agent.rename", "agent.start", "shell.create", "tab.create"}
# tab.create remains readable in legacy storage; gateway submissions use shell.create.
OUTCOMES = {"pending", "accepted", "dispatched", "unknown", "succeeded", "failed", "rejected"}
FINAL = {"succeeded", "failed", "rejected"}
IDENTITIES = {"workspace_id", "tab_id", "pane_id", "agent_id"}
_LOCKS: weakref.WeakKeyDictionary[CredentialStore, dict[str, asyncio.Lock]] = (
    weakref.WeakKeyDictionary()
)


class RecoveryError(TransportError):
    def __init__(self, detail: str) -> None:
        super().__init__("command_recovery", detail, False)


class MemoryCommandStore(CredentialStore):
    """Server-session/headless fallback; native always supplies secure storage."""

    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    async def available(self) -> bool:
        return True

    async def get(self, key: str) -> str | None:
        return self.data.get(key)

    async def set(self, key: str, value: str) -> None:
        self.data[key] = value

    async def delete(self, key: str) -> None:
        self.data.pop(key, None)

    async def clear_account(self) -> None:
        self.data.clear()


def _identity(value: Any) -> bool:
    return isinstance(value, str) and 0 < len(value) <= 128 and not any(ord(c) < 32 for c in value)


class PendingCommands:
    def __init__(
        self,
        store: CredentialStore,
        origin: str,
        account: str,
        device: str,
        *,
        current: Callable[[], bool] = lambda: True,
        version: Callable[[], Any] = lambda: None,
        clock: Callable[[], int] = lambda: int(time.time() * 1000),
    ) -> None:
        self.store = store
        self.scope = {"origin": origin.rstrip("/"), "account": account, "device": device}
        self.key = (
            "pending_" + hashlib.sha256(json.dumps(self.scope, sort_keys=True).encode()).hexdigest()
        )
        self.current = current
        self.version = version
        self.clock = clock
        self.records: dict[tuple[str, str], dict[str, Any]] = {}
        self.error = ""
        self.loaded = False
        self.closed = False
        self.lock = _LOCKS.setdefault(store, {}).setdefault(self.key, asyncio.Lock())
        self.listeners: set[Callable[[], Any]] = set()

    def subscribe(self, listener: Callable[[], Any]) -> Callable[[], None]:
        self.listeners.add(listener)
        return lambda: self.listeners.discard(listener)

    def changed(self) -> None:
        for listener in tuple(self.listeners):
            listener()

    def check(self) -> None:
        if self.closed or not self.current():
            raise RecoveryError("Command identity no longer current.")

    async def load(self) -> None:
        async with self.lock:
            await self._load()

    async def _load(self) -> None:
        self.check()
        try:
            raw = await self.store.get(self.key)
            self.check()
            rows = []
            if raw is not None:
                if not isinstance(raw, str) or len(raw.encode()) > MAX_BYTES:
                    raise ValueError

                def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
                    result: dict[str, Any] = {}
                    for key, value in pairs:
                        if key in result:
                            raise ValueError
                        result[key] = value
                    return result

                doc = json.loads(raw, object_pairs_hook=unique)
                if (
                    not isinstance(doc, dict)
                    or set(doc) != {"version", "scope", "records"}
                    or type(doc["version"]) is not int
                    or doc["version"] != 1
                    or doc["scope"] != self.scope
                ):
                    raise ValueError
                rows = doc["records"]
                if not isinstance(rows, list) or len(rows) > MAX_RECORDS:
                    raise ValueError
                for row in rows:
                    self._validate_row(row)
            self.records = {(r["host"], r["id"]): r for r in rows}
            if len(self.records) != len(rows):
                raise ValueError
            self.loaded = True
            # A proved prompt success has no created resource to review. Keep
            # creations and failed/uncertain sends until their existing review
            # or exact-resource settlement path resolves them.
            await self._save(
                {key: row for key, row in self.records.items() if not self._settled_prompt(row)}
            )
        except (ValueError, TypeError, KeyError, RecursionError):
            self.error = "Pending storage is invalid. Mutations disabled; reads remain available."
            raise RecoveryError(self.error) from None
        except RecoveryError:
            raise
        except Exception:
            self.error = "Pending storage unavailable. Check status before another mutation."
            raise RecoveryError(self.error) from None
        finally:
            self.changed()

    def _validate_row(self, row: Any) -> None:
        if not isinstance(row, dict) or set(row) != {
            "host",
            "id",
            "kind",
            "target",
            "created_ms",
            "outcome",
            "created",
        }:
            raise ValueError
        if (
            not _identity(row["host"])
            or not _identity(row["id"])
            or not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", row["id"])
            or row["kind"] not in KINDS
            or row["outcome"] not in OUTCOMES
        ):
            raise ValueError
        if type(row["created_ms"]) is not int or row["created_ms"] < 0:
            raise ValueError
        if (
            not isinstance(row["target"], dict)
            or not row["target"]
            or set(row["target"]) - {"agent_id", "workspace_id", "workspace_mode"}
            or any(not _identity(v) for v in row["target"].values())
        ):
            raise ValueError
        target = row["target"]
        if row["kind"] in ("agent.prompt", "agent.rename"):
            if set(target) != {"agent_id"}:
                raise ValueError
        elif set(target) != {"workspace_id"} and target != {"workspace_mode": "new"}:
            raise ValueError
        if (
            not isinstance(row["created"], dict)
            or set(row["created"]) - IDENTITIES
            or any(not _identity(v) for v in row["created"].values())
        ):
            raise ValueError

    async def _save(self, rows: dict[tuple[str, str], dict[str, Any]]) -> None:
        self.check()
        if rows == self.records and not self.error:
            return
        encoded = json.dumps(
            {"version": 1, "scope": self.scope, "records": list(rows.values())},
            separators=(",", ":"),
        )
        if len(rows) > MAX_RECORDS or len(encoded.encode()) > MAX_BYTES:
            raise RecoveryError(
                "Pending command capacity reached. Reconcile existing operations before sending."
            )
        try:
            await self.store.set(self.key, encoded)
        except Exception:
            self.error = "Pending storage unavailable. Check status before another mutation."
            self.changed()
            raise RecoveryError(self.error) from None
        self.records = rows
        self.check()
        self.error = ""
        self.changed()

    @staticmethod
    def _settled_prompt(row: dict[str, Any]) -> bool:
        return bool(
            row["kind"] in ("agent.prompt", "agent.rename") and row["outcome"] == "succeeded"
        )

    @staticmethod
    def unresolved(row: dict[str, Any]) -> bool:
        return row["outcome"] not in FINAL or bool(
            row["created"]
            and row["target"].get("workspace_mode") == "new"
            and row["outcome"] != "succeeded"
        )

    async def begin(self, command: dict[str, Any]) -> None:
        try:
            validate("relaydeck/gateway/v1/command-submit", command)
        except SchemaError:
            raise RecoveryError("Invalid command identity; nothing was sent.") from None
        async with self.lock:
            await self._load()
            key = (command["host_id"], command["command_id"])
            if key in self.records or any(
                r["host"] == key[0]
                and r["kind"] == command["kind"]
                and r["kind"] != "agent.rename"
                and r["target"] == command["target"]
                and self.unresolved(r)
                for r in self.records.values()
            ):
                raise RecoveryError(
                    "A matching operation is unresolved. Check its status; do not resend."
                )
            row = {
                "host": key[0],
                "id": key[1],
                "kind": command["kind"],
                "target": dict(command["target"]),
                "created_ms": self.clock(),
                "outcome": "pending",
                "created": {},
            }
            self._validate_row(row)
            await self._save({**self.records, key: row})

    async def observe(self, host: str, command_id: str, doc: dict[str, Any] | None) -> None:
        async with self.lock:
            await self._load()
            self.check()
            previous = self.records.get((host, command_id))
            if previous is None:
                return
            outcome, created = "unknown", dict(previous["created"])
            if doc is not None:
                try:
                    validate("relaydeck/gateway/v1/command-record", doc)
                    if (
                        doc["host_id"] != host
                        or doc["command_id"] != command_id
                        or doc["kind"] != previous["kind"]
                        or doc["target"] != previous["target"]
                    ):
                        raise ValueError
                    outcome = doc["status"] if doc["status"] in OUTCOMES else "unknown"
                    result = doc.get("result", {})
                    created.update(
                        {k: v for k, v in result.items() if k in IDENTITIES and _identity(v)}
                    )
                except (SchemaError, ValueError, KeyError, TypeError):
                    outcome = "unknown"
            # Lost/expired reads never weaken an already proven terminal outcome.
            if previous["outcome"] in FINAL:
                outcome = previous["outcome"]
            row = {**previous, "outcome": outcome, "created": created}
            rows = {**self.records, (host, command_id): row}
            if self._settled_prompt(row):
                del rows[(host, command_id)]
            await self._save(rows)

    async def reconcile(
        self,
        status: Callable[[str, str], Awaitable[dict[str, Any] | None]],
        granted: frozenset[str],
    ) -> None:
        await self.load()
        for host, command_id in tuple(self.records):
            self.check()
            if host not in granted:
                continue
            try:
                doc = await status(host, command_id)
                await self.observe(host, command_id, doc)
            except asyncio.CancelledError:
                raise
            except Exception:
                await self.observe(host, command_id, None)

    async def settle_creation(self, host: str, command_id: str, resource_id: str) -> None:
        """Forget a successful creation after the UI verifies its exact resource.

        Status reconciliation alone retains records for explicit review. Failed,
        partial and uncertain outcomes cannot be settled through this path.
        """
        async with self.lock:
            await self._load()
            key = (host, command_id)
            row = self.records.get(key)
            if row is None:
                return
            identity = {"agent.start": "agent_id", "shell.create": "tab_id"}.get(row["kind"])
            if (
                row["outcome"] != "succeeded"
                or identity is None
                or row["created"].get(identity) != resource_id
            ):
                raise RecoveryError("Creation is not verified; check its status before dismissing.")
            await self._save({k: r for k, r in self.records.items() if k != key})

    async def acknowledge(self, host: str, command_id: str) -> None:
        async with self.lock:
            await self._load()
            row = self.records.get((host, command_id))
            if row is None or row["outcome"] not in FINAL:
                raise RecoveryError("An uncertain operation cannot be dismissed as completed.")
            await self._save(
                {key: r for key, r in self.records.items() if key != (host, command_id)}
            )

    async def close(self) -> None:
        self.closed = True
        async with self.lock:
            self.listeners.clear()
