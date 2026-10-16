"""No internal workflow labels, review references or work-record dates in tracked text files.

Comments, docstrings and docs explain behavior in plain words. Task, review and slice labels
mean nothing to a reader of the public tree, and dates in comments go stale.
"""

from __future__ import annotations

import re
import subprocess
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SKIP_PREFIXES = ("extensions/terminal/third_party/",)
SKIP_NAMES = {"package-lock.json", "uv.lock", "pubspec.lock", "poetry.lock", "yarn.lock"}
SKIP_SUFFIXES = {".lock", ".json"}
SELF = Path(__file__).resolve().relative_to(ROOT).as_posix()

LABELS = re.compile(
    r"\bRD-[A-Z]*[0-9]+[A-Z]?\b"
    r"|\bR[0-9]-T\b"
    r"|\bR[0-9][A-Z]\b"
    r"|\b[Aa]ddendum [0-9]+"
    r"|\b[Bb]ullets? [0-9]+"
    r"|\bReview [0-9]+"
    r"|\bP[0-3]#[0-9]"
    r"|\bP[0-9]-[A-Z]\b"
    r"|\b(?:TASK|GATE|DES|ADR|UH|CP|SLICE)-[A-Z]*[0-9]+[A-Z]?\b"
    # "slice" is an ordinary word (array slice, state slice); only the work-item sense is banned.
    r"|\b(?:this|later|dispatched|contract) slices?\b"
    r"|\bslices add\b"
    r"|\bslice [A-Z]\b"
    # Work-instruction and decision-record wording. Exact phrases only: "owner" is also the
    # machine's operator and a lease owner, and "brief" is an ordinary word.
    r"|\b(?:the|paste|usage) brief(?:'s)?\b(?! (?:summary|pause))"
    r"|\bbrief (?:items?|point) [0-9]"
    r"|\b[Oo]wner[- ](?:mock|design|review|report(?:ed)?|direction|clarified|suggests)\b"
    r"|\b[Oo]wner decision:"
    r"|\b[Oo]wner confirms\b"
    r"|\bowner-approved (?:34px|catalog|Firebase)\b"
    r"|\bThe owner approved the\b"
    # Underscore-aware edges: "_" separates, so _X-1 and ids ending in _ still match.
    # The optional letter-led tail catches variants such as SPLIT-TYPE-1c.
    r"|(?<![A-Za-z0-9])[A-Z]+(?:-[A-Z]+)*-[0-9]+(?:[A-Za-z][0-9A-Za-z]*)?(?![A-Za-z0-9])"
    r"|(?<![A-Za-z0-9])round [0-9]+(?![A-Za-z0-9])"
    r"|(?<![A-Za-z0-9])TASK [A-Z0-9]+(?![A-Za-z0-9])"
    r"|(?<![A-Za-z0-9])[Rr]eview[0-9]+(?![A-Za-z0-9])"
    # Lowercase and contextual forms: numbered task ids, milestone suffixes, round-prefixed
    # helper names and work-record narration.
    r"|(?<![A-Za-z0-9])[Tt]ask[-_][0-9]+(?![A-Za-z0-9])"
    r"|\([Mm][0-9]\)"
    r"|(?:covers|\+|--|#) [Mm][0-9]\b"
    r"|(?<![A-Za-z0-9])ev-m[0-9]-"
    r"|(?<![A-Za-z0-9])_?r[0-9]_[a-z]"
    r"|\b[Pp]er the (?:assignment|brief|task)\b"
    r"|\(verified (?:by (?:search|execution)|live)\)"
    r"|\bimports verified by execution\b"
    r"|\bscratch launcher\b"
    r"|\bmaintainers' working copy\b"
    # Short round/check tags only where a label can start: line start or after
    # whitespace or an opening quote/paren/bracket. `code=C1` and `1.4.6-R6000.1`
    # stay out because `=` and `-` never start a label.
    r"|(?:^|[\s\"'#(\[])(?P<tok>[RC][0-9]+)(?![A-Za-z0-9])"
)
COMMENT_DATE = re.compile(r"#.*\b20[0-9]{2}-[01][0-9]-[0-3][0-9]\b")

# Match text -> reason; public identifiers the label pattern also matches.
LABEL_EXCEPTIONS = {
    "UTF-8": "character encoding",
    "UTF-16": "character encoding",
    "SHA-256": "hash algorithm",
    "ISO-8601": "date format",
    "AES-256": "cipher",
    "P-256": "signature curve",
    "UAX-29": "Unicode annex",
    "BSD-2": "license identifier",
    "BSD-3": "license identifier",
    "LGPL-2": "license identifier",
    "MIT-0": "license identifier",
    "MPL-2": "license identifier",
    "OFL-1": "license identifier",
    "PSF-2": "license identifier",
    "CANARY-APPROVE-YES-13579": "synthetic disclosure canary; value must stay byte-stable",
}
LABEL_EXCEPTION_PATTERNS = ((re.compile(r"RFC-[0-9]+"), "IETF RFC identifier"),)

