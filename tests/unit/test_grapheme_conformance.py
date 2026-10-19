"""The segmenter passes the full Unicode GraphemeBreakTest corpus."""

from __future__ import annotations

from pathlib import Path

from relaydeck.contracts.graphemes import graphemes

CORPUS = Path(__file__).resolve().parents[1] / "fixtures" / "unicode" / "GraphemeBreakTest.txt"


def _cases() -> list[tuple[str, list[str]]]:
    cases = []
    for raw in CORPUS.read_text(encoding="utf-8").splitlines():
        body = raw.split("#", 1)[0].strip()
        if not body:
            continue
        clusters, current = [], ""
        for token in body.split():
            if token in ("÷", "×"):
                if token == "÷" and current:
                    clusters.append(current)
                    current = ""
            else:
                current += chr(int(token, 16))
        if current:
            clusters.append(current)
        cases.append(("".join(clusters), clusters))
    return cases


def test_the_full_unicode_corpus_has_no_failures() -> None:
    cases = _cases()
    assert len(cases) == 1093
    failures = [(text, want) for text, want in cases if graphemes(text) != want]
    assert not failures, f"{len(failures)} of {len(cases)}: {failures[:3]}"


def test_the_corpus_copy_ships_with_its_license() -> None:
    assert (CORPUS.parent / "UNICODE_LICENSE.txt").is_file()
