import asyncio
import json
from dataclasses import replace

import pytest

from relaydeck.companion.config import UsageConfig, UsageSourceConfig, load_config
from relaydeck.companion.runtime import CompanionRuntime
from relaydeck.usage.collector import UsageCollector
from relaydeck.usage.contracts import UsageQuery, UsageSource
from relaydeck.usage.store import SqliteUsageStore
from tests.support.companion.asgi_companion import AsgiServer, config_file
from tests.support.usage.collector import FakeReader
from tests.support.usage.store import NOW


@pytest.fixture(autouse=True)
def private_picture_state(tmp_path, monkeypatch):
    from relaydeck.companion.pictures import PictureStore

    monkeypatch.setattr(
        "relaydeck.companion.pictures.PictureStore",
        lambda **kwargs: PictureStore(tmp_path.resolve() / "pictures", **kwargs),
    )


@pytest.mark.parametrize("configured", [False, True])
def test_one_collector_private_ledger_join_before_close(tmp_path, configured):
    async def run():
        config = load_config(config_file(tmp_path, web_ui=False))
        root = tmp_path.resolve() / "logs"
        root.mkdir(mode=0o700)
        if configured:
            config = replace(
                config,
                usage=UsageConfig(
                    "UTC", (UsageSourceConfig("kind-a", "jsonl-message-usage-v1", root, "Kind A"),)
                ),
            )
        runtime = CompanionRuntime(config, server_factory=AsgiServer)
        try:
            await runtime.start(asyncio.Event())
            if configured:
                collector = runtime.usage_collector
                thread = collector._thread
                assert thread and thread.is_alive()
                collector.start()
                assert collector._thread is thread
                assert runtime.gateway.usage is not None
                assert config.state_dir.stat().st_mode & 0o777 == 0o700
                assert (config.state_dir / "usage.db").stat().st_mode & 0o777 == 0o600
            else:
                assert runtime.usage_collector is None and runtime.usage_store is None
                assert runtime.gateway.usage is None
                assert not (config.state_dir / "usage.db").exists()
        finally:
            await runtime.close()
        if configured:
            assert not thread.is_alive()
            assert runtime.usage_store._closed
        assert runtime.server.closed

    asyncio.run(run())


def test_invalid_price_table_is_tokens_only_without_sensitive_logs(tmp_path, caplog):
    async def run():
        config = load_config(config_file(tmp_path, web_ui=False))
        root = tmp_path.resolve() / "logs"
        root.mkdir()
        path = tmp_path.resolve() / "prices.toml"
        path.write_text("secret-grant-message")
        config = replace(
            config,
            usage=UsageConfig(
                "UTC",
                (UsageSourceConfig("kind-a", "jsonl-message-usage-v1", root, "Kind A"),),
                (path,),
            ),
        )
        runtime = CompanionRuntime(config, server_factory=AsgiServer)
        try:
            await runtime.start(asyncio.Event())
            assert runtime.usage_store.prices.rates == ()
            assert runtime.usage_collector._thread.is_alive()
        finally:
            await runtime.close()
        assert "usage_prices_unavailable reason=invalid_price_table" in caplog.text
        assert "secret-grant-message" not in caplog.text and str(path) not in caplog.text

    asyncio.run(run())


def test_unavailable_usage_storage_does_not_prevent_companion_start(tmp_path, monkeypatch, caplog):
    def denied(*a, **k):
        raise ValueError("secret-session/path")

    monkeypatch.setattr("relaydeck.companion.runtime.SqliteUsageStore", denied)

    async def run():
        config = load_config(config_file(tmp_path, web_ui=False))
        config = replace(
            config,
            usage=UsageConfig(
                "UTC",
                (
                    UsageSourceConfig(
                        "kind-a", "jsonl-message-usage-v1", tmp_path.resolve(), "Kind A"
                    ),
                ),
            ),
        )
        runtime = CompanionRuntime(config, server_factory=AsgiServer)
        try:
            await runtime.start(asyncio.Event())
            assert runtime.server.started
            assert runtime.gateway.usage.store is None and runtime.usage_collector is None
        finally:
            await runtime.close()
        assert "usage_unavailable reason=storage_unavailable" in caplog.text
        assert "secret-session/path" not in caplog.text

    asyncio.run(run())


def test_limits_are_source_specific_never_global_default(tmp_path):
    root = tmp_path.resolve() / "logs"
    root.mkdir()
    private = tmp_path.resolve() / "private"
    private.mkdir(mode=0o700)
    sources = []
    for kind, percentage in (("kind-a", 12.5), ("kind-b", 75)):
        path = private / f"{kind}.json"
        path.write_text(
            json.dumps(
                {
                    "captured_ms": NOW,
                    "windows": {
                        "five_hour": {
                            "used_percentage": percentage,
                            "resets_at": (NOW + 1000) // 1000,
                        }
                    },
                }
            )
        )
        path.chmod(0o600)
        sources.append(
            UsageSource("home-mac", kind, "format-a", root, "json-statusline-limits-v1", path)
        )
    with SqliteUsageStore(tmp_path / "state/usage.db", "UTC", clock=lambda: NOW) as store:
        collector = UsageCollector(store, {"format-a": FakeReader()}, tuple(sources), lambda: ())
        collector.run_once(NOW)
        report = store.report_snapshot(UsageQuery("home-mac", "2026-10-11", "2026-10-11"), NOW)
        assert {s.kind: s.windows[0].used_percent for s in report.limits} == {
            "kind-a": 12.5,
            "kind-b": 75,
        }
