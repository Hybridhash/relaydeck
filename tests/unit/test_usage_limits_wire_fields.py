"""Additive limit fields and kinds[].limits_state."""

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


def config() -> UsageConfig:
    return UsageConfig(
        "UTC",
        (UsageSourceConfig("kind-a", "jsonl-message-usage-v1", Path("/tmp"), "A"),),
        (),
        90,
        (),
    )


def service(
    store: SqliteUsageStore,
    tmp_path: Path,
    status: dict[str, dict[str, object]] | None = None,
) -> UsageService:
    return UsageService(
        store,
        config(),
        "h1",
        tmp_path,
        lambda: NOW,
        limits_status=(lambda: status) if status is not None else None,
    )


def test_additive_limit_fields_and_v1_unchanged(tmp_path: Path) -> None:
    with SqliteUsageStore(tmp_path / "usage.db", "UTC", clock=lambda: NOW) as store:
        store.apply_limits(
            "kind-a",
            "h1/kind-a/fake-limits-stdio-v1",
            (
                LimitSnapshot(
                    "kind-a",
                    "quota-a",
                    NOW,
                    (LimitWindow("primary", 25.0, 300, NOW + 1000, LimitStatus.FRESH),),
                    LimitStatus.FRESH,
                    "fake",
                    "account-a",
                    None,
                ),
            ),
            NOW,
        )
        page = service(store, tmp_path)._report(UsageQuery("h1", DAY, DAY), NOW)
    (entry,) = page["limits"]
    assert entry["source"] == "fake"
    assert entry["account_ref"] == "account-a"
    assert entry["error"] is None
    assert entry["kind"] == "kind-a"
    assert entry["quota_key"] == "quota-a"
    assert entry["observed_ms"] == NOW
    assert entry["status"] == LimitStatus.FRESH
    (pane,) = entry["windows"]
    assert pane["window_key"] == "primary"
    assert pane["used_percent"] == 25.0
    assert pane["window_minutes"] == 300  # labels derive from this, never assumed
    assert pane["resets_at_ms"] == NOW + 1000


def test_limits_state_present_only_with_a_scheduler(tmp_path: Path) -> None:
    with SqliteUsageStore(tmp_path / "usage.db", "UTC", clock=lambda: NOW) as store:
        bare = service(store, tmp_path)._report(UsageQuery("h1", DAY, DAY), NOW)
        assert "limits_state" not in bare["kinds"][0]
        wired = service(
            store,
            tmp_path,
            {"kind-a": {"state": "error", "checked_ms": NOW, "error": "timeout"}},
        )._report(UsageQuery("h1", DAY, DAY), NOW)
        assert wired["kinds"][0]["limits_state"] == {
            "state": "error",
            "checked_ms": NOW,
            "error": "timeout",
        }
