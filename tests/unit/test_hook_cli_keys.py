"""the hook CLI and the companion config read ONE set of [usage] keys.

A key added to the config model (limits, inferred_linking) must never be rejected by the hook CLI
while the companion accepts it. The sets live in companion/config.py and both readers use them.
Temporary home only; the real companion.toml is never read.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from relaydeck.companion import config as companion_config
from relaydeck.companion.config import (
    USAGE_KEYS,
    USAGE_LIMITS_KEYS,
    USAGE_SOURCE_KEYS,
    load_config,
)
from relaydeck.usage import hook_cli
from relaydeck.usage.contracts import RETENTION_DAYS
from relaydeck.usage.hook_files import HookFileError

# The hook reads only status-line limits sources: a row without limits_format is not a hook source.
SOURCE_BLOCK = """[[usage.sources]]
kind = "kind-a"
format = "jsonl-message-usage-v1"
root = "{logs}"
display_name = "A"
limits_format = "json-statusline-limits-v1"
limits_cache = "{cache}"
"""
LIMITS_BLOCK = """[[usage.limits]]
kind = "kind-a"
format = "fake-limits-stdio-v1"
command = ["/bin/echo", "x"]
"""


def _write(home: Path, usage_lines: str, tables: str = "") -> Path:
    logs = home / "logs"
    logs.mkdir(exist_ok=True)
    state = home / "state"
    state.mkdir(mode=0o700, exist_ok=True)
    config = home / "companion.toml"
    config.write_text(
        "\n".join(
            [
                'advertised_origin = "https://mac.example-tailnet.ts.net"',
                'host_id = "home-mac"',
                f'state_dir = "{state}"',
                f'cookie_secret_file = "{home / "cookie.secret"}"',
                f'cursor_secret_file = "{home / "cursor.secret"}"',
                f'connector_credentials = "{home / "connector.json"}"',
                f'herdr_socket = "{home / "herdr.sock"}"',
                'agent_kinds = ["kind-a"]',
                "[usage]",
                usage_lines,
                SOURCE_BLOCK.format(logs=logs, cache=state / "statusline-limits.json"),
                LIMITS_BLOCK,
                tables,
                "",
            ]
        ),
        encoding="utf-8",
    )
    config.chmod(0o600)
    return config


@pytest.fixture
def home(tmp_path: Path) -> Path:
    directory = tmp_path / "home"
    directory.mkdir(mode=0o700)  # explicit: the hook reader refuses group-writable directories
    return directory


def test_the_owner_shape_loads_for_the_hook_cli_and_the_companion(home: Path) -> None:
    config = _write(
        home,
        'timezone = "Etc/UTC"\ninferred_linking = false',
        "[notifications]\nweb_push = true\nlock_screen_detail = false\n",
    )
    rows = hook_cli.load_hook_sources(config)
    assert [row.kind for row in rows] == ["kind-a"]
    loaded = load_config(config)
    assert loaded.usage is not None
    assert loaded.usage.inferred_linking is False
    assert len(loaded.usage.limits) == 1
    assert loaded.notifications.web_push is True


def test_every_key_in_the_shared_set_loads_for_both_readers(home: Path) -> None:
    config = _write(
        home,
        "\n".join(
            [
                f"retention_days = {RETENTION_DAYS}",
                'timezone = "Etc/UTC"',
                "price_tables = []",
                "inferred_linking = true",
            ]
        ),
    )
    assert set(USAGE_KEYS) == {
        "retention_days",
        "timezone",
        "price_tables",
        "sources",
        "limits",
        "inferred_linking",
    }
    assert [row.kind for row in hook_cli.load_hook_sources(config)] == ["kind-a"]
    assert load_config(config).usage is not None


def test_a_truly_unknown_usage_key_is_still_rejected_by_the_hook_cli(home: Path) -> None:
    config = _write(home, 'timezone = "Etc/UTC"\nbogus_key = true')
    with pytest.raises(HookFileError):
        hook_cli.load_hook_sources(config)
    assert load_config(config).usage is None  # the companion fails closed on the same key


def test_an_unknown_key_on_a_source_row_is_still_rejected_by_the_hook_cli(home: Path) -> None:
    config = _write(home, 'timezone = "Etc/UTC"')
    text = config.read_text(encoding="utf-8").replace(
        'display_name = "A"', 'display_name = "A"\nbogus_source_key = 1'
    )
    config.write_text(text, encoding="utf-8")
    with pytest.raises(HookFileError):
        hook_cli.load_hook_sources(config)


# Scalar keys with a valid value. sources and limits are tables already in every config built here.
SCALAR_VALUES = {
    "retention_days": f"retention_days = {RETENTION_DAYS}",
    "timezone": 'timezone = "Etc/UTC"',
    "price_tables": "price_tables = []",
    "inferred_linking": "inferred_linking = true",
}


@pytest.mark.parametrize("key", sorted(set(USAGE_KEYS) - {"sources", "limits"}))
def test_the_two_readers_agree_on_each_shared_usage_key(home: Path, key: str) -> None:
    config = _write(home, SCALAR_VALUES[key])
    assert [row.kind for row in hook_cli.load_hook_sources(config)] == ["kind-a"]
    assert load_config(config).usage is not None


def test_a_key_outside_the_shared_set_is_rejected_by_both_readers(home: Path) -> None:
    config = _write(home, 'timezone = "Etc/UTC"\nnot_a_key = "x"')
    with pytest.raises(HookFileError):
        hook_cli.load_hook_sources(config)
    assert load_config(config).usage is None


def test_the_hook_cli_reads_the_shared_sets_and_hardcodes_no_key_names() -> None:
    source = inspect.getsource(hook_cli.load_hook_sources)
    assert "USAGE_KEYS" in source and "USAGE_SOURCE_KEYS" in source
    for key in ("inferred_linking", "retention_days", "price_tables", '"limits"'):
        assert key not in source, key
    assert "USAGE_KEYS" in inspect.getsource(companion_config.usage_from)
    assert "USAGE_SOURCE_KEYS" in inspect.getsource(companion_config.usage_from)


def test_the_shared_sets_have_exactly_the_documented_members() -> None:
    assert USAGE_KEYS == frozenset(
        {"retention_days", "timezone", "price_tables", "sources", "limits", "inferred_linking"}
    )
    assert USAGE_SOURCE_KEYS == frozenset(
        {
            "kind",
            "format",
            "root",
            "display_name",
            "limits_format",
            "limits_settings",
            "limits_cache",
        }
    )
    assert USAGE_LIMITS_KEYS == frozenset(
        {
            "kind",
            "format",
            "command",
            "min_interval_seconds",
            "timeout_seconds",
            "enabled",
            # manual-only blocks (a deliberate config extension).
            "on_demand",
            "auto_interval_seconds",
        }
    )
