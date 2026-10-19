"""Q3: callback failures log fixed categories only; the secret scanner never echoes matches."""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import sys
from pathlib import Path
from typing import Any

import pytest

from relaydeck.client.diagnostics import failure_category, safe_stage
from relaydeck.client.features.terminal.events import spawn

MARKER = "SYNTHETIC-SECRET-MARKER-9f3c"


class _Leaky(ValueError):
    def __init__(self) -> None:
        super().__init__(f"token={MARKER} path=/Users/synthetic/{MARKER}.txt")


def _flatten(caplog: pytest.LogCaptureFixture) -> str:
    parts: list[str] = []
    for record in caplog.records:
        parts += [record.getMessage(), str(record.args), str(record.exc_info), str(record.exc_text)]
    return "\n".join(parts)


@pytest.mark.parametrize("reason", ["offset_evicted", "probe_timeout", MARKER])
def test_ui_resume_incomplete_logs_fixed_reason_only(
    reason: str, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    from relaydeck_terminal.terminal import ResumeOutcome

    from relaydeck.client.features.terminal.ui_resume import TerminalUIResumes
    from tests.support.scripted_sessions import make_scripted_sessions
    from tests.support.terminal.resume import _open_term

    async def scenario() -> None:
        caplog.set_level("INFO", logger="relaydeck.lease_lifecycle")
        _app, term, _conn, _channel = await _open_term()
        scripted = make_scripted_sessions()
        session = scripted.factory.make_agent(
            host_id="mac", host_epoch="h1", agent_id="a", target="a"
        )
        session.open()
        await session.confirm_takeover()
        session.bind_view(term, write=lambda *_: True)

        async def incomplete(**kwargs: Any) -> ResumeOutcome:
            return ResumeOutcome("incomplete", reason)

        monkeypatch.setattr(term, "rebind_resumed", incomplete)
        resumes = TerminalUIResumes()
        try:
            task = resumes.start(
                session, transport=object(), current=lambda: True, closed=asyncio.Event()
            )
            assert task is not None
            outcome = await task
            assert outcome is not None and outcome.status == "incomplete"
            expected = "other" if reason == MARKER else reason
            assert f"ui_resume_incomplete why={expected}" in _flatten(caplog)
            assert MARKER not in _flatten(caplog)
        finally:
            await resumes.close()
            await session.dispose()

    asyncio.run(scenario())


def test_failure_category_is_allowlisted_builtin_or_other() -> None:
    assert failure_category(_Leaky()) == "ValueError"
    assert failure_category(ConnectionResetError(MARKER)) == "ConnectionError"
    assert failure_category(KeyError(MARKER)) == "KeyError"

    class Custom(Exception):
        pass

    assert failure_category(Custom(MARKER)) == "other"
    assert safe_stage("terminal-size-report") == "terminal-size-report"
    assert safe_stage(f"bad label {MARKER}") == "unknown"
    assert safe_stage(None) == "unknown"


def test_spawn_logs_stage_and_category_only(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)

    async def failing() -> None:
        raise _Leaky()

    async def scenario() -> None:
        task = spawn(failing(), label="terminal-input")
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.sleep(0)

    asyncio.run(scenario())
    text = _flatten(caplog)
    assert "stage=terminal-input category=ValueError" in text
    assert MARKER not in text and "Traceback" not in text
    assert all(record.exc_info is None for record in caplog.records)


def test_spawn_cancellation_still_silent(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)

    async def scenario() -> None:
        task = spawn(asyncio.sleep(10), label="terminal-input")
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.sleep(0)

    asyncio.run(scenario())
    assert [r for r in caplog.records if r.name.startswith("relaydeck")] == []


def test_key_bar_action_failure_logs_category_only(caplog: pytest.LogCaptureFixture) -> None:
    from relaydeck.client.components.design_system.terminal_window import MobileKeyBar
    from tests.support.host_harness import render_component, run

    caplog.set_level(logging.DEBUG)

    async def boom(*args: Any) -> None:
        raise _Leaky()

    async def scenario() -> None:
        app = await render_component(
            MobileKeyBar,
            on_item=boom,
            modifier_states={},
            enabled=True,
            on_paste=boom,
            on_select=boom,
        )
        await app.find("terminal-paste")._trigger_event("click", "")
        await app.settle()
        assert MARKER not in str(app.find("terminal-keybar-error").value)

    run(scenario())
    text = _flatten(caplog)
    assert "category=ValueError" in text
    assert MARKER not in text and "Traceback" not in text
    assert all(record.exc_info is None for record in caplog.records)


def _load_scanner() -> Any:
    path = Path(__file__).parents[2] / "scripts" / "scan_release_secrets.py"
    spec = importlib.util.spec_from_file_location("scan_release_secrets", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolves the module by name
    spec.loader.exec_module(module)
    return module


def test_scanner_reports_location_and_pattern_only(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    scanner = _load_scanner()
    # Assembled at runtime so this test file itself never matches the scanner.
    line = "pass" + "word = " + chr(34) + MARKER + chr(34) + "\n"
    (tmp_path / "leak.txt").write_text(line, encoding="utf-8")
    hits = scanner.scan(str(tmp_path))
    assert hits == [("leak.txt", 1, "password-assignment")]
    assert scanner.main(["--root", str(tmp_path)]) == 1
    out = capsys.readouterr().out
    assert "leak.txt:1:password-assignment" in out
    assert MARKER not in out and "word =" not in out


def _secret_line() -> str:
    return "pass" + "word = " + chr(34) + MARKER + chr(34) + "\n"


def test_scanner_fallback_walk_skips_scratch_dirs(tmp_path: Path, monkeypatch: Any) -> None:
    scanner = _load_scanner()
    monkeypatch.setattr(scanner, "_in_git_checkout", lambda root: False)
    for rel in ("temp/probe.txt", "build/out.txt", ".venv/lib.txt", "src/temp/keep.txt"):
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(_secret_line(), encoding="utf-8")
    hits = scanner.scan(str(tmp_path))
    assert [hit[0] for hit in hits] == [str(Path("src/temp/keep.txt"))]  # only non-root temp


def test_scanner_git_listing_limits_scan_to_listed_files(tmp_path: Path, monkeypatch: Any) -> None:
    scanner = _load_scanner()
    (tmp_path / "tracked.txt").write_text(_secret_line(), encoding="utf-8")
    (tmp_path / "ignored.txt").write_text(_secret_line(), encoding="utf-8")
    monkeypatch.setattr(scanner, "_in_git_checkout", lambda root: True)
    monkeypatch.setattr(scanner, "_git_files", lambda root: ["tracked.txt", "new_untracked.txt"])
    (tmp_path / "new_untracked.txt").write_text(_secret_line(), encoding="utf-8")
    # untracked-but-not-ignored files ARE scanned (maintainer decision);
    # ignored ones are not listed.
    assert [hit[0] for hit in scanner.scan(str(tmp_path))] == ["new_untracked.txt", "tracked.txt"]


def test_git_files_parsing_and_failures_raise(monkeypatch: Any) -> None:
    import subprocess

    scanner = _load_scanner()
    calls: list[list[str]] = []

    def fake(argv: list[str], **kwargs: Any) -> Any:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout=b"a.txt\0dir/b.txt\0", stderr=b"")

    monkeypatch.setattr(scanner.subprocess, "run", fake)
    assert scanner._git_files("/repo") == ["a.txt", "dir/b.txt"]
    assert "--exclude-standard" in calls[0] and "--others" in calls[0] and "--cached" in calls[0]

    monkeypatch.setattr(
        scanner.subprocess,
        "run",
        lambda argv, **kw: subprocess.CompletedProcess(argv, 128, stdout=b"", stderr=b""),
    )
    with pytest.raises(scanner.GitEnumerationError):
        scanner._git_files("/repo")

    def missing(argv: list[str], **kwargs: Any) -> Any:
        raise FileNotFoundError("git")

    monkeypatch.setattr(scanner.subprocess, "run", missing)
    with pytest.raises(scanner.GitEnumerationError):
        scanner._git_files("/repo")

    def timeout(argv: list[str], **kwargs: Any) -> Any:
        raise subprocess.TimeoutExpired(argv, 60)

    monkeypatch.setattr(scanner.subprocess, "run", timeout)
    with pytest.raises(scanner.GitEnumerationError):
        scanner._git_files("/repo")


def test_git_failure_inside_checkout_fails_clearly_without_walking(
    tmp_path: Path, monkeypatch: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    scanner = _load_scanner()
    (tmp_path / ".git").mkdir()  # a checkout, as far as the filesystem check goes
    (tmp_path / "leak.txt").write_text(_secret_line(), encoding="utf-8")

    def broken(root: str) -> list[str]:
        raise scanner.GitEnumerationError("git file listing failed (exit 128)")

    monkeypatch.setattr(scanner, "_git_files", broken)
    assert scanner._in_git_checkout(str(tmp_path)) is True
    assert scanner.main(["--root", str(tmp_path)]) == 2
    captured = capsys.readouterr()
    assert "git file listing failed" in captured.err
    assert "leak.txt" not in captured.out and "secret-pattern hits" not in captured.out


def test_non_git_export_uses_documented_walk(tmp_path: Path, monkeypatch: Any) -> None:
    scanner = _load_scanner()
    monkeypatch.setattr(scanner, "_in_git_checkout", lambda root: False)

    def forbidden(root: str) -> list[str]:
        raise AssertionError("git must not be consulted outside a checkout")

    monkeypatch.setattr(scanner, "_git_files", forbidden)
    (tmp_path / "leak.txt").write_text(_secret_line(), encoding="utf-8")
    assert [hit[0] for hit in scanner.scan(str(tmp_path))] == ["leak.txt"]


def test_stage_labels_are_a_fixed_vocabulary() -> None:
    assert safe_stage("Direct typing focus") == "direct-typing-focus"
    assert safe_stage("Close Direct typing focus") == "close-direct-typing-focus"
    assert safe_stage("terminal-size-report") == "terminal-size-report"
    # A regex-compatible token that is not a known stage is rejected.
    assert safe_stage("synthetic-secret-9f3c") == "unknown"
    assert safe_stage("terminal-input ") == "unknown"
    assert safe_stage(None) == "unknown"


def test_clipboard_read_failure_logs_category_only(caplog: pytest.LogCaptureFixture) -> None:
    from relaydeck.client.adapters.clipboard import ClipboardReadStatus, read_clipboard

    caplog.set_level(logging.DEBUG)

    class Port:
        async def get_text(self) -> str:
            raise RuntimeError(f"clipboard text {MARKER}")

    result = asyncio.run(read_clipboard(Port()))  # type: ignore[arg-type]
    assert result.status is ClipboardReadStatus.UNAVAILABLE
    text = _flatten(caplog)
    assert "category=RuntimeError" in text
    assert MARKER not in text and "Traceback" not in text
    assert all(record.exc_info is None for record in caplog.records)
