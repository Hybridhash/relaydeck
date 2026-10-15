import logging
from pathlib import Path

import pytest

from relaydeck.companion.config import load_config, usage_from
from tests.support.companion.asgi_companion import config_file


def raw_usage(tmp_path: Path) -> dict:
    root = tmp_path.resolve() / "logs"
    root.mkdir(mode=0o700, exist_ok=True)
    return {
        "timezone": "America/New_York",
        "sources": [{"kind": "kind-a", "format": "jsonl-message-usage-v1", "root": str(root)}],
    }


def test_config_defaults_and_disabled_are_independent(tmp_path, monkeypatch, caplog):
    raw = raw_usage(tmp_path)
    raw.pop("timezone")
    monkeypatch.setenv("TZ", "Asia/Riyadh")
    config = usage_from(raw, ("kind-a",))
    assert config and config.retention_days == 90 and config.timezone == "Asia/Riyadh"
    assert config.sources[0].display_name == "kind-a" and config.price_tables == ()
    assert usage_from(None, ()) is None
    path = config_file(tmp_path, agent_kinds=["kind-a"])
    path.write_text(path.read_text() + '\n[usage]\ntimezone="secret-bearer-grant/path"\n')
    with caplog.at_level(logging.WARNING):
        companion = load_config(path)
    assert companion.usage is None and companion.port == 8125
    assert "reason=invalid_configuration" in caplog.text
    assert "secret-bearer-grant" not in caplog.text


@pytest.mark.parametrize(
    "change",
    [
        "unknown",
        "retention",
        "bool",
        "zone",
        "kind",
        "format",
        "relative",
        "missing",
        "symlink",
        "duplicate",
        "root_file",
        "tables_relative",
        "display_path",
        "settings_without_format",
        "cache_missing",
        "cache_shared",
        "unsafe_cache",
    ],
)
def test_invalid_usage_fails_closed_without_profile_failure(tmp_path, change):
    raw = raw_usage(tmp_path)
    source = raw["sources"][0]
    if change == "unknown":
        raw["surprise"] = "secret"
    elif change == "retention":
        raw["retention_days"] = 91
    elif change == "bool":
        raw["retention_days"] = True
    elif change == "zone":
        raw["timezone"] = "unknown/zone"
    elif change == "kind":
        # a usage source needs no agent_kinds entry; only a malformed name fails.
        source["kind"] = "Not Allowed!"
    elif change == "format":
        source["format"] = "not-registered"
    elif change == "relative":
        source["root"] = "relative"
    elif change == "missing":
        source["root"] += "/missing"
    elif change == "root_file":
        p = tmp_path.resolve() / "file"
        p.write_text("")
        source["root"] = str(p)
    elif change == "symlink":
        p = tmp_path.resolve() / "linked"
        p.symlink_to(source["root"])
        source["root"] = str(p)
    elif change == "duplicate":
        raw["sources"].append(dict(source))
    elif change == "tables_relative":
        raw["price_tables"] = ["relative"]
    elif change == "display_path":
        source["display_name"] = "/secret/path"
    elif change == "settings_without_format":
        source["limits_settings"] = str(tmp_path / "x")
    else:
        source["limits_format"] = "json-statusline-limits-v1"
        if change != "cache_missing":
            parent = tmp_path.resolve() / "private"
            parent.mkdir(mode=0o700)
            source["limits_cache"] = str(parent / "cache.json")
        if change == "unsafe_cache":
            parent.chmod(0o755)
        if change == "cache_shared":
            raw["sources"].append({**source, "kind": "kind-b"})
    assert usage_from(raw, ("kind-a", "kind-b")) is None


def test_limits_source_paths_bound_and_unmanaged(tmp_path):
    raw = raw_usage(tmp_path)
    source = raw["sources"][0]
    parent = tmp_path.resolve() / "private"
    parent.mkdir(mode=0o700)
    source.update(limits_format="json-statusline-limits-v1", limits_cache=str(parent / "cache"))
    config = usage_from(raw, ("kind-a",))
    assert config and config.sources[0].limits_cache == parent / "cache"
    assert config.sources[0].limits_settings is None
    source["limits_settings"] = str(parent / "settings.json")
    assert usage_from(raw, ("kind-a",)).sources[0].limits_settings == parent / "settings.json"


def test_positive_toml_profile_and_owner_only_root_validation(tmp_path, monkeypatch):
    raw = raw_usage(tmp_path)
    root = raw["sources"][0]["root"]
    path = config_file(tmp_path, agent_kinds=["kind-a"])
    path.write_text(
        path.read_text() + '\n[usage]\nretention_days=90\ntimezone="UTC"\n'
        '[[usage.sources]]\nkind="kind-a"\nformat="jsonl-message-usage-v1"\n'
        f'root="{root}"\n'
    )
    assert load_config(path).usage.sources[0].root == Path(root)
    import os

    uid = os.getuid()
    monkeypatch.setattr("relaydeck.companion.config.os.getuid", lambda: uid + 1)
    assert usage_from(raw, ("kind-a",)) is None


def test_hardlinked_and_duplicate_kind_quota_caches_are_rejected(tmp_path):
    raw = raw_usage(tmp_path)
    parent = tmp_path.resolve() / "private"
    parent.mkdir(mode=0o700)
    cache = parent / "cache"
    cache.write_text("{}")
    cache.chmod(0o600)
    linked = parent / "linked"
    linked.hardlink_to(cache)
    raw["sources"][0].update(limits_format="json-statusline-limits-v1", limits_cache=str(cache))
    assert usage_from(raw, ("kind-a",)) is None
    linked.unlink()
    raw["sources"].append(
        {
            **raw["sources"][0],
            "format": "jsonl-event-usage-v1",
            "limits_cache": str(parent / "second"),
        }
    )
    assert usage_from(raw, ("kind-a",)) is None
