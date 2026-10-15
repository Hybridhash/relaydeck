"""Configured argv and key path reach the provider; end to end."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

from relaydeck.companion.config import (
    CompanionConfig,
    UsageConfig,
    UsageLimitsConfig,
    UsageSourceConfig,
)
from relaydeck.companion.runtime import CompanionRuntime
from relaydeck.gateway.usage import UsageService
from relaydeck.usage.contracts import (
    LimitSnapshot,
    LimitStatus,
    LimitWindow,
    UsageQuery,
)
from relaydeck.usage.limits_plane.contracts import FetchContext, ProviderResult, ProviderSpec
from relaydeck.usage.limits_plane.providers.jsonrpc_rate_limits_stdio import (
    FORMAT_ID,
    JsonRpcRateLimitsProvider,
)
from relaydeck.usage.limits_plane.scheduler import LimitsScheduler
from relaydeck.usage.store import SqliteUsageStore
from tests.support.usage.store import NOW

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "usage" / "limits_provider"
HOST = "h1"
KIND = "kind-a"
KEY = f"{HOST}/{KIND}/{FORMAT_ID}"
DAY = "2026-10-11"
RAW_ID = "synthetic-account-1"


def fake_argv(tmp_path: Path, transcript: str) -> tuple[str, ...]:
    return (
        sys.executable,
        str(FIXTURES / "fake_app_server.py"),
        str(FIXTURES / f"{transcript}.json"),
        str(tmp_path / f"{transcript}.requests.log"),
    )


def test_runtime_passes_command_and_key_path(tmp_path: Path) -> None:
    block = UsageLimitsConfig(KIND, FORMAT_ID, ("/bin/echo", "x"), 300, 15, True)
    config = CompanionConfig(
        "http://127.0.0.1/",
        tmp_path / "cookie.secret",
        tmp_path / "cursor.secret",
        tmp_path / "state",
        tmp_path / "connector.json",
        str(tmp_path / "herdr.sock"),
        host_id=HOST,
        usage=UsageConfig("UTC", (), (), 90, (block,)),
    )
    runtime = CompanionRuntime(config)
    runtime.gateway = SimpleNamespace(now_ms=lambda: NOW, usage=None)
    runtime._prepare_usage()
    try:
        scheduler = runtime.limits_scheduler
        assert scheduler is not None
        assert scheduler._specs[KEY].command == ("/bin/echo", "x")
        assert scheduler._key_path == tmp_path / "state" / "limits-account.key"
    finally:
        assert runtime.usage_store is not None
        runtime.usage_store.close()


def test_scheduler_binds_command_and_key_path_into_context(tmp_path: Path) -> None:
    seen: list[FetchContext] = []

    class Recorder:
        format_id = "fake-wiring-v1"

        def fetch(self, ctx):
            seen.append(ctx)
            return ProviderResult()

    key = "h1/kind-a/fake-wiring-v1"
    spec = ProviderSpec(key, KIND, "fake-wiring-v1", 60_000, 1.0, command=("/bin/echo",))
    key_path = tmp_path / "limits-account.key"
    store = SqliteUsageStore(tmp_path / "usage.db", "UTC", clock=lambda: NOW)
    scheduler = LimitsScheduler(
        store, {key: spec}, {key: Recorder()}, clock=lambda: NOW, key_path=key_path
    )
    try:
        assert scheduler.run_once(NOW) == 1
        assert seen[0].command == ("/bin/echo",)
        assert seen[0].key_path == key_path
    finally:
        store.close()


def test_end_to_end_provider_scheduler_store_gateway(tmp_path: Path) -> None:
    key_path = tmp_path / "limits-account.key"
    spec = ProviderSpec(KEY, KIND, FORMAT_ID, 300_000, 10.0, command=fake_argv(tmp_path, "happy"))
    store = SqliteUsageStore(tmp_path / "usage.db", "UTC", clock=lambda: NOW)
    scheduler = LimitsScheduler(
        store,
        {KEY: spec},
        {KEY: JsonRpcRateLimitsProvider()},
        clock=lambda: NOW,
        key_path=key_path,
    )
    try:
        assert scheduler.run_once(NOW) == 1
        (snapshot,) = store.query(UsageQuery(HOST, DAY, DAY), NOW).limits
        assert snapshot.source == FORMAT_ID
        assert snapshot.account_ref is not None and len(snapshot.account_ref) == 16
        assert snapshot.account_ref != RAW_ID
        # A legacy row for the same kind is stored but hidden by the gateway.
        store.apply_limits(
            KIND,
            "h1/kind-a/legacy-v1",
            (
                LimitSnapshot(
                    KIND,
                    "q-old",
                    NOW,
                    (LimitWindow("primary", 1.0, 300, None, LimitStatus.FRESH),),
                ),
            ),
            NOW,
        )
        assert len(store.query(UsageQuery(HOST, DAY, DAY), NOW).limits) == 2
        config = UsageConfig(
            "UTC",
            (UsageSourceConfig(KIND, "jsonl-message-usage-v1", Path("/tmp"), "A"),),
            (),
            90,
            (),
        )
        service = UsageService(store, config, HOST, tmp_path, lambda: NOW)
        page = service._report(UsageQuery(HOST, DAY, DAY), NOW)
        assert len(page["limits"]) == 1
        assert page["limits"][0]["source"] == FORMAT_ID
        assert RAW_ID not in json.dumps(page, default=str)
        assert scheduler.status()[KIND]["state"] == "ok"
    finally:
        store.close()


def test_end_to_end_without_key_has_no_account_ref(tmp_path: Path) -> None:
    spec = ProviderSpec(KEY, KIND, FORMAT_ID, 300_000, 10.0, command=fake_argv(tmp_path, "happy"))
    store = SqliteUsageStore(tmp_path / "usage.db", "UTC", clock=lambda: NOW)
    scheduler = LimitsScheduler(
        store, {KEY: spec}, {KEY: JsonRpcRateLimitsProvider()}, clock=lambda: NOW
    )
    try:
        assert scheduler.run_once(NOW) == 1
        (snapshot,) = store.query(UsageQuery(HOST, DAY, DAY), NOW).limits
        assert snapshot.account_ref is None
    finally:
        store.close()
