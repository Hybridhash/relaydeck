"""Concurrent sessions reconcile per window: the maximum within the newest reset.

Each session reports the whole account's limits, and their readings can disagree for a moment.
persist() merges under the existing lock: a newer resets_at replaces that window, an equal one
keeps the maximum used percentage, an older one is ignored, and a window a payload omits is
kept. The receipt/confirm/generation lock format is unchanged. Tests use temp dirs only.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from relaydeck.usage import hook_install
from relaydeck.usage.contracts import Budget, Cursor, LimitStatus
from relaydeck.usage.hook_install import install_hook
from relaydeck.usage.hook_resource import hook_bytes
from relaydeck.usage.hook_setup import HOOK_FILENAME, HOOK_SHA256, PREVIOUS_HOOK_SHA256
from relaydeck.usage.limits.json_statusline_limits import JsonStatuslineLimitsReader
from relaydeck.usage.resources import statusline_limits_hook as hook

T0 = 1_700_000_000_000
MINUTE = 60 * 1000
GEN_A = "a" * 32
GEN_B = "b" * 32
RESET_1 = T0 // 1000 + 3600
RESET_2 = T0 // 1000 + 2 * 3600


@pytest.fixture
def cache(tmp_path: Path) -> Path:
    directory = tmp_path / "state"
    directory.mkdir(mode=0o700)
    return directory / "statusline-limits.json"


def _windows(cache: Path) -> dict[str, Any]:
    return json.loads(cache.read_text())["windows"]


def _read(cache: Path, now: int) -> Any:
    snapshots, _cursor = JsonStatuslineLimitsReader(cache).read(
        cache.parent, "kind-a", Cursor(), Budget(), now
    )
    return snapshots[0] if snapshots else None


def _window(used: int | float, reset: int) -> dict[str, dict[str, int | float]]:
    return {"five_hour": {"used_percentage": used, "resets_at": reset}}


# -- the merge rules -----------------------------------------------------------------


def test_interleaved_sessions_keep_the_maximum_at_the_same_reset(cache: Path) -> None:
    assert hook.persist(cache, _window(30, RESET_1), T0, GEN_A)
    assert hook.persist(cache, _window(25, RESET_1), T0 + MINUTE, GEN_B)
    assert _windows(cache) == _window(30, RESET_1)


def test_reversed_arrival_still_keeps_the_maximum(cache: Path) -> None:
    assert hook.persist(cache, _window(25, RESET_1), T0, GEN_B)
    assert hook.persist(cache, _window(30, RESET_1), T0 + MINUTE, GEN_A)
    assert _windows(cache) == _window(30, RESET_1)


def test_same_millisecond_sessions_still_merge(cache: Path) -> None:
    assert hook.persist(cache, _window(25, RESET_1), T0, GEN_B)
    assert hook.persist(cache, _window(30, RESET_1), T0, GEN_A)
    assert _windows(cache) == _window(30, RESET_1)


def test_a_new_reset_replaces_the_window_even_when_lower(cache: Path) -> None:
    assert hook.persist(cache, _window(30, RESET_1), T0, GEN_A)
    assert hook.persist(cache, _window(5, RESET_2), T0 + MINUTE, GEN_B)
    assert _windows(cache) == _window(5, RESET_2)
    assert json.loads(cache.read_text())["captured_ms"] == T0 + MINUTE


def test_an_older_reset_arriving_late_is_ignored(cache: Path) -> None:
    assert hook.persist(cache, _window(30, RESET_2), T0, GEN_A)
    assert hook.persist(cache, _window(90, RESET_1), T0 + MINUTE, GEN_B)
    assert _windows(cache) == _window(30, RESET_2)
    assert json.loads(cache.read_text())["captured_ms"] == T0


def test_each_window_merges_independently(cache: Path) -> None:
    stored = {
        "five_hour": {"used_percentage": 30, "resets_at": RESET_2},
        "seven_day": {"used_percentage": 10, "resets_at": RESET_1},
    }
    assert hook.persist(cache, stored, T0, GEN_A)
    incoming = {
        "five_hour": {"used_percentage": 90, "resets_at": RESET_1},  # older: ignored
        "seven_day": {"used_percentage": 12, "resets_at": RESET_1},  # same: max
    }
    assert hook.persist(cache, incoming, T0 + MINUTE, GEN_B)
    assert _windows(cache) == {
        "five_hour": {"used_percentage": 30, "resets_at": RESET_2},
        "seven_day": {"used_percentage": 12, "resets_at": RESET_1},
    }


def test_a_window_missing_from_a_payload_is_kept(cache: Path) -> None:
    stored = {
        "five_hour": {"used_percentage": 30, "resets_at": RESET_1},
        "seven_day": {"used_percentage": 10, "resets_at": RESET_1},
    }
    assert hook.persist(cache, stored, T0, GEN_A)
    assert hook.persist(cache, _window(40, RESET_1), T0 + MINUTE, GEN_B)
    assert _windows(cache) == {
        "five_hour": {"used_percentage": 40, "resets_at": RESET_1},
        "seven_day": {"used_percentage": 10, "resets_at": RESET_1},
    }


def test_a_window_missing_from_the_cache_is_added(cache: Path) -> None:
    assert hook.persist(cache, _window(30, RESET_1), T0, GEN_A)
    incoming = {
        "five_hour": {"used_percentage": 25, "resets_at": RESET_1},
        "seven_day": {"used_percentage": 10, "resets_at": RESET_1},
    }
    assert hook.persist(cache, incoming, T0 + MINUTE, GEN_B)
    assert _windows(cache) == {
        "five_hour": {"used_percentage": 30, "resets_at": RESET_1},
        "seven_day": {"used_percentage": 10, "resets_at": RESET_1},
    }


def test_newer_reset_first_then_older_reset_keeps_the_newer(cache: Path) -> None:
    assert hook.persist(cache, _window(5, RESET_2), T0, GEN_A)
    assert hook.persist(cache, _window(90, RESET_1), T0 + MINUTE, GEN_B)
    assert _windows(cache) == _window(5, RESET_2)


# -- freshness and fencing still hold across sessions ----------------------------------


def test_a_cross_session_repaint_with_a_new_generation_stays_fresh(cache: Path) -> None:
    windows = _window(30, RESET_1)
    assert hook.persist(cache, windows, T0, GEN_A)
    later = T0 + 10 * MINUTE
    assert hook.persist(cache, windows, later, GEN_B)  # another session, same numbers
    assert json.loads(cache.read_text())["captured_ms"] == T0
    snapshot = _read(cache, T0 + 20 * MINUTE)
    assert snapshot.observed_ms == later
    assert snapshot.status == LimitStatus.FRESH


def test_a_merged_no_change_still_fences_an_older_different_payload(cache: Path) -> None:
    assert hook.persist(cache, _window(30, RESET_1), T0, GEN_A)
    assert hook.persist(cache, _window(25, RESET_1), T0 + 5 * MINUTE, GEN_B)  # merged away
    assert not hook.persist(cache, _window(40, RESET_1), T0 + 2 * MINUTE, GEN_B)
    assert _windows(cache) == _window(30, RESET_1)


# -- the hook upgrade ------------------------------------------------------------------


def test_the_allow_list_upgrades_the_hook_this_build_replaces() -> None:
    assert PREVIOUS_HOOK_SHA256 == (
        "eece0979aa2ccd21be47121fd2ba2a958a7a2caf8ccf4c8a3b8b24c7995c0c64",
        "f5632268feff5fa6ee8ec65e97049bbbd30f3f42f8000d977267fced84f0e5e7",
        "81833a623b5e14b8b0cf242d87ffdbe3fafb01ceee87e06845bc946fcde4914c",
    )
    assert HOOK_SHA256 not in PREVIOUS_HOOK_SHA256


def test_an_upgrade_from_a_previous_hash_replaces_the_script(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path.resolve()
    root.chmod(0o700)
    settings = root / "settings.json"
    settings.write_text(json.dumps({"unrelated": True}))
    settings.chmod(0o600)
    cache = root / "state/cache.json"
    hooks = root / "hooks"
    python = root / "tool env/python"
    python.parent.mkdir(mode=0o700)
    python.symlink_to(Path(sys.executable))
    assert (
        install_hook(settings, cache, confirmed=True, hooks_dir=hooks, interpreter=python).state
        == "installed"
    )
    planted = b"# synthetic previous RelayDeck hook\n"
    digest = hashlib.sha256(planted).hexdigest()
    monkeypatch.setattr(hook_install, "PREVIOUS_HOOK_SHA256", (digest, *PREVIOUS_HOOK_SHA256))
    script = hooks / HOOK_FILENAME
    script.write_bytes(planted)
    receipt = hooks / ("limits-" + hashlib.sha256(bytes(settings)).hexdigest() + ".json")
    body = json.loads(receipt.read_text())
    body["hash"] = digest
    receipt.write_text(json.dumps(body))
    assert (
        install_hook(settings, cache, confirmed=True, hooks_dir=hooks, interpreter=python).state
        == "installed"
    )
    assert script.read_bytes() == hook_bytes()
    assert json.loads(receipt.read_text())["hash"] == HOOK_SHA256
    assert json.loads(settings.read_text())["unrelated"] is True
