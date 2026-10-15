"""no agent product or developer-assistant names in shipped source.

Every file under src/relaydeck (Python, TOML, Markdown, JSON) is scanned, case-insensitively
and word-bounded, so "solid" or "console" never match. Agent kinds come from the owner's
config at run time, not from code.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2] / "src" / "relaydeck"
SUFFIXES = {".py", ".toml", ".md", ".json"}
# exactly one file may name CLI products: the approved catalog (versioned data).
# The AI-assistant role names stay banned even there (see ROLE_NAMES below).
CATALOG_EXCEPTION = ROOT / "companion" / "data" / "cli_catalog.toml"
# Split so this file itself never contains the banned words (guard).
ROLE_NAMES = "|".join(["as" + "tra", "no" + "va", "s" + "ol", "son" + "net", "op" + "us"])
_ORDINARY_WORDS = {
    "crush",
    "cursor",
    "hermes",
    "droid",
    "letta",
    "devin",
    "grok",
    "cline",
    "copilot",
}
_CATALOG_IDS = [
    entry["id"] for entry in tomllib.loads(CATALOG_EXCEPTION.read_text(encoding="utf-8"))["cli"]
]
# Product names come from the catalog itself; short or ordinary-word ids would match plain prose.
PRODUCT_IDS = sorted(i for i in _CATALOG_IDS if len(i) >= 5 and i not in _ORDINARY_WORDS)
PRODUCT_NAMES = "|".join(re.escape(i) for i in PRODUCT_IDS)
BANNED = re.compile(
    rf"\b(?:{PRODUCT_NAMES}|{ROLE_NAMES})\b",
    re.IGNORECASE,
)
ROLES_ONLY = re.compile(rf"\b(?:{ROLE_NAMES})\b", re.IGNORECASE)


def _shipped_files() -> list[Path]:
    return sorted(
        path
        for path in ROOT.rglob("*")
        if path.is_file() and path.suffix in SUFFIXES and "__pycache__" not in path.parts
    )


def test_scan_covers_the_shipped_source() -> None:
    files = _shipped_files()
    assert any(path.name == "theme.py" for path in files)
    assert any(path.name == "companion.template.toml" for path in files)


def _hits(path: Path, text: str) -> list[str]:
    pattern = ROLES_ONLY if path == CATALOG_EXCEPTION else BANNED
    return [
        f"{path.relative_to(ROOT)}:{number}"
        for number, line in enumerate(text.splitlines(), 1)
        if pattern.search(line)
    ]


def test_no_agent_product_names_in_shipped_source() -> None:
    hits: list[str] = []
    for path in _shipped_files():
        hits += _hits(path, path.read_text(encoding="utf-8", errors="replace"))
    assert hits == []


def test_the_catalog_exception_is_exactly_one_file_and_keeps_the_role_names_banned() -> None:
    """The exception does not leak: the catalog may name products, but a role name fails there, and
    any other file that names a product still fails."""
    assert CATALOG_EXCEPTION in _shipped_files()
    product = PRODUCT_IDS[0]
    assert _hits(CATALOG_EXCEPTION, f'id = "{product}"') == []
    assert _hits(CATALOG_EXCEPTION, 'id = "as' + 'tra"') != []
    assert _hits(ROOT / "companion" / "other.toml", f'id = "{product}"') != []
    assert _hits(ROOT / "companion" / "data" / "other.toml", f'id = "{product}"') != []
    assert _hits(ROOT / "companion" / "cli.py", "# op" + "us") != []
    assert [path for path in _shipped_files() if path.name == "cli_catalog.toml"] == [
        CATALOG_EXCEPTION
    ]
