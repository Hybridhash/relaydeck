"""Authenticated, bounded immutable usage pages. No transcript identities cross this boundary."""

from __future__ import annotations

import asyncio
import functools
import hashlib
import json
import logging
import re
import secrets
from collections import OrderedDict
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from relaydeck.gateway.backend import BackendError
from relaydeck.gateway.cursors import CursorError, decode_cursor
from relaydeck.persistence_contracts import StorageUnavailable
from relaydeck.usage.contracts import MAX_QUERY_DAYS, MAX_QUERY_ROWS, Coverage, UsageQuery
from relaydeck.usage.hook_install import upgrade_outdated_hook
from relaydeck.usage.hook_setup import HOOK_FILENAME, HookSetupCheck, default_hook_path
from relaydeck.usage.limits_plane.contracts import RefreshDecision
from relaydeck.usage.readers import READERS
from relaydeck.usage.store import SqliteUsageStore

if TYPE_CHECKING:
    from relaydeck.companion.config import UsageConfig
    from relaydeck.gateway.app import GatewayState
    from relaydeck.gateway.identity import Session

LOG = logging.getLogger(__name__)

NO_STORE = {"Cache-Control": "no-store"}
CURSOR_TTL_MS = 300_000
MAX_REPORT_BYTES = 8 * 1024 * 1024
MAX_CACHE_BYTES = 32 * 1024 * 1024
MAX_REPORTS = 16
BUSY_WAIT_SECONDS = 10.0
RETRY_AFTER_SECONDS = 2
_CURSOR = re.compile(r"[A-Za-z0-9_-]{43}")


class _Busy(Exception):
    """The bounded wait for the worker slot expired."""


def failure(code: str, status: int) -> JSONResponse:
    return JSONResponse({"error": code}, status_code=status, headers=NO_STORE)


def parse_query(request: Request, host: str, timezone: str, now_ms: int) -> tuple[UsageQuery, str]:
    pairs = list(request.query_params.multi_items())
    values = dict(pairs)
    allowed = {"from", "to", "days", "space_id", "cursor", "bucket"}
    if len(values) != len(pairs) or set(values) - allowed:
        raise ValueError
    if "days" in values:
        days = values["days"]
        if {"from", "to"} & values.keys() or not re.fullmatch(r"[1-9][0-9]?", days):
            raise ValueError
        if not 1 <= int(days) <= MAX_QUERY_DAYS:
            raise ValueError
        last = datetime.fromtimestamp(now_ms / 1000, ZoneInfo(timezone)).date()
        first = last - timedelta(days=int(days) - 1)
        selector = "days:" + days
    else:
        first, last = date.fromisoformat(values["from"]), date.fromisoformat(values["to"])
        if first.isoformat() != values["from"] or last.isoformat() != values["to"]:
            raise ValueError
        selector = f"range:{first}:{last}"
    hourly = "bucket" in values
    if (
        not 0 <= (last - first).days < MAX_QUERY_DAYS
        or (
            "space_id" in values
            and (
                not 1 <= len(values["space_id"]) <= 256
                or any(ord(c) < 32 for c in values["space_id"])
            )
        )
        or ("cursor" in values and not _CURSOR.fullmatch(values["cursor"]))
        # Hourly buckets exist for exactly one host day, and only the value "hour" is accepted.
        or (hourly and (values["bucket"] != "hour" or first != last))
    ):
        raise ValueError
    if hourly:
        selector += ":hour"  # a binding of its own: an hourly cursor never serves a daily page
    return UsageQuery(
        host,
        first.isoformat(),
        last.isoformat(),
        values.get("space_id"),
        values.get("cursor"),
        hour=hourly,
    ), selector


def live_workspace_labels(state: GatewayState, host_id: str) -> dict[str, str]:
    """The connector's inventory + newer bounded metadata; no filesystem inference."""
    try:
        snapshot = state.backend.snapshot(host_id)
        labels = {
            row["id"]: row["label"]
            for row in snapshot.get("workspaces", [])[:4096]
            if isinstance(row, dict)
            and isinstance(row.get("id"), str)
            and isinstance(row.get("label"), str)
        }
        after = 0
        try:
            cursor = decode_cursor(state.cursor_secret, snapshot.get("cursor", ""))
            if cursor.get("host_id") == host_id:
                after = int(cursor.get("sequence", 0))
        except (CursorError, TypeError, ValueError):
            pass
        epoch = snapshot.get("host", {}).get("epoch")
        for event in state.backend.events_after(host_id, after)[-4096:]:
            if event.get("host_id") != host_id or event.get("host_epoch") != epoch:
                continue
            resource = event.get("resource_id")
            if not isinstance(resource, str):
                continue
            if event.get("type") == "workspace.upserted":
                payload = event.get("payload", {})
                if isinstance(payload, dict) and isinstance(payload.get("label"), str):
                    labels[resource] = payload["label"]
            elif event.get("type") == "resource.removed":
                labels.pop(resource, None)
        return labels
    except BackendError:
        return {}