# (path, text on the line) -> reason; genuine non-label matches only.
ALLOWED: dict[tuple[str, str], str] = {
    (
        "tests/unit/test_usage_redesign_selectors.py",
        "# 2026-10-05 12:00 UTC",
    ): "test data: the instant that renders as 'Mon 12:00'",
    (
        "src/relaydeck/client/features/terminal/keys.py",
        "C0 control pairing",
    ): "ASCII C0 control range; not a work label",
    (
        "src/relaydeck/client/features/terminal/keys.py",
        "C0 pairing fixture",
    ): "ASCII C0 control range; not a work label",
    (
        "tests/ui/test_hosts_connect_design.py",
        "ABC-123",
    ): "synthetic pairing-code fixture",
    (
        "tests/unit/test_usage_pane_limits.py",
        "PRO-1_X.Y",
    ): "plan-label sanitizer fixture",
    (
        "tests/integration/test_notifications.py",
        "CANARY-PRIVATE-KEY-",
    ): "synthetic private-key canary",
    (
        "tests/unit/test_herdr_plugin_manifest.py",
        "CONTEXT-MARKER-7f3a",
    ): "plugin-context probe marker",
    (
        "tests/unit/test_plugin_shell_entry.py",
        "CONTEXT-MARKER-91c2",
    ): "plugin-context probe marker",
    (
        "tests/unit/test_safe_diagnostics.py",
        "SYNTHETIC-SECRET-MARKER-9f3c",
    ): "log-redaction probe marker",
    (
        "tests/unit/test_webpush_logging.py",
        "SECRET-7f3a",
    ): "push-URL redaction probe marker",
}


def _allowed(rel: str, line: str) -> bool:
    return any(rel == path and text in line for path, text in ALLOWED)


def _label_hit(line: str) -> str | None:
    for match in LABELS.finditer(line):
        text = match.groupdict().get("tok") or match.group(0)
        if text in LABEL_EXCEPTIONS:
            continue
        if any(pattern.fullmatch(text) for pattern, _ in LABEL_EXCEPTION_PATTERNS):
            continue
        return text
    return None


def _tracked() -> list[Path]:
    try:
        listing = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=ROOT,
            capture_output=True,
            check=True,
            timeout=30,
        ).stdout.decode("utf-8")
        names = [name for name in listing.split("\0") if name]
    except (OSError, subprocess.SubprocessError):
        names = [
            path.relative_to(ROOT).as_posix()
            for path in ROOT.rglob("*")
            if path.is_file() and ".git" not in path.parts and "node_modules" not in path.parts
        ]
    found = []
    for name in names:
        path = ROOT / name
        if (
            name.startswith(SKIP_PREFIXES)
            or name == SELF
            or path.name in SKIP_NAMES
            or path.suffix in SKIP_SUFFIXES
            or not path.is_file()
        ):
            continue
        found.append(path)
    return sorted(found)


def _lines(path: Path) -> list[str]:
    try:
        return path.read_text(encoding="utf-8").splitlines()
    except (UnicodeDecodeError, OSError):
        return []  # binary or unreadable


def _findings() -> list[str]:
    hits: list[str] = []
    for path in _tracked():
        rel = path.relative_to(ROOT).as_posix()
        for number, line in enumerate(_lines(path), 1):
            if _allowed(rel, line):
                continue
            if _label_hit(line) or (path.suffix == ".py" and COMMENT_DATE.search(line)):
                hits.append(f"{rel}:{number}: {line.strip()[:100]}")
    return hits


def test_no_workflow_labels_or_comment_dates_in_tracked_files() -> None:
    assert _findings() == []


