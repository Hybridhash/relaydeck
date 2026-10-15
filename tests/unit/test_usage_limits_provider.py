"""Handshake, parsing, digests, all against the fake stdio server."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import time
from dataclasses import asdict
from pathlib import Path

from relaydeck.usage.contracts import LimitError
from relaydeck.usage.limits_plane.contracts import FetchContext
from relaydeck.usage.limits_plane.providers.jsonrpc_rate_limits_stdio import (
    FORMAT_ID,
    JsonRpcRateLimitsProvider,
    account_ref_for,
    package_version,
)
from tests.support.usage.store import NOW

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "usage" / "limits_provider"
HOST = "h1"
KIND = "kind-a"
KEY = f"{HOST}/{KIND}/{FORMAT_ID}"
RAW_ID = "synthetic-account-1"

assert os.path.isabs(sys.executable)


def command_for(tmp_path: Path, transcript: str) -> tuple[tuple[str, ...], Path]:
    log = tmp_path / f"{transcript}.requests.log"
    argv = (
        sys.executable,
        str(FIXTURES / "fake_app_server.py"),
        str(FIXTURES / f"{transcript}.json"),
        str(log),
    )
    return argv, log


def fetch(tmp_path: Path, transcript: str, timeout: float = 10.0, key: Path | None = None) -> tuple:
    argv, log = command_for(tmp_path, transcript)
    key_path = tmp_path / "limits-account.key" if key is None else key
    ctx = FetchContext(
        KEY,
        KIND,
        FORMAT_ID,
        NOW,
        timeout,
        lambda argv, timeout_s, extra: b"",
        command=argv,
        key_path=key_path,
    )
    result = JsonRpcRateLimitsProvider().fetch(ctx)
    return result, log, key_path


def requests_of(log: Path) -> list:
    return [json.loads(line) for line in log.read_text().splitlines() if line.strip()]


def test_happy_path_one_bucket(tmp_path: Path) -> None:
    result, log, key_path = fetch(tmp_path, "happy")
    assert result.error is None
    (snapshot,) = result.snapshots
    assert snapshot.kind == KIND
    assert snapshot.quota_key == "limit-a"
    assert snapshot.observed_ms == NOW
    assert snapshot.source == FORMAT_ID
    assert snapshot.account_ref is not None and snapshot.account_ref != RAW_ID
    (pane,) = snapshot.windows
    assert pane.window_key == "primary"
    assert pane.used_percent == 41
    assert pane.window_minutes == 10080
    assert pane.resets_at_ms == 1791820000 * 1000  # server seconds -> ms
    assert key_path.exists()
    # Handshake: initialize, initialized notification, exactly one read.
    requests = requests_of(log)
    assert len(requests) == 3
    assert requests[0]["method"] == "initialize"
    assert requests[0]["id"] == 1
    assert requests[0]["params"]["clientInfo"]["name"] == "relaydeck"
    assert requests[0]["params"]["clientInfo"]["version"] == package_version()
    assert requests[1] == {"jsonrpc": "2.0", "method": "initialized"}
    assert requests[2]["method"] == "account/rateLimits/read"
    assert requests[2]["id"] == 2


def test_multiple_limit_ids(tmp_path: Path) -> None:
    result, _, _ = fetch(tmp_path, "multi")
    assert result.error is None
    by_quota = {s.quota_key: s for s in result.snapshots}
    assert set(by_quota) == {"limit-a", "limit-b"}
    assert [w.window_key for w in by_quota["limit-a"].windows] == ["primary"]
    assert [w.window_key for w in by_quota["limit-b"].windows] == ["primary", "secondary"]
    assert by_quota["limit-b"].windows[1].window_minutes == 10080


def test_legacy_rate_limits_without_by_id(tmp_path: Path) -> None:
    result, _, _ = fetch(tmp_path, "legacy")
    assert result.error is None
    (snapshot,) = result.snapshots
    assert snapshot.quota_key == "limit-a"
    assert snapshot.windows[0].used_percent == 33


def test_missing_secondary_is_absent_not_zero(tmp_path: Path) -> None:
    result, _, _ = fetch(tmp_path, "happy")
    assert result.error is None
    assert [w.window_key for w in result.snapshots[0].windows] == ["primary"]


def test_auth_error_maps(tmp_path: Path) -> None:
    result, _, _ = fetch(tmp_path, "error_auth")
    assert result.error == LimitError.AUTH_REQUIRED
    assert result.snapshots == ()


def test_plain_error_maps_unavailable(tmp_path: Path) -> None:
    result, _, _ = fetch(tmp_path, "error_plain")
    assert result.error == LimitError.UNAVAILABLE
    assert result.snapshots == ()


def test_init_error_fails_the_fetch(tmp_path: Path) -> None:
    result, _, _ = fetch(tmp_path, "error_init")
    assert result.error == LimitError.AUTH_REQUIRED
    assert result.snapshots == ()


def test_noise_lines_ignored(tmp_path: Path) -> None:
    result, _, _ = fetch(tmp_path, "noise")
    assert result.error is None
    assert len(result.snapshots) == 1


def test_malformed_bucket_maps_parse(tmp_path: Path) -> None:
    result, _, _ = fetch(tmp_path, "bad_bucket")
    assert result.error == LimitError.PARSE
    assert result.snapshots == ()


def test_output_over_cap_maps_parse(tmp_path: Path) -> None:
    result, _, _ = fetch(tmp_path, "big")
    assert result.error == LimitError.PARSE
    assert result.snapshots == ()


def test_early_exit_maps_unavailable(tmp_path: Path) -> None:
    result, _, _ = fetch(tmp_path, "exit")
    assert result.error == LimitError.UNAVAILABLE
    assert result.snapshots == ()


def test_hang_killed_on_timeout(tmp_path: Path) -> None:
    argv, log = command_for(tmp_path, "hang")
    ctx = FetchContext(
        KEY,
        KIND,
        FORMAT_ID,
        NOW,
        0.5,
        lambda argv, timeout_s, extra: b"",
        command=argv,
        key_path=tmp_path / "limits-account.key",
    )
    started = time.monotonic()
    result = JsonRpcRateLimitsProvider().fetch(ctx)
    assert time.monotonic() - started < 10
    assert result.error == LimitError.TIMEOUT
    assert result.snapshots == ()
    # The process group is gone: nothing still runs this fake invocation.
    time.sleep(0.2)
    found = subprocess.run(["pgrep", "-f", str(log)], capture_output=True, text=True)
    assert found.stdout == ""


def test_missing_account_id_keeps_reading_without_ref(tmp_path: Path) -> None:
    result, _, _ = fetch(tmp_path, "no_account")
    assert result.error is None
    (snapshot,) = result.snapshots
    assert snapshot.account_ref is None


def test_empty_command_is_unavailable(tmp_path: Path) -> None:
    ctx = FetchContext(
        KEY,
        KIND,
        FORMAT_ID,
        NOW,
        5.0,
        lambda argv, timeout_s, extra: b"",
        command=(),
        key_path=tmp_path / "limits-account.key",
    )
    result = JsonRpcRateLimitsProvider().fetch(ctx)
    assert result.error == LimitError.UNAVAILABLE
    assert result.snapshots == ()


def test_digest_stable_per_key_and_distinct_across_keys(tmp_path: Path) -> None:
    first = tmp_path / "a.key"
    second = tmp_path / "b.key"
    ref = account_ref_for(first, RAW_ID)
    assert ref is not None and len(ref) == 16
    assert account_ref_for(first, RAW_ID) == ref
    assert account_ref_for(second, RAW_ID) != ref


def test_key_file_created_0600_with_32_bytes(tmp_path: Path) -> None:
    key_path = tmp_path / "limits-account.key"
    assert account_ref_for(key_path, RAW_ID) is not None
    assert len(key_path.read_bytes()) == 32
    assert stat.S_IMODE(key_path.stat().st_mode) == 0o600


def test_missing_key_means_none(tmp_path: Path) -> None:
    assert account_ref_for(None, RAW_ID) is None
    assert account_ref_for(tmp_path / "no-such-dir" / "k", RAW_ID) is None
    assert account_ref_for(tmp_path / "k", "") is None
    assert account_ref_for(tmp_path / "k", None) is None


def test_raw_account_id_never_in_snapshot(tmp_path: Path) -> None:
    result, _, _ = fetch(tmp_path, "happy")
    assert result.error is None
    dumped = json.dumps(asdict(result.snapshots[0]), default=str)
    assert RAW_ID not in dumped
    assert RAW_ID not in repr(result.snapshots[0])
