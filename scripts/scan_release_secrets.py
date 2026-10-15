"""Static release scan for committed secrets.

Fails (exit 1) when any tracked text file contains a committed-secret
pattern: private key blocks, provider tokens, password/secret
assignments, credentialed connection strings, RelayDeck token
literals, connector seed values, or canary markers outside
their home in tests/security. Binary files and dependency/build
caches are skipped, never parsed.

Scope (maintainer decision): inside a git checkout the scan covers tracked files
AND untracked-but-not-ignored files (`git ls-files --cached --others
--exclude-standard`), so a secret in a new file is caught BEFORE it is
committed; ignored scratch such as temp/ is never scanned. If git cannot
enumerate files inside a checkout (missing git, timeout, error) the scan
fails clearly (exit 2) instead of silently walking the directory. The
directory-walk fallback exists only for a genuine non-git export (no .git
anywhere above the root) and skips .venv, build and the root temp folder.

Allowlisted on purpose (verified synthetic, see --list notes):
tests/fixtures token literals and short seed placeholders.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass


@dataclass(frozen=True)
class SecretPattern:
    name: str
    regex: str
    description: str
    allow_dirs: tuple[str, ...] = ()


PATTERNS: tuple[SecretPattern, ...] = (
    SecretPattern(
        "private-key-block",
        r"-----BEGIN (?:[A-Z0-9 ]* )?PRIVATE KEY-----",
        "PEM private key block (any type). No allowlist: fixtures use raw bytes.",
    ),
    SecretPattern(
        "openssh-private-key",
        "-----BEGIN " + "OPENSSH PRIVATE KEY-----",
        "OpenSSH private key block.",
    ),
    SecretPattern(
        "aws-access-key",
        r"AKIA[0-9A-Z]{16}",
        "AWS access key id.",
    ),
    SecretPattern(
        "github-token",
        r"gh[pousr]_[A-Za-z0-9]{36,}",
        "GitHub token.",
    ),
    SecretPattern(
        "slack-token",
        r"xox[baprs]-[A-Za-z0-9-]{10,}",
        "Slack token.",
    ),
    SecretPattern(
        "password-assignment",
        r"(?i)(?:password|passwd|pwd)\s*[:=]\s*['\"][^'\"]{4,}['\"]",
        "Quoted password assignment. Prose ('password for x') does not match.",
        allow_dirs=("tests/security",),
    ),
    SecretPattern(
        "secret-assignment",
        r"(?i)(?:api[_-]?key|api[_-]?secret|client[_-]?secret|secret[_-]?key)"
        r"\s*[:=]\s*['\"][A-Za-z0-9_\-+/=]{16,}['\"]",
        "Quoted API/secret-key assignment with a plausible value.",
        allow_dirs=("tests/security",),
    ),
    SecretPattern(
        "connection-string",
        r"(?i)(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqp)://[^/\s]*:[^/\s]*@",
        "Credentialed database/broker connection string.",
    ),
    SecretPattern(
        "relaydeck-token-literal",
        r"(?:rdh_|rdt_)[A-Za-z0-9_\-]{20,}",
        "RelayDeck host/terminal token literal.",
        allow_dirs=("tests/fixtures",),
    ),
    SecretPattern(
        "connector-seed-value",
        r"['\"]seed_b64['\"]\s*:\s*['\"][A-Za-z0-9+/=]{40,}['\"]",
        "Connector seed value (real seeds are 44 base64 chars).",
        allow_dirs=("tests/fixtures",),
    ),
    SecretPattern(
        "bearer-literal",
        r"Bearer [A-Za-z0-9_\-\.~+/=]{24,}",
        "Long literal Bearer credential (format strings do not match).",
        allow_dirs=("tests",),
    ),
    SecretPattern(
        "task-024-canary",
        r"canary-(?:pw|draft|clipboard|push|token|event)-[0-9a-f]{8}",
        "Canary marker outside its home in tests/security.",
        allow_dirs=("tests/security",),
    ),
)

SKIP_DIRS = {
    ".git",
    ".venv",
    ".mypy_cache",
    ".pytest_cache",
    "__pycache__",
    "node_modules",
}
SKIP_SUFFIXES = (".pyc", ".pyo", ".docx", ".zip")
SKIP_NAMES = {"uv.lock"}


# Fallback walk only (outside a git checkout): scratch/build output is never
# scanned. `temp` is the AI scratch folder at the repo root; `build` anywhere.
FALLBACK_SKIP_DIRS = SKIP_DIRS | {"build"}
FALLBACK_SKIP_ROOT_DIRS = {"temp"}


class GitEnumerationError(RuntimeError):
    """git could not list files inside a checkout (fail the scan, never walk)."""


def _in_git_checkout(root: str) -> bool:
    """True when `root` or a parent has a .git entry (filesystem check only)."""
    current = os.path.abspath(root)
    while True:
        if os.path.exists(os.path.join(current, ".git")):
            return True
        parent = os.path.dirname(current)
        if parent == current:
            return False
        current = parent


def _git_files(root: str) -> list[str]:
    """Tracked plus untracked-but-not-ignored files (honours .gitignore).

    Raises GitEnumerationError when git is unavailable, times out or fails."""
    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["git", "-C", root, "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            capture_output=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise GitEnumerationError(f"git file listing unavailable ({type(exc).__name__})") from None
    if proc.returncode != 0:
        raise GitEnumerationError(f"git file listing failed (exit {proc.returncode})")
    names = proc.stdout.decode("utf-8", "surrogateescape").split("\0")
    return [name for name in names if name]


def _skip_name(name: str) -> bool:
    return name in SKIP_NAMES or name.endswith(SKIP_SUFFIXES)


def _iter_files(root: str) -> Iterator[str]:
    if _in_git_checkout(root):
        for rel in sorted(set(_git_files(root))):
            if not _skip_name(os.path.basename(rel)):
                yield os.path.join(root, rel)
        return
    # Genuine non-git export only (see module docstring).
    for dirpath, dirnames, filenames in os.walk(root):
        at_root = os.path.abspath(dirpath) == os.path.abspath(root)
        dirnames[:] = [
            d
            for d in dirnames
            if d not in FALLBACK_SKIP_DIRS
            and ".egg-info" not in d
            and not (at_root and d in FALLBACK_SKIP_ROOT_DIRS)
        ]
        for name in sorted(filenames):
            if not _skip_name(name):
                yield os.path.join(dirpath, name)


def scan(root: str) -> list[tuple[str, int, str]]:
    """Locations and pattern names only; matched text is never kept or printed."""
    compiled = [(p, re.compile(p.regex)) for p in PATTERNS]
    hits: list[tuple[str, int, str]] = []
    for path in _iter_files(root):
        try:
            with open(path, encoding="utf-8") as handle:
                text = handle.read()
        except (UnicodeDecodeError, OSError):
            continue  # binary or unreadable: skipped, never parsed
        rel = os.path.relpath(path, root)
        for pattern, regex in compiled:
            if any(rel == d or rel.startswith(d + os.sep) for d in pattern.allow_dirs):
                continue
            for lineno, line in enumerate(text.splitlines(), 1):
                if regex.search(line) is not None:
                    hits.append((rel, lineno, pattern.name))
    return hits


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Scan the repo for committed secrets.")
    parser.add_argument("--list", action="store_true", help="list patterns")
    parser.add_argument("--root", default="", help="repo root (default: script parent)")
    args = parser.parse_args(argv)
    if args.list:
        for pattern in PATTERNS:
            allowed = f" [allow: {', '.join(pattern.allow_dirs)}]" if pattern.allow_dirs else ""
            print(f"{pattern.name}: {pattern.description}{allowed}")
        return 0
    root = args.root or os.path.join(os.path.dirname(__file__), "..")
    try:
        hits = scan(os.path.abspath(root))
    except GitEnumerationError as exc:
        print(f"error: {exc}; refusing to scan without the git file list", file=sys.stderr)
        return 2
    for rel, lineno, name in hits:
        print(f"{rel}:{lineno}:{name}")
    print(f"{len(hits)} secret-pattern hits")
    return 1 if hits else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
