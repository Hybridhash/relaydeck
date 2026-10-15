"""Reader roots, the order of each preview and its question, and the wording.

Synthetic layouts only, under a temporary HOME. The readers are the shipped ones (read-only use).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from relaydeck.companion import detect, setup
from relaydeck.companion.detect import DetectReport
from relaydeck.usage.contracts import Budget, Coverage, Cursor
from relaydeck.usage.readers import READERS
from tests.support.companion.setup_sources import (
    CUM_CLI,
    MESSAGE_READER,
    MSG_CLI,
    UNSUPPORTED_CLI,
    Answers,
    setup_home,
    under_home,
)

TIER_A = sorted(entry.id for entry in detect.load_catalog().entries if entry.reader_root)


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    fake = tmp_path / "home"
    fake.mkdir(mode=0o700)
    monkeypatch.setenv("HOME", str(fake))
    return fake


def test_every_tier_a_entry_has_a_shipped_reader_and_a_root_over_its_log_location() -> None:
    assert TIER_A
    for entry in detect.load_catalog().entries:
        if not entry.reader_root:
            continue
        assert entry.reader_format in READERS, entry.id
        log = entry.default_roots[0]
        assert log == entry.reader_root or log.startswith(entry.reader_root + "/"), entry.id


def _layout(reader_format: str, root: Path) -> None:
    """A minimal synthetic log layout for each tier-A reader, under its reader root."""
    if reader_format == "jsonl-message-usage-v1":
        session = root / "-synthetic-project" / "session.jsonl"
        line = {
            "type": "assistant",
            "timestamp": "2026-10-12T10:00:00Z",
            "message": {
                "id": "m1",
                "model": "synthetic",
                "usage": {"input_tokens": 3, "output_tokens": 2},
            },
        }
    elif reader_format == "jsonl-cumulative-token-count-v1":
        session = root / "sessions" / "2026" / "10" / "12" / "rollout-synthetic.jsonl"
        line = {
            "timestamp": "2026-10-12T10:00:00Z",
            "type": "event_msg",
            "payload": {"type": "token_count", "info": {"total_token_usage": {"input_tokens": 3}}},
        }
    elif reader_format == "jsonl-event-usage-v1":
        session = root / "sessions" / "2026" / "10" / "12" / "synthetic" / "session.jsonl"
        line = {
            "recorded_at": "2026-10-12T10:00:00Z",
            "type": "model_completed",
            "model_id": "synthetic",
            "usage": {"input_tokens": 3, "output_tokens": 2},
        }
    else:
        session = root / "sessions" / "cli" / "synthetic.json"
        line = None
    session.parent.mkdir(parents=True, exist_ok=True)
    if line is None:
        session.write_text(json.dumps({"turns": []}), encoding="utf-8")
    else:
        session.write_text(json.dumps(line) + "\n", encoding="utf-8")


@pytest.mark.parametrize("cli_id", TIER_A)
def test_every_tier_a_reader_root_is_readable_by_its_reader(home: Path, cli_id: str) -> None:
    entry = next(item for item in detect.load_catalog().entries if item.id == cli_id)
    root = detect._reader_path(entry, str(home))
    assert root is not None
    _layout(entry.reader_format or "", Path(root))
    reader = READERS[entry.reader_format or ""]
    batch = reader.read(Path(root), Cursor(), Budget())
    assert batch.coverage not in (Coverage.UNAVAILABLE,), (cli_id, batch.coverage)


def test_the_preview_and_its_question_come_in_that_order(home: Path) -> None:
    config, report = setup_home(home)
    events: list[str] = []

    class Recorder(Answers):
        def __call__(self, prompt: str) -> bool:
            events.append(f"QUESTION {prompt.strip()}")
            return super().__call__(prompt)

    setup.offer_sources(
        config,
        confirm=Recorder(True, False),
        detect_fn=lambda _path: report,
        emit=events.append,
    )
    questions = [index for index, event in enumerate(events) if event.startswith("QUESTION")]
    assert len(questions) == 2
    first_preview = events.index(f"{MSG_CLI.display_name} ({MSG_CLI.id})")
    second_preview = events.index(f"{CUM_CLI.display_name} ({CUM_CLI.id})")
    assert first_preview < questions[0] < second_preview < questions[1]
    assert events[questions[0] - 1].startswith("  reads:") or events[questions[0] - 1].startswith(
        "           "
    )
    assert events[questions[1] - 1].startswith("  reads:") or events[questions[1] - 1].startswith(
        "           "
    )


def test_the_reads_line_has_the_agreed_wording(home: Path) -> None:
    config, report = setup_home(home)
    lines, _code = setup.offer_sources(
        config, confirm=Answers(False, False), detect_fn=lambda _p: report
    )
    text = "\n".join(lines)
    assert "token counts and timestamps only; prompts and file contents are never" in text
    assert "stored or sent" in text
    assert "never prompts or file contents to the server" not in text


def test_the_written_source_uses_the_reader_root(home: Path) -> None:
    config, report = setup_home(home)
    setup.offer_sources(config, confirm=Answers(True, False), detect_fn=lambda _p: report)
    written = config.read_text(encoding="utf-8")
    assert f'root = "{under_home(home, MSG_CLI)}"' in written


def test_a_configured_source_on_the_reader_root_counts_as_configured(home: Path) -> None:
    """A source named differently but pointing at the reader root still marks the CLI configured."""
    under_home(home, MSG_CLI).mkdir(parents=True)
    (home / "state").mkdir(mode=0o700, exist_ok=True)
    config = home / "companion.toml"
    config.write_text(
        "\n".join(
            [
                'advertised_origin = "https://mac.example-tailnet.ts.net"',
                'host_id = "home-mac"',
                f'state_dir = "{home / "state"}"',
                f'cookie_secret_file = "{home / "cookie.secret"}"',
                f'cursor_secret_file = "{home / "cursor.secret"}"',
                f'connector_credentials = "{home / "connector.json"}"',
                f'herdr_socket = "{home / "herdr.sock"}"',
                "agent_kinds = []",
                "[usage]",
                'timezone = "Etc/UTC"',
                "[[usage.sources]]",
                f'kind = "{MSG_CLI.id}-logs"',
                f'format = "{MESSAGE_READER}"',
                f'root = "{under_home(home, MSG_CLI)}"',
                f'display_name = "{MSG_CLI.display_name} logs"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    config.chmod(0o600)
    report = detect.run_detect(
        detect.load_catalog(),
        home=str(home),
        environ={"PATH": ""},
        config_path=config,
        herdr_reader=lambda: None,
    )
    row = next(item for item in report.rows if item.id == MSG_CLI.id)
    assert row.configured is True
    assert row.reader_root == str(under_home(home, MSG_CLI))


def test_detection_reports_no_reader_root_for_a_cli_without_a_reader(home: Path) -> None:
    report: DetectReport = detect.run_detect(
        detect.load_catalog(),
        home=str(home),
        environ={"PATH": ""},
        config_path=None,
        herdr_reader=lambda: None,
    )
    assert next(item for item in report.rows if item.id == UNSUPPORTED_CLI.id).reader_root is None
