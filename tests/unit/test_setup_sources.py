"""consent-gated usage sources in `relaydeck setup`.

Every test runs in a temporary home (HOME is set to it), with a fake detection report and a scripted
confirm function. The real companion.toml and the real detection are never touched.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from relaydeck import cli
from relaydeck.companion import config_edit, setup
from relaydeck.companion.config import load_config
from relaydeck.companion.detect import DetectReport
from tests.support.companion.setup_sources import (
    CUM_CLI,
    MESSAGE_READER,
    MSG_CLI,
    UNSUPPORTED_CLI,
    Answers,
    setup_home,
)

MSG = MSG_CLI.id
CUM = CUM_CLI.id


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    fake = tmp_path / "home"
    fake.mkdir(mode=0o700)
    monkeypatch.setenv("HOME", str(fake))
    return fake


def _offer(config: Path, report: DetectReport, confirm: Answers) -> tuple[list[str], int]:
    return setup.offer_sources(config, confirm=confirm, detect_fn=lambda _path: report)


# -- consent, default and the preview ----------------------------------------


def test_consent_is_asked_per_cli_and_only_the_approved_one_is_written(home: Path) -> None:
    config, report = setup_home(home)
    confirm = Answers(True, False)
    lines, code = _offer(config, report, confirm)
    assert code == 0
    assert confirm.asked == [
        f"Add {MSG_CLI.display_name}? [y/N] ",
        f"Add {CUM_CLI.display_name}? [y/N] ",
    ]
    text = config.read_text(encoding="utf-8")
    assert f'kind = "{MSG}"' in text and f'kind = "{CUM}"' not in text
    assert any("-> approved" in line for line in lines)
    assert any("-> not added" in line for line in lines)


def test_declining_every_cli_writes_nothing(home: Path) -> None:
    config, report = setup_home(home)
    before = config.read_bytes()
    _lines, code = _offer(config, report, Answers(False, False))
    assert code == 0
    assert config.read_bytes() == before
    assert not list(home.glob("companion.toml.bak-*"))


def test_the_default_answer_is_no(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("builtins.input", lambda _prompt: "")
    assert cli._confirm("Add a tool? [y/N] ") is False
    monkeypatch.setattr("builtins.input", lambda _prompt: "y")
    assert cli._confirm("Add a tool? [y/N] ") is True


def test_the_preview_shows_kind_format_root_history_and_token_only_reads(home: Path) -> None:
    config, report = setup_home(home)
    lines, _code = _offer(config, report, Answers(False, False))
    text = "\n".join(lines)
    for expected in (
        f"kind:    {MSG}",
        f"format:  {MESSAGE_READER}",
        f"root:    {MSG_CLI.default_roots[0]}",
        "history: up to 90 days",
        "token counts and timestamps only",
    ):
        assert expected in text


def test_the_preview_never_shows_an_absolute_home_path(home: Path) -> None:
    config, report = setup_home(home)
    lines, _code = _offer(config, report, Answers(False, False))
    assert str(home) not in "\n".join(lines)


def test_unsupported_clis_are_listed_and_never_written(home: Path) -> None:
    config, report = setup_home(home)
    lines, _code = _offer(config, report, Answers(True, True))
    assert any(
        "Not supported yet" in line and UNSUPPORTED_CLI.display_name in line for line in lines
    )
    assert f'kind = "{UNSUPPORTED_CLI.id}"' not in config.read_text(encoding="utf-8")


# -- the write: comments, validity, concurrency, interruption ----------------------------------


def test_existing_comments_and_text_are_preserved(home: Path) -> None:
    config, report = setup_home(home)
    original = config.read_text(encoding="utf-8")
    _offer(config, report, Answers(True, False))
    assert config.read_text(encoding="utf-8").startswith(original)


def test_the_written_file_parses_with_the_existing_loader(home: Path) -> None:
    config, report = setup_home(home)
    _offer(config, report, Answers(True, True))
    loaded = load_config(config)
    assert loaded.usage is not None
    assert [source.kind for source in loaded.usage.sources] == [MSG, CUM]
    assert loaded.agent_kinds == ()  # consent to read usage never starts an agent


def test_a_concurrent_edit_is_detected_and_the_write_aborts(home: Path) -> None:
    config, report = setup_home(home)

    class Editor(Answers):
        def __call__(self, prompt: str) -> bool:
            super().__call__(prompt)
            # Someone edits the file while the question is open.
            config.write_text(
                config.read_text(encoding="utf-8") + "# edited meanwhile\n", encoding="utf-8"
            )
            return True

    edited = config.read_text(encoding="utf-8")
    lines, code = _offer(config, report, Editor())
    assert code == 1
    assert any("changed while setup was running" in line for line in lines)
    assert config.read_text(encoding="utf-8") != edited or "# edited meanwhile" in config.read_text(
        encoding="utf-8"
    )
    assert f'kind = "{MSG}"' not in config.read_text(encoding="utf-8")


def test_an_interrupted_write_leaves_the_original_intact(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, report = setup_home(home)
    before = config.read_bytes()

    def interrupted(path: Path, expected_text: str, text: str, **kwargs: object) -> None:
        raise OSError("interrupted before the replace")

    monkeypatch.setattr(config_edit, "replace_if_match", interrupted)
    lines, code = _offer(config, report, Answers(True, False))
    assert code == 1
    assert any("could not be replaced" in line for line in lines)
    assert config.read_bytes() == before
    assert not list(home.glob(".companion.toml.*"))


def test_a_successful_write_keeps_a_private_backup_and_a_secret_free_receipt(home: Path) -> None:
    config, report = setup_home(home)
    original = config.read_bytes()
    _offer(config, report, Answers(True, False))
    backups = list(home.glob("companion.toml.bak-*"))
    assert len(backups) == 1 and backups[0].read_bytes() == original
    assert (backups[0].stat().st_mode & 0o077) == 0
    receipts = list((home / "setup-receipts").glob("sources-*-add.json"))
    assert len(receipts) == 1
    body = receipts[0].read_text(encoding="utf-8")
    assert f'"kind": "{MSG}"' in body and str(home) not in body


# -- undo and repeat runs ----------------------------------------


def test_undo_removes_only_unchanged_managed_entries(home: Path) -> None:
    config, report = setup_home(home)
    _offer(config, report, Answers(True, True))
    edited = config.read_text(encoding="utf-8").replace(
        f'display_name = "{CUM_CLI.display_name}"',
        f'display_name = "My {CUM_CLI.display_name}"',
    )
    config.write_text(edited, encoding="utf-8")
    lines, code = setup.undo_sources(config)
    assert code == 0
    text = config.read_text(encoding="utf-8")
    assert f'kind = "{MSG}"' not in text  # unchanged, removed
    assert f'display_name = "My {CUM_CLI.display_name}"' in text  # changed by the owner, kept
    assert any("changed since setup" in line and CUM in line for line in lines)
    assert "# The owner's notes stay exactly as they are." in text


def test_an_idempotent_rerun_adds_nothing_and_does_not_duplicate(home: Path) -> None:
    config, report = setup_home(home)
    _offer(config, report, Answers(True, False))
    once = config.read_text(encoding="utf-8")
    confirm = Answers(False)
    _lines, code = _offer(config, report, confirm)
    assert code == 0
    assert config.read_text(encoding="utf-8") == once
    assert confirm.asked == [
        f"Add {CUM_CLI.display_name}? [y/N] "
    ]  # the first CLI is already a source: not offered again
    assert config.read_text(encoding="utf-8").count(f'kind = "{MSG}"') == 1


def test_yes_is_not_consent_to_add_sources(home: Path) -> None:
    """The source step asks for each CLI whatever --yes says: it has no such parameter at all."""
    config, report = setup_home(home)
    confirm = Answers(False, False)
    _offer(config, report, confirm)
    assert len(confirm.asked) == 2
    assert f'kind = "{MSG}"' not in config.read_text(encoding="utf-8")


# -- without a terminal: zero writes ----------------------------------------


def test_setup_without_a_terminal_writes_nothing_and_lists_the_pending_choices(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config, report = setup_home(home)
    before = {path: path.read_bytes() for path in home.rglob("*") if path.is_file()}
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    monkeypatch.setattr(cli, "detect_report", lambda _path: report)
    assert cli.main(["--config", str(config), "setup"]) == 1
    after = {path: path.read_bytes() for path in home.rglob("*") if path.is_file()}
    assert after == before
    out = capsys.readouterr().out
    assert "Pending source choices" in out and MSG_CLI.display_name in out
    assert "Nothing was written." in out


def test_yes_without_a_terminal_still_writes_nothing(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config, report = setup_home(home)
    before = config.read_bytes()
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    monkeypatch.setattr(cli, "detect_report", lambda _path: report)
    assert cli.main(["--config", str(config), "setup", "--yes"]) == 1
    assert config.read_bytes() == before


def test_undo_sources_flag_removes_its_own_entries(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, report = setup_home(home)
    _offer(config, report, Answers(True, False))
    assert cli.main(["--config", str(config), "setup", "--undo-sources"]) == 0
    assert f'kind = "{MSG}"' not in config.read_text(encoding="utf-8")
