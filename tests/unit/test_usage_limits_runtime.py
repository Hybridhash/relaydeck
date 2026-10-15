"""The scheduler runs only when limits resolve to a provider."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from relaydeck.companion.config import (
    CompanionConfig,
    UsageConfig,
    UsageLimitsConfig,
)
from relaydeck.companion.runtime import CompanionRuntime
from relaydeck.usage.limits_plane import PROVIDERS
from relaydeck.usage.limits_plane.contracts import ProviderResult
from tests.support.usage.store import NOW


class FakeProvider:
    format_id = "fake-limits-stdio-v1"

    def __init__(self) -> None:
        self.calls = 0

    def fetch(self, ctx):
        self.calls += 1
        return ProviderResult()


def runtime_with(tmp_path: Path, usage: UsageConfig) -> CompanionRuntime:
    config = CompanionConfig(
        "http://127.0.0.1/",
        tmp_path / "cookie.secret",
        tmp_path / "cursor.secret",
        tmp_path / "state",
        tmp_path / "connector.json",
        str(tmp_path / "herdr.sock"),
        usage=usage,
    )
    runtime = CompanionRuntime(config)
    runtime.gateway = SimpleNamespace(now_ms=lambda: NOW, usage=None)
    runtime._prepare_usage()
    return runtime


def test_no_scheduler_without_limits(tmp_path: Path) -> None:
    runtime = runtime_with(tmp_path, UsageConfig("UTC", (), (), 90, ()))
    try:
        assert runtime.usage_collector is not None
        assert runtime.limits_scheduler is None
        assert runtime.gateway.usage._limits_status is None
    finally:
        assert runtime.usage_store is not None
        runtime.usage_store.close()


def test_unknown_format_starts_no_scheduler(tmp_path: Path) -> None:
    block = UsageLimitsConfig("kind-a", "future-format-v9", ("/bin/echo",), 300, 15, True)
    runtime = runtime_with(tmp_path, UsageConfig("UTC", (), (), 90, (block,)))
    try:
        assert runtime.limits_scheduler is None
    finally:
        assert runtime.usage_store is not None
        runtime.usage_store.close()


def test_known_format_wires_scheduler_and_service(tmp_path: Path, monkeypatch) -> None:
    provider = FakeProvider()
    monkeypatch.setitem(PROVIDERS, provider.format_id, provider)
    block = UsageLimitsConfig("kind-a", provider.format_id, ("/bin/echo",), 300, 15, True)
    runtime = runtime_with(tmp_path, UsageConfig("UTC", (), (), 90, (block,)))
    try:
        assert runtime.limits_scheduler is not None
        assert runtime.gateway.usage._limits_status is not None
        assert runtime.gateway.usage._limits_status() == {}
    finally:
        assert runtime.usage_store is not None
        runtime.usage_store.close()
