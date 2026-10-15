"""Shared helpers for the usage-source tests: scripted answers and a fake home."""

from __future__ import annotations

from pathlib import Path

from relaydeck.companion.detect import CatalogEntry, DetectReport, DetectRow, load_catalog

ORIGIN = "https://mac.example-tailnet.ts.net"

MESSAGE_READER = "jsonl-message-usage-v1"
CUMULATIVE_READER = "jsonl-cumulative-token-count-v1"


def catalog_by_reader(reader_format: str) -> CatalogEntry:
    """The first catalog entry read by one reader format."""
    return next(e for e in load_catalog().entries if e.reader_format == reader_format)


def catalog_unsupported() -> CatalogEntry:
    """The first catalog entry with a log location but no reader yet."""
    return next(e for e in load_catalog().entries if e.reader_format is None and e.default_roots)


def under_home(home: Path, entry: CatalogEntry, index: int = 0) -> Path:
    """One of an entry's default log roots inside a fake home."""
    return home / entry.default_roots[index].removeprefix("~/")


def state_dir_of(home: Path, entry: CatalogEntry) -> Path:
    """The CLI's own directory: the first path segment of its default root."""
    return home / entry.default_roots[0].removeprefix("~/").split("/")[0]


MSG_CLI = catalog_by_reader(MESSAGE_READER)
CUM_CLI = catalog_by_reader(CUMULATIVE_READER)
UNSUPPORTED_CLI = catalog_unsupported()


class Answers:
    """A confirm function with scripted answers that records every question asked."""

    def __init__(self, *answers: bool) -> None:
        self.answers = list(answers)
        self.asked: list[str] = []

    def __call__(self, prompt: str) -> bool:
        self.asked.append(prompt)
        return self.answers.pop(0) if self.answers else False


def row(
    kind: str,
    name: str,
    *,
    next_step: str,
    roots: tuple[str, ...] = (),
    adapter: str = "not yet",
    binary: str | None = "/fake/bin/tool",
) -> DetectRow:
    return DetectRow(
        id=kind,
        display_name=name,
        tier="A",
        herdr_detects=True,
        herdr_hook="current v1",
        binary=binary,
        adapter=adapter,
        configured=next_step == "already configured",
        roots_present=roots,
        next_step=next_step,
        reader_root=roots[0] if roots and adapter != "not yet" else None,
    )


def setup_home(home: Path) -> tuple[Path, DetectReport]:
    """A fake home with two installable CLIs and one unsupported CLI."""
    for entry in (MSG_CLI, CUM_CLI, UNSUPPORTED_CLI):
        under_home(home, entry).mkdir(parents=True)
    (home / "state").mkdir(mode=0o700, exist_ok=True)
    config = home / "companion.toml"
    config.write_text(
        "\n".join(
            [
                "# The owner's notes stay exactly as they are.",
                f'advertised_origin = "{ORIGIN}"',
                'host_id = "home-mac"',
                f'state_dir = "{home / "state"}"',
                f'cookie_secret_file = "{home / "cookie.secret"}"',
                f'cursor_secret_file = "{home / "cursor.secret"}"',
                f'connector_credentials = "{home / "connector.json"}"',
                f'herdr_socket = "{home / "herdr.sock"}"',
                "agent_kinds = []",
                "",
            ]
        ),
        encoding="utf-8",
    )
    config.chmod(0o600)
    report = DetectReport(
        rows=(
            row(
                MSG_CLI.id,
                MSG_CLI.display_name,
                next_step="can be added",
                roots=(str(under_home(home, MSG_CLI)),),
                adapter=MESSAGE_READER,
            ),
            row(
                CUM_CLI.id,
                CUM_CLI.display_name,
                next_step="can be added",
                roots=(str(under_home(home, CUM_CLI)),),
                adapter=CUMULATIVE_READER,
            ),
            row(
                UNSUPPORTED_CLI.id,
                UNSUPPORTED_CLI.display_name,
                next_step="needs an adapter (not supported yet)",
                roots=(str(under_home(home, UNSUPPORTED_CLI)),),
            ),
        ),
        herdr_status="ok",
        config_status="ok",
    )
    return config, report