@dataclass(frozen=True)
class _Snapshot:
    binding: tuple[str, ...]
    expires: int
    pages: tuple[bytes, ...]
    cursors: tuple[str, ...]
    generation: int = -1
    price_digest: str = ""

    @property
    def size(self) -> int:
        return sum(map(len, self.pages))


class UsageService:
    def __init__(
        self,
        store: SqliteUsageStore | None,
        config: UsageConfig,
        host_id: str,
        state_dir: Path,
        clock: Callable[[], int],
        *,
        workspace_labels: Callable[[], dict[str, str]] | None = None,
        limits_status: Callable[[], Mapping[str, Mapping[str, Any]]] | None = None,
        limits_refresh_status: Callable[[], Mapping[str, Mapping[str, Any]]] | None = None,
        request_limits_refresh: Callable[[str], RefreshDecision] | None = None,
        hooks_dir: Path | None = None,
    ) -> None:
        self.store, self.config, self.host_id, self.clock = store, config, host_id, clock
        self.workspace_labels = workspace_labels or (lambda: {})
        self._limits_status = limits_status
        self._limits_refresh_status = limits_refresh_status
        self.request_limits_refresh = request_limits_refresh
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="usage-query")
        # One worker computes at a time. Identical requests share one computation (`_inflight`);
        # different ones wait their turn for up to `wait_s` seconds, then get `usage_busy`.
        self._slot = asyncio.Lock()
        self._inflight: dict[tuple[Any, ...], asyncio.Task[_Snapshot]] = {}
        self.wait_s = BUSY_WAIT_SECONDS
        self._closed = False
        self._reports: OrderedDict[str, _Snapshot] = OrderedDict()
        self._cursors: dict[str, tuple[str, int]] = {}
        self._reuse: OrderedDict[tuple[str, ...], _Snapshot] = OrderedDict()
        self._hooks_dir = hooks_dir
        script = (hooks_dir or default_hook_path().parent) / HOOK_FILENAME
        self._sources = {(s.kind, s.format): s for s in config.sources}
        self._checks = {
            (s.kind, s.format): HookSetupCheck(s.limits_settings, s.limits_cache, script)
            for s in config.sources
        }

    def _refresh_hook_script(self, source: Any) -> None:
        # A previous shipped hook is replaced once the check calls it outdated. An upgrade
        # that cannot proceed safely is left for later; startup never fails over it.
        settings, cache = source.limits_settings, source.limits_cache
        if settings is None or cache is None:
            return
        try:
            outcome = upgrade_outdated_hook(settings, cache, hooks_dir=self._hooks_dir)
        except Exception:
            LOG.warning("usage_hook_upgrade_failed source=%s", f"{source.kind}/{source.format}")
            return
        if outcome != "not_upgraded":
            # The script changed in both cases, so the cached diagnosis is stale.
            self._checks[(source.kind, source.format)].invalidate()
        if outcome == "upgraded":
            LOG.info("usage_hook_upgraded source=%s", f"{source.kind}/{source.format}")
        elif outcome == "uncertain":
            LOG.warning("usage_hook_upgrade_uncertain source=%s", f"{source.kind}/{source.format}")

    def check_setup(self) -> None:
        for check in self._checks.values():
            check.check(self.clock())

    def run_startup_upgrades(self) -> None:
        # Startup only, called synchronously on the event-loop thread before the server
        # starts: each outdated source gets exactly one attempt. No awaits happen here,
        # so startup cancellation cannot land mid-transaction; shutdown only runs after.
        for source in self._sources.values():
            self._refresh_hook_script(source)

    def _fingerprint(self, now: int) -> str:
        """Digest of snapshot inputs that live outside the usage store
        (live labels, limits plane state, hook setup checks), so a cached
        report is reused only while every embedded field would be identical."""
        try:
            checks = [
                self._checks[source.kind, source.format].check(now)
                for source in self.config.sources
            ]
            payload = [
                self.workspace_labels(),
                self._limits_status() if self._limits_status is not None else {},
                self._limits_refresh_status() if self._limits_refresh_status is not None else {},
                checks,
            ]
            return hashlib.sha256(
                json.dumps(payload, sort_keys=True, default=str).encode()
            ).hexdigest()
        except Exception:
            return ""

    def _metadata(self, now: int) -> list[dict[str, Any]]:
        kinds: dict[str, dict[str, Any]] = {}
        plane = self._limits_status() if self._limits_status is not None else {}
        refresh = self._limits_refresh_status() if self._limits_refresh_status is not None else {}
        for source in self.config.sources:
            kind = kinds.setdefault(
                source.kind,
                {
                    "kind": source.kind,
                    "display_name": source.display_name,
                    "tokens_reported": False,
                    "credits_reported": False,
                    "limits_setup": None,
                    "sources": [],
                },
            )
            if source.kind in plane and "limits_state" not in kind:
                kind["limits_state"] = dict(plane[source.kind])
            if "limits_refresh" not in kind:
                # every kind reports refresh availability; kinds
                # without an on-demand block are explicitly not configured.
                kind["limits_refresh"] = (
                    dict(refresh[source.kind])
                    if source.kind in refresh
                    else {
                        "available": False,
                        "reason": "not_configured",
                        "in_progress": False,
                        "cooldown_until_ms": None,
                        "last_error": None,
                        "last_attempt": None,
                    }
                )
            kind["tokens_reported"] |= bool(
                getattr(READERS[source.format], "tokens_reported", False)
            )
            # kinds whose readers report credits (never tokens) instead.
            kind["credits_reported"] |= bool(
                getattr(READERS[source.format], "credits_reported", False)
            )
            check = self._checks[source.kind, source.format].check(now)
            if check is not None:
                kind["limits_setup"] = check  # v1 field: the last configured source's state
            # Every configured source of the kind, with its own format and setup state (v2).
            kind["sources"].append({"format": source.format, "limits_setup": check})
        return list(kinds.values())

    def _report(self, query: UsageQuery, now: int) -> dict[str, Any]:
        if self.store is None:
            raise RuntimeError
        report = self.store.report_snapshot(query, now, inferred=self.config.inferred_linking)
        body = asdict(report)
        body["coverage"] = list(body["coverage"])
        # a kind with a provider reading (source set) shows only provider
        # rows; its older log-derived (source "") rows are hidden. Kinds without
        # a provider reading show legacy rows exactly as before.
        plane_kinds = {entry["kind"] for entry in body["limits"] if entry.get("source")}
        if plane_kinds:
            body["limits"] = [
                entry
                for entry in body["limits"]
                if entry.get("source") or entry["kind"] not in plane_kinds
            ]
        # Each space's last day with usage comes from the store, read before any space filter: a
        # space filter never erases another space's recency. No row in the period means null.
        last_day = {space: day for space, day in body.pop("workspace_days")}
        if query.space_id is not None:
            body["rows"] = tuple(row for row in body["rows"] if row["scope"] == "workspace")
        # Every space the host knows about (its inventory labels), plus any space that has rows.
        # Superseded generations show the label stored with their episodes; the current
        # generation prefers the live inventory label.
        labels = self.workspace_labels()
        stored = dict(body.pop("workspace_labels"))
        body["workspaces"] = [
            {
                "workspace_id": space,
                "label": labels.get(space, stored.get(space, space)),
                "last_used_day": last_day.get(space),
            }
            for space in sorted({*labels, *last_day})
        ]
        body["snapshot_labels"] = [
            {"date": day, "label": label} for day, label in self.store.prices.snapshot_labels
        ]
        body["kinds"] = self._metadata(now)
        present = {row["kind"] for row in body["coverage"]}
        body["coverage"].extend(
            {
                "kind": kind["kind"],
                "coverage": Coverage.UNAVAILABLE,
                "observed_ms": None,
                "unmapped_sessions": 0,
                "linked_sessions": 0,
                "inferred_sessions": 0,
            }
            for kind in body["kinds"]
            if kind["kind"] not in present
        )
        return body

    def _snapshot(
        self,
        query: UsageQuery,
        now: int,
        binding: tuple[str, ...],
        generation: int = -1,
        price_digest: str = "",
    ) -> _Snapshot:
        body = self._report(query, now)
        rows = body["rows"]
        cursors = tuple(
            secrets.token_urlsafe(32) for _ in range((max(1, len(rows)) - 1) // MAX_QUERY_ROWS)
        )
        pages: list[bytes] = []
        size = 0
        for index, start in enumerate(range(0, max(1, len(rows)), MAX_QUERY_ROWS)):
            page = {
                **body,
                "rows": rows[start : start + MAX_QUERY_ROWS],
                "next_cursor": cursors[index] if index < len(cursors) else None,
            }
            value = json.dumps(page, separators=(",", ":"), allow_nan=False).encode()
            size += len(value)
            if size > MAX_REPORT_BYTES:
                raise RuntimeError
            pages.append(value)
        return _Snapshot(
            binding, now + CURSOR_TTL_MS, tuple(pages), cursors, generation, price_digest
        )

    def _drop(self, key: str) -> None:
        snap = self._reports.pop(key)
        for cursor in snap.cursors:
            self._cursors.pop(cursor, None)

    def _prune(self, now: int) -> None:
        for key, snap in tuple(self._reports.items()):
            if now >= snap.expires:
                self._drop(key)
        for reuse_binding, reuse_snap in tuple(self._reuse.items()):
            if now >= reuse_snap.expires:
                del self._reuse[reuse_binding]

    async def page(
        self, query: UsageQuery, account: str, device: str, *, selector: str | None = None
    ) -> Response:
        now = self.clock()
        self._prune(now)
        binding = (
            account,
            device,
            query.host_id,
            selector or f"range:{query.from_day}:{query.to_day}",
            query.space_id or "",
            self.config.timezone,
        )
        if self._closed or self.store is None:
            return failure("usage_unavailable", 503)
        if query.cursor is not None:
            pointer = self._cursors.get(query.cursor)
            if pointer is None:
                return failure("invalid_cursor", 400)
            key, offset = pointer
            snap = self._reports[key]
            if snap.binding != binding:
                return failure("invalid_cursor", 400)
            return Response(snap.pages[offset], media_type="application/json", headers=NO_STORE)
        if self.store is not None:
            gen_fn = getattr(self.store, "data_generation", None)
            digest_fn = getattr(self.store, "price_digest", None)
            if callable(gen_fn) and callable(digest_fn):
                try:
                    generation, digest = gen_fn(), digest_fn()
                except Exception:
                    generation, digest = -1, ""
                digest = f"{digest}:{self._fingerprint(now)}" if generation >= 0 else ""
                if generation >= 0:
                    cached = self._reuse.get(binding)
                    if (
                        cached is not None
                        and cached.generation == generation
                        and cached.price_digest == digest
                        and now < cached.expires
                    ):
                        return Response(
                            cached.pages[0],
                            media_type="application/json",
                            headers=NO_STORE,
                        )
        flight = (binding, query.from_day, query.to_day)
        task = self._inflight.get(flight)
        if task is None:
            task = asyncio.get_running_loop().create_task(self._produce(query, now, binding))
            self._inflight[flight] = task
            task.add_done_callback(functools.partial(self._finished, flight))
        try:
            # Waiters share the computation; one disconnecting cancels only its own wait.
            snap = await asyncio.shield(task)
        except _Busy:
            return JSONResponse(
                {"error": "usage_busy"},
                status_code=503,
                headers={**NO_STORE, "Retry-After": str(RETRY_AFTER_SECONDS)},
            )
        except asyncio.CancelledError:
            if task.cancelled():
                return failure("usage_unavailable", 503)
            raise
        except Exception:
            return failure("usage_unavailable", 503)
        return Response(snap.pages[0], media_type="application/json", headers=NO_STORE)

    @property
    def _busy(self) -> bool:
        """True while a computation holds the worker slot."""
        return self._slot.locked()

    def _finished(self, key: tuple[Any, ...], done: asyncio.Task[_Snapshot]) -> None:
        if self._inflight.get(key) is done:
            del self._inflight[key]
        if not done.cancelled():
            _ = done.exception()  # retrieved: a failure is reported to the waiters

    async def _produce(self, query: UsageQuery, now: int, binding: tuple[str, ...]) -> _Snapshot:
        """One computation for every identical request. Waits its turn for the worker slot
        (bounded), runs the report off the loop and caches the pages."""
        try:
            await asyncio.wait_for(self._slot.acquire(), self.wait_s)
        except TimeoutError:
            raise _Busy from None
        loop = asyncio.get_running_loop()
        gen_fn = getattr(self.store, "data_generation", None)
        digest_fn = getattr(self.store, "price_digest", None)
        generation = gen_fn() if callable(gen_fn) else -1
        digest = digest_fn() if callable(digest_fn) else ""
        if generation >= 0:
            digest = f"{digest}:{self._fingerprint(now)}"
        future = loop.run_in_executor(
            self._executor, self._snapshot, query, now, binding, generation, digest
        )

        def release(done: asyncio.Future[_Snapshot]) -> None:
            # The slot frees only when the worker really finished, even if every waiter left.
            self._slot.release()
            if not done.cancelled():
                _ = done.exception()

        future.add_done_callback(release)
        snap = await asyncio.shield(future)
        if self._closed or self.clock() >= snap.expires:
            raise RuntimeError
        while self._reports and (
            len(self._reports) >= MAX_REPORTS
            or sum(s.size for s in self._reports.values()) + snap.size > MAX_CACHE_BYTES
        ):
            self._drop(next(iter(self._reports)))
        if snap.cursors:
            key = secrets.token_urlsafe(24)
            self._reports[key] = snap
            self._cursors.update({cursor: (key, i + 1) for i, cursor in enumerate(snap.cursors)})
        self._reuse[binding] = snap
        self._reuse.move_to_end(binding)
        while len(self._reuse) > MAX_REPORTS:
            self._reuse.popitem(last=False)
        return snap

    async def close(self) -> None:
        self._closed = True
        for task in tuple(self._inflight.values()):
            task.cancel()
        await asyncio.to_thread(self._executor.shutdown, wait=True, cancel_futures=True)
        self._reports.clear()
        self._cursors.clear()


def install(
    app: FastAPI,
    state: GatewayState,
    current_session: Callable[[Request], Session | None],
    host_guard: Callable[[Session, str], JSONResponse | None],
) -> None:
    def authorized(request: Request, host: str) -> tuple[Session | None, JSONResponse | None]:
        try:
            session = current_session(request)
        except StorageUnavailable:
            return None, failure("unauthenticated", 503)
        if session is None:
            return None, failure("unauthenticated", 401)
        guard = host_guard(session, host)
        if guard is not None:
            return None, failure(
                "forbidden" if guard.status_code == 403 else "not_found", guard.status_code
            )
        record = state.hosts.record_for_host(host)
        if (
            record is None
            or record.account != session.account
            or not state.hosts.allowed(session.account, host)
        ):
            return None, failure("not_found", 404)
        return session, None

    @app.post("/v1/hosts/{host_id}/usage/limits/refresh", status_code=202)
    async def refresh_limits(request: Request, host_id: str) -> Response:
        """Start (or join) one on-demand pane refresh for a kind.

        Session + host auth plus the pairing Origin rule (native requests have
        no Origin; browser origins must match exactly -- see app.py). The
        attempt runs on the scheduler worker; a client disconnect never
        cancels it. Joins return the in-flight attempt's id.
        """
        origins = request.headers.getlist("origin")
        if origins and origins != [state.public_origin]:
            return failure("forbidden", 403)
        session, denied = authorized(request, host_id)
        if denied is not None:
            return denied
        assert session is not None
        service = state.usage
        if service is None or service.host_id != host_id:
            return failure("usage_disabled", 404)
        try:
            raw = await request.body()
            if len(raw) > 1024:
                raise ValueError
            body = json.loads(raw)
            if set(body) != {"kind"} or not isinstance(body["kind"], str):
                raise ValueError
            kind = body["kind"]
            if not 0 < len(kind) <= 256 or any(ord(c) < 32 for c in kind):
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            return failure("invalid_body", 400)
        if service.request_limits_refresh is None:
            return failure("not_configured", 404)
        decision = service.request_limits_refresh(kind)
        if decision.state == "not_configured":
            return failure("not_configured", 404)
        if decision.state == "cooldown":
            retry = decision.retry_after_s or 1
            return JSONResponse(
                {"state": "cooldown", "retry_after_s": retry},
                status_code=429,
                headers={**NO_STORE, "Retry-After": str(retry)},
            )
        return JSONResponse(
            {"state": decision.state, "attempt_id": decision.attempt_id},
            status_code=202,
            headers=NO_STORE,
        )

    @app.get("/v1/hosts/{host_id}/usage")
    async def get_usage(request: Request, host_id: str) -> Response:
        session, denied = authorized(request, host_id)
        if denied is not None:
            return denied
        assert session is not None
        service = state.usage
        if service is None or service.host_id != host_id:
            return failure("usage_disabled", 404)
        try:
            query, selector = parse_query(request, host_id, service.config.timezone, state.now_ms())
        except (ValueError, KeyError, TypeError):
            return failure("invalid_query", 400)
        response = await service.page(query, session.account, session.device_id, selector=selector)
        # Revocation or a host ownership change during the off-loop query is authoritative.
        current, denied = authorized(request, host_id)
        if denied is not None:
            return denied
        if current is None or (current.account, current.device_id) != (
            session.account,
            session.device_id,
        ):
            return failure("unauthenticated", 401)
        return response
