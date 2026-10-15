"""no AI agent or role names in tracked files.

Every tracked text file outside temp/ is scanned, case-insensitively and
word-bounded, so "solid" or "console" never match. The pattern is built from
split strings so this file itself stays clean and needs no exclusion.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SUFFIXES = {
    ".py",
    ".md",
    ".toml",
    ".json",
    ".sh",
    ".js",
    ".cjs",
    ".html",
    ".txt",
    ".yml",
    ".yaml",
}
SCOPES = ("src", "tests", "scripts", "docs", "plugin", "examples")
TOP_FILES = ("README.md", "pyproject.toml")
SKIP_DIRS = {"__pycache__", "node_modules", "build", "dist"}
# Split so this file itself never contains the banned words.
WORDS = ("as" + "tra", "no" + "va", "s" + "ol", "son" + "net", "op" + "us", "ch" + "ief")
BANNED = re.compile(r"\b(?:" + "|".join(WORDS) + r")\b", re.IGNORECASE)


def _tracked_files() -> list[Path]:
    found: list[Path] = []
    for scope in SCOPES:
        root = ROOT / scope
        if not root.is_dir():
            continue
        found += [
            path
            for path in root.rglob("*")
            if path.is_file() and path.suffix in SUFFIXES and not (SKIP_DIRS & set(path.parts))
        ]
    for name in TOP_FILES:
        path = ROOT / name
        if path.is_file():
            found.append(path)
    return sorted(found)


def test_scan_covers_the_tracked_roots() -> None:
    files = _tracked_files()
    assert Path(__file__).resolve() in files
    assert ROOT / "README.md" in files
    assert ROOT / "pyproject.toml" in files
    assert any(path.parent.name == "relaydeck" and path.suffix == ".py" for path in files)


def _hits(path: Path, text: str) -> list[str]:
    return [
        f"{path.relative_to(ROOT)}:{number}"
        for number, line in enumerate(text.splitlines(), 1)
        if BANNED.search(line)
    ]


def test_no_ai_role_names_in_tracked_files() -> None:
    hits: list[str] = []
    for path in _tracked_files():
        hits += _hits(path, path.read_text(encoding="utf-8", errors="replace"))
    assert hits == []


def test_the_detector_matches_whole_words_case_insensitively() -> None:
    assert len(WORDS) == 6 and BANNED.pattern.count("|") == 5
    assert _hits(ROOT / "src" / "x.py", "the " + WORDS[0] + " review") != []
    assert _hits(ROOT / "src" / "x.py", "THE " + WORDS[5].upper() + " RUNS") != []
    assert _hits(ROOT / "src" / "x.py", "solid console opencode") == []