def test_the_detector_finds_labels_and_leaves_ordinary_words() -> None:
    for text in (
        "see RD-R3A for details",
        "addendum 4 repro",
        "Review 45 finding",
        "P2#1",
        "TASK-020 and GATE-03",
        "not part of this slice",
        "slice C picker",
        "ten items in the brief's order",
        "(brief point 5)",
        "the owner mock screens",
        "(owner review: tokens)",
        "Owner direction accepted",
        "The owner clarified that",
        "use the owner-approved 34px",
        "task-024-canary rule",
        "ev-m2-100 event",
        "(M1) snapshot guard",
        "## 9. Journal purge (m1)",
        "def _r2_keychain_sdk(",
        "insufficient per the assignment",
        "(verified by search)",
        "imports verified by execution",
        "Exit codes (verified live): 0",
        "# -- m2: bounded logs",
        "bounds. Also covers m2 here",
        "the untracked scratch launcher",
        "the maintainers' working copy",
    ):
        assert LABELS.search(text), text
    for text in (
        "a = items[1:3]  # array slice",
        "the selection slice changes",
        "time slice per tick",
        "records dispatched status",
        "a brief pause before retry",
        "the owner's verified public key",
        "the lease owner closes the session",
        "REQUIRES-OWNER-APPROVAL",
        "Create the owner-only config",
        "task_queue and a task list",
        "r2d2 robot, _result_row",
        "Verified by the signature check",
        "each verified live use extends it",
        "message ids m1 and m2",
        "FROM mappings m2 WHERE m2.rank",
        "builds from a scratch copy",
    ):
        assert not LABELS.search(text), text
    assert COMMENT_DATE.search("x = 1  # fixed 2026-10-12")
    assert not COMMENT_DATE.search('day = "2026-10-12"')


def test_the_detector_finds_label_forms_and_leaves_real_identifiers() -> None:
    for text in (
        "fix SOFT-KEYBOARD-1 handling",
        "SPLIT-TYPE-1c layout",
        "the IOS-POINTER-1 patch",
        "done in round 3",
        "TASK MK1 scope",
        "TASK B item",
        "P(R3)+A agree",
        '"""C6 owned devices',
        "Review20 markers",
        "review25-medium key",
        "see R11 flow",
        "X_TASK-1 joined",
        "TASK-1_Y joined",
    ):
        assert _label_hit(text), text
    for text in (
        "decode UTF-8 text",
        "SHA-256 digest",
        "ISO-8601 date",
        "RFC-9000 section",
        "AES-256 key",
        "UTF-16 units",
        "UAX-29 subset",
        "P-256 key",
        "BSD-3-Clause license",
        "MPL-2.0 copyleft",
        "MIT-0 terms",
        "PSF-2.0",
        "OFL-1.1 fonts",
        "LGPL-2.0-or-later",
        "BSD-2-Clause",
        "CANARY-APPROVE-YES-13579 marker",
        "HTTP/2 stream",
        "version 1.2.3",
        "v0.2.0 tag",
        "code=C1 verifier",
        "tool-bin-1.4.6-R6000.1",
        "0xC0 byte",
    ):
        assert _label_hit(text) is None, text


EVIDENCE_SHORTHAND = re.compile(r"\b[PA]\b|[PA]\(|\+[PAL]\b|[PAL]\+|\(R[0-9]+\)|\bL\b")

# Every shorthand evidence string the catalog used to carry (17 distinct forms).
OLD_EVIDENCE_STRINGS = (
    "A mostly; P(root)",
    "A only",
    "A only; closed source",
    "A+L",
    "A+P(bundle)",
    "P only",
    "P(R2)+A agree",
    "P(R3)",
    "P(R3)+A agree",
    "P(R3)+A",
    "P(R3)+A; two layouts",
    "P(R4)+L",
    "P(path)/A(fields)",
    "P(source)",
    "P(source+doc)",
    "P(status line)/A(db)",
    "P+A agree",
)


def test_catalog_evidence_has_no_internal_shorthand() -> None:
    for text in OLD_EVIDENCE_STRINGS:
        assert EVIDENCE_SHORTHAND.search(text), text
    assert not EVIDENCE_SHORTHAND.search("closed source; unresolved")
    assert not EVIDENCE_SHORTHAND.search("Documented JSONL session layout; usage is not read.")
    catalog = tomllib.loads((ROOT / "src/relaydeck/companion/data/cli_catalog.toml").read_text())
    assert catalog["cli"]
    for entry in catalog["cli"]:
        evidence = entry["evidence"]
        assert evidence and not EVIDENCE_SHORTHAND.search(evidence), entry["id"]


def test_the_scan_covers_the_tracked_tree() -> None:
    names = {path.relative_to(ROOT).as_posix() for path in _tracked()}
    assert "README.md" in names
    assert "src/relaydeck/companion/data/cli_catalog.toml" in names
    assert (
        "extensions/terminal/src/flutter/relaydeck_terminal/test/relaydeck_terminal_test.dart"
        in names
    )
    assert any(name.startswith("patches/") for name in names)
    assert not any(name.startswith(SKIP_PREFIXES) for name in names)
