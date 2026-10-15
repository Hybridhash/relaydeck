"""[[usage.limits]] validation. Invalid blocks fail closed."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from relaydeck.companion.config import UsageConfig, usage_from

KINDS = ("kind-a",)
SOURCE = {
    "kind": "kind-a",
    "format": "jsonl-message-usage-v1",
    "root": "",
    "display_name": "A",
}
LIMIT = {
    "kind": "kind-a",
    "format": "fake-limits-stdio-v1",
    "command": ["/bin/echo", "x"],
}


def value(tmp_path: Path, *limits: dict[str, Any]) -> dict[str, Any]:
    source = dict(SOURCE)
    source["root"] = str(tmp_path)
    return {"sources": [source], "limits": list(limits)}


def parsed(tmp_path: Path, *limits: dict[str, Any]) -> UsageConfig | None:
    return usage_from(value(tmp_path, *limits), KINDS)


def limit(**over: Any) -> dict[str, Any]:
    row = dict(LIMIT)
    row.update(over)
    return row


def test_defaults_applied(tmp_path: Path) -> None:
    config = parsed(tmp_path, limit())
    assert config is not None
    (entry,) = config.limits
    assert entry.kind == "kind-a"
    assert entry.format == "fake-limits-stdio-v1"
    assert entry.command == ("/bin/echo", "x")
    assert entry.min_interval_seconds == 300
    assert entry.timeout_seconds == 15
    assert entry.enabled is True


def test_explicit_values_kept(tmp_path: Path) -> None:
    config = parsed(tmp_path, limit(min_interval_seconds=60, timeout_seconds=30, enabled=False))
    assert config is not None
    (entry,) = config.limits
    assert (entry.min_interval_seconds, entry.timeout_seconds, entry.enabled) == (60, 30, False)


def test_no_limits_key_means_empty(tmp_path: Path) -> None:
    source = dict(SOURCE)
    source["root"] = str(tmp_path)
    config = usage_from({"sources": [source]}, KINDS)
    assert config is not None
    assert config.limits == ()


def test_relative_executable_rejected(tmp_path: Path) -> None:
    assert parsed(tmp_path, limit(command=["bin/echo"])) is None


def test_shell_string_rejected(tmp_path: Path) -> None:
    assert parsed(tmp_path, limit(command="/bin/echo x")) is None


def test_unknown_key_rejected(tmp_path: Path) -> None:
    row = limit()
    row["origin"] = "https://example.invalid"
    assert parsed(tmp_path, row) is None


def test_bad_intervals_rejected(tmp_path: Path) -> None:
    assert parsed(tmp_path, limit(min_interval_seconds=59)) is None
    assert parsed(tmp_path, limit(min_interval_seconds="300")) is None
    assert parsed(tmp_path, limit(timeout_seconds=0)) is None
    assert parsed(tmp_path, limit(timeout_seconds=1.5)) is None
    assert parsed(tmp_path, limit(enabled="yes")) is None


def test_bad_identity_rejected(tmp_path: Path) -> None:
    assert parsed(tmp_path, limit(kind="kind-b")) is None
    assert parsed(tmp_path, limit(format="has space")) is None
    assert parsed(tmp_path, limit(format="")) is None
    assert parsed(tmp_path, limit(), limit()) is None  # duplicate (kind, format)


def test_bad_command_rejected(tmp_path: Path) -> None:
    row = limit()
    del row["command"]
    assert parsed(tmp_path, row) is None
    assert parsed(tmp_path, limit(command=[])) is None
    assert parsed(tmp_path, limit(command=["/does/not/exist"])) is None
    assert parsed(tmp_path, limit(command=["/bin/echo", ""])) is None
    assert parsed(tmp_path, limit(command=["/bin/echo\x00x"])) is None
    assert parsed(tmp_path, limit(command=[str(tmp_path)])) is None  # a directory


def test_limits_must_be_a_list(tmp_path: Path) -> None:
    source = dict(SOURCE)
    source["root"] = str(tmp_path)
    assert usage_from({"sources": [source], "limits": dict(LIMIT)}, KINDS) is None
