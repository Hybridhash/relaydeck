"""The vendored Flet backend never logs event or outgoing payload contents (pasted text rides them).

A source check, not a Dart test: building a real backend needs a socket and a page, while the rule
is only that `debugPrint` is never handed a raw payload.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "extensions/terminal/third_party/flet-1.0.3-rd1/lib/src/flet_backend.dart"
RAW = re.compile(r"\$\{?(eventData|data|message\.payload|payload)\b")


def _debug_print_calls(source: str) -> list[str]:
    calls = []
    for match in re.finditer(r"debugPrint\(", source):
        depth, end = 0, match.end() - 1
        while end < len(source):
            depth += {"(": 1, ")": -1}.get(source[end], 0)
            if depth == 0:
                break
            end += 1
        calls.append(source[match.start() : end + 1])
    return calls


def test_debug_print_never_receives_a_raw_payload() -> None:
    offenders = [c for c in _debug_print_calls(BACKEND.read_text()) if RAW.search(c)]
    assert offenders == []


def test_the_two_payload_sites_log_a_size() -> None:
    calls = _debug_print_calls(BACKEND.read_text())
    sized = [c for c in calls if "_payloadSize(" in c]
    assert len(sized) == 2
