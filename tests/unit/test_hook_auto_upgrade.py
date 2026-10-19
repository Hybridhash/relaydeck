"""A previous shipped hook refreshes itself at startup and in the periodic check.

A synthetic previous version is planted in a temporary directory: its bytes, a receipt that
records its hash, and the settings line that the earlier install wrote. The allow-list is
monkeypatched per version, so the real shipped previous bytes (hashes only in the repo) are
not needed here. Never touches the owner's CLI settings or ~/.config.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import stat
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from relaydeck.companion.config import UsageConfig, UsageSourceConfig
from relaydeck.companion.lifecycle import status, supervise
from relaydeck.companion.runtime import CompanionRuntime
from relaydeck.gateway.usage import UsageService
from relaydeck.usage import hook_files as fs
from relaydeck.usage import hook_install
from relaydeck.usage.hook_doctor import doctor_sources
from relaydeck.usage.hook_install import (
    install_hook,
    uninstall_hook,
    upgrade_available,
    upgrade_outdated_hook,
)
from relaydeck.usage.hook_repair import diagnose_source, repair_sources
from relaydeck.usage.hook_resource import hook_bytes
from relaydeck.usage.hook_setup import HOOK_FILENAME, HOOK_SHA256, managed_command

NOW_MS = 2_000_000_000_000
FORMAT = "jsonl-message-usage-v1"
LIMITS_FORMAT = "json-statusline-limits-v1"
VERSIONS = (
    b"# synthetic previous RelayDeck hook v1",
    b"# synthetic previous RelayDeck hook v2",
    b"# synthetic previous RelayDeck hook v3",
)
UNKNOWN_BODY = b"# someone else's statusline script"


@dataclass
class Files:
    settings: Path
    cache: Path
    hooks: Path
    kind: str = "tool-a"

    @property
    def script(self) -> Path:
        return self.hooks / HOOK_FILENAME

    def source(self) -> UsageSourceConfig:
        return UsageSourceConfig(
            kind=self.kind,
            format=FORMAT,
            root=self.settings.parent,
            display_name="Tool A",
            limits_format=LIMITS_FORMAT,
            limits_settings=self.settings,
            limits_cache=self.cache,
        )

    def service(self) -> UsageService:
        config = UsageConfig(timezone="UTC", sources=(self.source(),))
        return UsageService(
            None, config, "home-mac", self.settings.parent, lambda: NOW_MS, hooks_dir=self.hooks
        )

    def install(self) -> Any:
        return install_hook(self.settings, self.cache, confirmed=True, hooks_dir=self.hooks)

    def plant_previous(self, monkeypatch: pytest.MonkeyPatch, body: bytes) -> str:
        """Install normally, then rewrite the script and receipt as the earlier install
        left them, with that version allow-listed."""
        digest = hashlib.sha256(body).hexdigest()
        monkeypatch.setattr(hook_install, "PREVIOUS_HOOK_SHA256", (digest,))
        assert self.install().state == "installed"
        self.script.write_bytes(body)
        receipt = self.hooks / (
            "limits-" + hashlib.sha256(bytes(self.settings)).hexdigest() + ".json"
        )
        payload = json.loads(receipt.read_text())
        payload["hash"] = digest
        receipt.write_text(json.dumps(payload))
        receipt.chmod(0o600)
        return digest


@pytest.fixture
def files(tmp_path: Path) -> Files:
    root = tmp_path.resolve()
    root.chmod(0o700)
    settings = root / "cli" / "settings.json"
    settings.parent.mkdir(mode=0o700)
    settings.write_text(json.dumps({"unrelated": True}))
    settings.chmod(0o600)
    return Files(settings, root / "private" / "cache.json", root / "hooks")


def _upgraded(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if "usage_hook_upgraded" in r.getMessage()]


def _uncertain(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        r.getMessage() for r in caplog.records if "usage_hook_upgrade_uncertain" in r.getMessage()
    ]


def _failed(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if "usage_hook_upgrade_failed" in r.getMessage()]


def _receipt(files: Files) -> Path:
    return files.hooks / ("limits-" + hashlib.sha256(bytes(files.settings)).hexdigest() + ".json")


def _count_attempts(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    attempts: list[str] = []
    real_upgrade = hook_install.upgrade_outdated_hook

    def counted(*args: Any, **kwargs: Any) -> Any:
        attempts.append("attempt")
        return real_upgrade(*args, **kwargs)

    monkeypatch.setattr("relaydeck.gateway.usage.upgrade_outdated_hook", counted)
    return attempts


# -- startup ----------------------------------------------------------------------


@pytest.mark.parametrize("body", VERSIONS)
def test_each_previous_version_upgrades_at_startup_exactly_once(
    files: Files, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, body: bytes
) -> None:
    files.plant_previous(monkeypatch, body)
    settings_before = files.settings.read_bytes()
    with caplog.at_level(logging.INFO, logger="relaydeck.gateway.usage"):
        files.service().run_startup_upgrades()
        files.service().run_startup_upgrades()
    assert files.script.read_bytes() == hook_bytes()
    assert files.settings.read_bytes() == settings_before
    assert _upgraded(caplog) == [f"usage_hook_upgraded source={files.kind}/{FORMAT}"]


def test_a_healthy_startup_writes_nothing_and_logs_nothing(
    files: Files, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(hook_install, "PREVIOUS_HOOK_SHA256", ())
    assert files.install().state == "installed"
    before = {p: p.read_bytes() for p in sorted(files.hooks.rglob("*")) if p.is_file()}
    with caplog.at_level(logging.INFO, logger="relaydeck.gateway.usage"):
        files.service().run_startup_upgrades()
    assert {p: p.read_bytes() for p in sorted(files.hooks.rglob("*")) if p.is_file()} == before
    assert _upgraded(caplog) == []


# -- what is never replaced -----------------------------------------------------------


def test_an_unknown_script_is_never_replaced_and_stays_outdated(
    files: Files, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    files.plant_previous(monkeypatch, VERSIONS[0])
    files.script.write_bytes(UNKNOWN_BODY)
    with caplog.at_level(logging.INFO, logger="relaydeck.gateway.usage"):
        files.service().run_startup_upgrades()
    assert files.script.read_bytes() == UNKNOWN_BODY
    assert _upgraded(caplog) == []
    service = files.service()
    assert service._checks[(files.kind, FORMAT)].check(NOW_MS)["state"] == "script_outdated"


def test_an_edited_previous_script_is_never_replaced(
    files: Files, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    files.plant_previous(monkeypatch, VERSIONS[0])
    files.script.write_bytes(VERSIONS[0] + b"# owner tweak")
    with caplog.at_level(logging.INFO, logger="relaydeck.gateway.usage"):
        files.service().run_startup_upgrades()
    assert files.script.read_bytes() == VERSIONS[0] + b"# owner tweak"
    assert _upgraded(caplog) == []


def test_a_group_writable_script_is_refused(
    files: Files, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    files.plant_previous(monkeypatch, VERSIONS[0])
    files.script.chmod(0o664)
    with caplog.at_level(logging.INFO, logger="relaydeck.gateway.usage"):
        files.service().run_startup_upgrades()
    assert files.script.read_bytes() == VERSIONS[0]
    assert _upgraded(caplog) == []


def test_a_group_writable_hooks_directory_is_refused(
    files: Files, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    files.plant_previous(monkeypatch, VERSIONS[0])
    files.hooks.chmod(0o775)
    with caplog.at_level(logging.INFO, logger="relaydeck.gateway.usage"):
        files.service().run_startup_upgrades()
    assert files.script.read_bytes() == VERSIONS[0]
    assert _upgraded(caplog) == []


def test_a_foreign_owned_install_is_refused(
    files: Files, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    files.plant_previous(monkeypatch, VERSIONS[0])
    monkeypatch.setattr("os.getuid", lambda: 43210)
    with caplog.at_level(logging.INFO, logger="relaydeck.gateway.usage"):
        files.service().run_startup_upgrades()
    assert files.script.read_bytes() == VERSIONS[0]
    assert _upgraded(caplog) == []


def test_a_status_line_not_written_by_relaydeck_blocks_the_upgrade(
    files: Files, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    files.plant_previous(monkeypatch, VERSIONS[0])
    body = json.loads(files.settings.read_text())
    body["statusLine"] = {"type": "command", "command": "owner-command"}
    files.settings.write_text(json.dumps(body))
    with caplog.at_level(logging.INFO, logger="relaydeck.gateway.usage"):
        files.service().run_startup_upgrades()
    assert files.script.read_bytes() == VERSIONS[0]
    assert _upgraded(caplog) == []


def test_a_missing_receipt_blocks_the_upgrade(
    files: Files, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    files.plant_previous(monkeypatch, VERSIONS[0])
    receipt = files.hooks / (
        "limits-" + hashlib.sha256(bytes(files.settings)).hexdigest() + ".json"
    )
    receipt.unlink()
    with caplog.at_level(logging.INFO, logger="relaydeck.gateway.usage"):
        files.service().run_startup_upgrades()
    assert files.script.read_bytes() == VERSIONS[0]
    assert _upgraded(caplog) == []


# -- repair and doctor ------------------------------------------------------------------


def test_repair_asks_first_then_upgrades_an_outdated_script(
    files: Files, monkeypatch: pytest.MonkeyPatch
) -> None:
    files.plant_previous(monkeypatch, VERSIONS[0])
    diagnosis = diagnose_source(files.source(), hooks_dir=files.hooks)
    assert diagnosis.reason == "hook_script_outdated"
    assert diagnosis.repairable is True
    assert diagnosis.fix.startswith("relaydeck usage repair --source ")
    assert "settings file is not changed" in diagnosis.change
    asked: list[Any] = []

    def confirm(planned: Any) -> bool:
        asked.append(list(planned))
        return True

    settings_before = files.settings.read_bytes()
    (outcome,) = repair_sources([files.source()], confirm=confirm, hooks_dir=files.hooks)
    assert len(asked) == 1
    assert outcome.action == "repaired"
    assert files.script.read_bytes() == hook_bytes()
    assert files.settings.read_bytes() == settings_before


def test_declining_repair_leaves_the_outdated_script_in_place(
    files: Files, monkeypatch: pytest.MonkeyPatch
) -> None:
    files.plant_previous(monkeypatch, VERSIONS[0])
    (outcome,) = repair_sources(
        [files.source()], confirm=lambda _planned: False, hooks_dir=files.hooks
    )
    assert outcome.action == "confirmation_declined"
    assert files.script.read_bytes() == VERSIONS[0]


def test_doctor_reports_repairable_only_when_the_rule_holds(
    files: Files, monkeypatch: pytest.MonkeyPatch
) -> None:
    files.plant_previous(monkeypatch, VERSIONS[0])
    (row,) = doctor_sources([files.source()], hooks_dir=files.hooks)
    assert row["installation"]["state"] == "script_outdated"
    assert row["diagnosis"]["reason"] == "hook_script_outdated"
    assert row["diagnosis"]["repairable"] is True
    files.script.write_bytes(UNKNOWN_BODY)
    (row,) = doctor_sources([files.source()], hooks_dir=files.hooks)
    assert row["diagnosis"]["reason"] == "hook_script_outdated"
    assert row["diagnosis"]["repairable"] is False
    assert "changed outside RelayDeck" in row["diagnosis"]["summary"]


def test_a_previous_script_with_a_custom_line_is_left_alone_with_a_reason(
    files: Files, monkeypatch: pytest.MonkeyPatch
) -> None:
    files.plant_previous(monkeypatch, VERSIONS[0])
    body = json.loads(files.settings.read_text())
    body["statusLine"] = {"type": "command", "command": "owner-command"}
    files.settings.write_text(json.dumps(body))
    diagnosis = diagnose_source(files.source(), hooks_dir=files.hooks)
    assert diagnosis.reason == "hook_script_outdated"
    assert diagnosis.repairable is False
    assert "left alone" in diagnosis.summary
    (outcome,) = repair_sources(
        [files.source()], confirm=lambda _planned: True, hooks_dir=files.hooks
    )
    assert outcome.action == "not_repairable"
    assert files.script.read_bytes() == VERSIONS[0]


def test_the_managed_line_after_upgrade_runs_the_shipped_script(
    files: Files, monkeypatch: pytest.MonkeyPatch
) -> None:
    files.plant_previous(monkeypatch, VERSIONS[0])
    files.service().run_startup_upgrades()
    line = json.loads(files.settings.read_text())["statusLine"]
    assert line == {
        "type": "command",
        "command": managed_command(files.script, files.cache, Path(sys.executable)),
    }
    assert hashlib.sha256(files.script.read_bytes()).hexdigest() == HOOK_SHA256


# -- the check-to-write race ------------------------------------------------------------------


def test_an_uninstall_and_removal_mid_transaction_writes_nothing(
    files: Files, monkeypatch: pytest.MonkeyPatch
) -> None:
    files.plant_previous(monkeypatch, VERSIONS[0])
    real_atomic = fs.atomic
    real_read = fs.read
    raced = False
    evidence: dict[str, bytes] = {}
    targets: list[str] = []

    def atomic_spy(path: Path, *args: Any, **kwargs: Any) -> None:
        targets.append(path.name)
        real_atomic(path, *args, **kwargs)

    def read_then_race(fd: int, name: str, **kwargs: Any) -> Any:
        nonlocal raced
        image = real_read(fd, name, **kwargs)
        if not raced:
            raced = True
            assert (
                uninstall_hook(
                    files.settings, files.cache, confirmed=True, hooks_dir=files.hooks
                ).state
                == "uninstalled"
            )
            files.script.unlink()
            evidence["settings"] = files.settings.read_bytes()
            del targets[:]  # the race's own writes; ours must add nothing after this
        return image

    monkeypatch.setattr(fs, "atomic", atomic_spy)
    monkeypatch.setattr(fs, "read", read_then_race)
    assert (
        upgrade_outdated_hook(files.settings, files.cache, hooks_dir=files.hooks) == "not_upgraded"
    )
    assert raced
    assert files.settings.read_bytes() == evidence["settings"]
    assert not files.script.exists()
    assert targets == []


def test_a_successful_upgrade_only_targets_the_script_and_receipt(
    files: Files, monkeypatch: pytest.MonkeyPatch
) -> None:
    files.plant_previous(monkeypatch, VERSIONS[0])
    real_atomic = fs.atomic
    targets: list[str] = []

    def atomic_spy(path: Path, *args: Any, **kwargs: Any) -> None:
        targets.append(path.name)
        real_atomic(path, *args, **kwargs)

    monkeypatch.setattr(fs, "atomic", atomic_spy)
    assert upgrade_outdated_hook(files.settings, files.cache, hooks_dir=files.hooks) == "upgraded"
    assert sorted(targets) == sorted([HOOK_FILENAME, _receipt(files).name])
    assert files.settings.name not in targets


# -- receipt backup validity --------------------------------------------------------------------


@pytest.mark.parametrize("corruption", ["missing", "corrupt", "unsafe"])
def test_an_invalid_backup_leaves_everything_byte_identical(
    files: Files, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, corruption: str
) -> None:
    files.plant_previous(monkeypatch, VERSIONS[0])
    receipt = _receipt(files)
    backup = files.hooks / json.loads(receipt.read_text())["backup"]
    if corruption == "missing":
        backup.unlink()
    elif corruption == "corrupt":
        backup.write_bytes(b'{"not": "the recorded bytes"}')
    else:
        backup.chmod(0o664)
    assert upgrade_available(files.settings, files.cache, hooks_dir=files.hooks) is False
    assert (
        upgrade_outdated_hook(files.settings, files.cache, hooks_dir=files.hooks) == "not_upgraded"
    )
    script_before = files.script.read_bytes()
    receipt_before = receipt.read_bytes()
    settings_before = files.settings.read_bytes()
    with caplog.at_level(logging.INFO, logger="relaydeck.gateway.usage"):
        files.service().run_startup_upgrades()
    assert files.script.read_bytes() == script_before
    assert receipt.read_bytes() == receipt_before
    assert files.settings.read_bytes() == settings_before
    assert _upgraded(caplog) == []


def test_a_receipt_write_failure_after_commit_reports_uncertain(
    files: Files, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    files.plant_previous(monkeypatch, VERSIONS[0])
    real_atomic = fs.atomic

    def fail_receipt(path: Path, *args: Any, **kwargs: Any) -> None:
        if path.name.endswith(".json"):
            raise OSError("receipt disk full")
        real_atomic(path, *args, **kwargs)

    monkeypatch.setattr(fs, "atomic", fail_receipt)
    with caplog.at_level(logging.INFO, logger="relaydeck.gateway.usage"):
        files.service().run_startup_upgrades()
    assert files.script.read_bytes() == hook_bytes()
    assert _upgraded(caplog) == []
    assert _uncertain(caplog) == [f"usage_hook_upgrade_uncertain source={files.kind}/{FORMAT}"]


def test_a_script_sync_failure_after_commit_reports_uncertain(
    files: Files, monkeypatch: pytest.MonkeyPatch
) -> None:
    files.plant_previous(monkeypatch, VERSIONS[0])
    real_fsync = os.fsync

    def fail_dir_fsync(fd: int) -> None:
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError("directory sync failed")
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fail_dir_fsync)
    assert upgrade_outdated_hook(files.settings, files.cache, hooks_dir=files.hooks) == "uncertain"
    assert files.script.read_bytes() == hook_bytes()


# -- pages stay read-only ------------------------------------------------------------------


def test_pages_never_attempt_upgrades_even_under_contention(
    files: Files, monkeypatch: pytest.MonkeyPatch
) -> None:
    files.plant_previous(monkeypatch, VERSIONS[0])
    service = files.service()
    attempts = _count_attempts(monkeypatch)
    with fs.directory(files.hooks, private=True) as fd, fs.lock(fd, ".hook-install.lock"):
        start = time.monotonic()
        for _ in range(3):
            state = service._metadata(NOW_MS)[0]["sources"][0]["limits_setup"]["state"]
        elapsed = time.monotonic() - start
    assert state == "script_outdated"
    assert attempts == []
    assert files.script.read_bytes() == VERSIONS[0]
    assert elapsed < 1.0


# -- startup is the only attempt -------------------------------------------------------


def test_startup_under_contention_proceeds_without_upgrading(
    files: Files, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    files.plant_previous(monkeypatch, VERSIONS[0])
    service = files.service()
    with fs.directory(files.hooks, private=True) as fd, fs.lock(fd, ".hook-install.lock"):
        with caplog.at_level(logging.INFO, logger="relaydeck.gateway.usage"):
            start = time.monotonic()
            service.run_startup_upgrades()
            elapsed = time.monotonic() - start
    assert elapsed < 2.0
    assert files.script.read_bytes() == VERSIONS[0]
    assert _upgraded(caplog) == []


def test_startup_survives_an_unexpected_upgrade_failure(
    files: Files, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    files.plant_previous(monkeypatch, VERSIONS[0])

    def fail(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("synthetic disk failure")

    monkeypatch.setattr("relaydeck.gateway.usage.upgrade_outdated_hook", fail)
    with caplog.at_level(logging.INFO, logger="relaydeck.gateway.usage"):
        files.service().run_startup_upgrades()
    assert files.script.read_bytes() == VERSIONS[0]
    assert _upgraded(caplog) == []
    assert _uncertain(caplog) == []
    (line,) = _failed(caplog)
    assert line == f"usage_hook_upgrade_failed source={files.kind}/{FORMAT}"


def test_the_first_page_after_an_upgraded_startup_shows_the_healthy_state(
    files: Files, monkeypatch: pytest.MonkeyPatch
) -> None:
    files.plant_previous(monkeypatch, VERSIONS[0])
    service = files.service()
    service.check_setup()
    assert service._metadata(NOW_MS)[0]["sources"][0]["limits_setup"]["state"] == "script_outdated"
    service.run_startup_upgrades()
    assert service._metadata(NOW_MS)[0]["sources"][0]["limits_setup"]["state"] == "installed"


def test_a_failure_while_releasing_locks_after_commit_reports_uncertain(
    files: Files, monkeypatch: pytest.MonkeyPatch
) -> None:
    files.plant_previous(monkeypatch, VERSIONS[0])
    real_lock = fs.lock
    released: list[str] = []

    @contextmanager
    def fail_release(fd: int, name: str, **kwargs: Any) -> Any:
        with real_lock(fd, name, **kwargs):
            yield
        released.append(name)
        raise OSError("synthetic lock release failure")

    monkeypatch.setattr(fs, "lock", fail_release)
    settings_before = files.settings.read_bytes()
    assert upgrade_outdated_hook(files.settings, files.cache, hooks_dir=files.hooks) == "uncertain"
    assert released
    assert files.script.read_bytes() == hook_bytes()
    assert files.settings.read_bytes() == settings_before


def test_stop_during_startup_leaves_no_upgrade_after_close(
    files: Files, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    files.plant_previous(monkeypatch, VERSIONS[0])
    service = files.service()
    real_atomic = fs.atomic
    entered = threading.Event()
    release = threading.Event()
    prep_entered = threading.Event()
    prep_release = threading.Event()
    prep_done = threading.Event()

    def barrier(path: Path, *args: Any, **kwargs: Any) -> None:
        if path == files.script:
            entered.set()
            assert release.wait(timeout=10)
        real_atomic(path, *args, **kwargs)

    monkeypatch.setattr(fs, "atomic", barrier)
    attempts = _count_attempts(monkeypatch)
    script_before = files.script.read_bytes()
    settings_before = files.settings.read_bytes()

    async def scenario() -> None:
        stop = asyncio.Event()

        def prep() -> None:
            prep_entered.set()
            assert prep_release.wait(timeout=10)
            service.check_setup()
            prep_done.set()

        async def start(stop: asyncio.Event) -> None:
            # Mirrors production start(): threaded checks, then the synchronous
            # pre-server upgrade step. Cancellation can only land at the await.
            await asyncio.to_thread(prep)
            service.run_startup_upgrades()

        async def close() -> None:
            await CompanionRuntime.close(stub)

        stub = SimpleNamespace(
            route=None,
            _remove_route=lambda: None,
            _request_shutdown=lambda: CompanionRuntime._request_shutdown(stub),
            _finish=CompanionRuntime._finish,
            stop=stop,
            webpush_task=None,
            usage_collector=None,
            limits_scheduler=None,
            persistence_task=None,
            link_task=None,
            source=None,
            server=None,
            server_task=None,
            gateway=SimpleNamespace(
                usage=service,
                backend=SimpleNamespace(journal=SimpleNamespace(close=lambda: None)),
                push=SimpleNamespace(close=lambda: None),
            ),
            usage_store=None,
            webpush_provider=None,
            webpush_store=None,
            web_store=None,
            persistence=None,
            start=start,
            close=close,
            report=lambda: {"state": "unexpected"},
        )
        state_dir = files.settings.parent.parent / "state"
        try:
            with caplog.at_level(logging.INFO, logger="relaydeck.gateway.usage"):
                owner = asyncio.create_task(supervise(state_dir, lambda: stub, stop))
                assert await asyncio.to_thread(prep_entered.wait, 10)
                stop.set()
                assert await asyncio.wait_for(owner, 10) == 0
                assert status(state_dir)["state"] == "stopped"
                assert attempts == []
                prep_release.set()
                assert await asyncio.to_thread(prep_done.wait, 10)
                # The gated prep finished after close: checks only, never an upgrade.
                assert attempts == []
                assert not entered.is_set()
            assert files.script.read_bytes() == script_before
            assert files.settings.read_bytes() == settings_before
            assert _upgraded(caplog) == []
        finally:
            release.set()

    asyncio.run(scenario())


def test_no_upgrade_machinery_runs_after_startup(files: Files) -> None:
    # Removal proof: with no loop, worker, drain or retry entry points, nothing can run
    # an upgrade once startup's single check_setup pass has finished.
    assert not hasattr(CompanionRuntime, "_hook_upgrade_loop")
    service = files.service()
    for name in (
        "run_hook_upgrades",
        "run_upgrade_pass",
        "drain_hook_upgrades",
        "stop_hook_upgrades",
        "_upgrade_executor",
        "_upgrade_stop",
    ):
        assert not hasattr(service, name), name
