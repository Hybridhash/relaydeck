"""Provider readings hide a kind's legacy rows."""

from __future__ import annotations

from pathlib import Path

from relaydeck.companion.config import UsageConfig, UsageSourceConfig
from relaydeck.gateway.usage import UsageService
from relaydeck.usage.contracts import (
    LimitSnapshot,
    LimitStatus,
    LimitWindow,
    UsageQuery,
)
from relaydeck.usage.store import SqliteUsageStore
from tests.support.usage.store import NOW

DAY = "2026-10-11"
FORMAT = "jsonrpc-rate-limits-stdio-v1"


def config() -> UsageConfig:
    return UsageConfig(
        "UTC",
        (
            UsageSourceConfig("kind-a", "jsonl-message-usage-v1", Path("/tmp"), "A"),
            UsageSourceConfig("kind-b", "jsonl-message-usage-v1", Path("/tmp"), "B"),
        ),
        (),
        90,
        (),
    )


def service(store: SqliteUsageStore, tmp_path: Path) -> UsageService:
    return UsageService(store, config(), "h1", tmp_path, lambda: NOW)


def legacy(kind: str, quota: str) -> LimitSnapshot:
    return LimitSnapshot(
        kind, quota, NOW, (LimitWindow("primary", 10.0, 300, None, LimitStatus.FRESH),)
    )


def plane(kind: str, quota: str) -> LimitSnapshot:
    return LimitSnapshot(
        kind,
        quota,
        NOW,
        (LimitWindow("primary", 20.0, 300, None, LimitStatus.FRESH),),
        LimitStatus.FRESH,
        FORMAT,
        "digest-1",
        None,
    )


def limits_of(store: SqliteUsageStore, tmp_path: Path) -> list:
    return service(store, tmp_path)._report(UsageQuery("h1", DAY, DAY), NOW)["limits"]


def test_provider_reading_hides_legacy_rows_for_that_kind(tmp_path: Path) -> None:
    with SqliteUsageStore(tmp_path / "usage.db", "UTC", clock=lambda: NOW) as store:
        store.apply_limits("kind-a", "h1/kind-a/legacy-v1", (legacy("kind-a", "q-old"),), NOW)
        store.apply_limits("kind-a", f"h1/kind-a/{FORMAT}", (plane("kind-a", "q-new"),), NOW)
        shown = limits_of(store, tmp_path)
        assert len(shown) == 1
        assert shown[0]["source"] == FORMAT
        assert shown[0]["quota_key"] == "q-new"


def test_kinds_without_provider_keep_legacy_rows(tmp_path: Path) -> None:
    with SqliteUsageStore(tmp_path / "usage.db", "UTC", clock=lambda: NOW) as store:
        store.apply_limits("kind-a", f"h1/kind-a/{FORMAT}", (plane("kind-a", "q-new"),), NOW)
        store.apply_limits("kind-b", "h1/kind-b/legacy-v1", (legacy("kind-b", "q-old"),), NOW)
        shown = limits_of(store, tmp_path)
        by_kind = {entry["kind"]: entry for entry in shown}
        assert set(by_kind) == {"kind-a", "kind-b"}
        assert by_kind["kind-a"]["source"] == FORMAT
        assert by_kind["kind-b"]["source"] == ""
        # The store itself still holds both rows; only the gateway filters.
        assert len(store.query(UsageQuery("h1", DAY, DAY), NOW).limits) == 2


def test_legacy_only_kind_shows_as_before(tmp_path: Path) -> None:
    with SqliteUsageStore(tmp_path / "usage.db", "UTC", clock=lambda: NOW) as store:
        store.apply_limits("kind-a", "h1/kind-a/legacy-v1", (legacy("kind-a", "q-old"),), NOW)
        shown = limits_of(store, tmp_path)
        assert len(shown) == 1
        assert shown[0]["source"] == ""
