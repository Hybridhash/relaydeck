"""Display names, Herdr slugs, graphemes, validation and suggestions: no Flet."""

from __future__ import annotations

import re

import pytest

from relaydeck.client.features.inventory import rename_model as m
from relaydeck.contracts.graphemes import grapheme_count, graphemes
from relaydeck.contracts.launch import (
    AGENT_NAME,
    agent_slug,
    display_name_clean,
    display_name_error,
)


@pytest.mark.parametrize(
    ("label", "slug"),
    [
        ("Docs Writer", "docs-writer"),
        ("8", ""),
        ("🚀", ""),
        ("🚀 Launch", "launch"),
        ("Ünïcode Name!", "unicode-name"),
        ("a.b/c d", "a-b-c-d"),
        ("42 Answers", "answers"),
        ("x" * 40, "x" * 32),
        ("snake_case-ok", "snake_case-ok"),
        ("ends with dash -", "ends-with-dash"),
        ("a" * 31 + " b", "a" * 31),
    ],
)
def test_slug_is_always_a_valid_herdr_name_or_empty(label: str, slug: str) -> None:
    assert agent_slug(label) == slug
    assert not slug or AGENT_NAME.fullmatch(slug)


@pytest.mark.parametrize(
    ("text", "count"),
    [
        ("abc", 3),
        ("é", 1),
        ("é", 1),
        ("🚀", 1),
        ("👨‍👩‍👧", 1),
        ("👍🏽", 1),
        ("🇩🇪🇫🇷", 2),
        ("1️⃣", 1),
        ("", 0),
    ],
)
def test_grapheme_count_counts_clusters_not_code_points(text: str, count: int) -> None:
    assert grapheme_count(text) == count
    assert "".join(graphemes(text)) == text


def test_clean_trims_and_collapses_whitespace() -> None:
    assert display_name_clean("  Docs \t  Writer  ") == "Docs Writer"
    assert display_name_clean("   ") == ""


@pytest.mark.parametrize(
    ("text", "ok"),
    [
        ("Docs Writer", True),
        ("🚀 launch", True),
        ("x" * 32, True),
        ("🚀" * 32, True),
        ("x" * 33, False),
        ("🚀" * 33, False),
        ("", False),
        ("   ", False),
        ("a\nb", False),
        ("a\x00b", False),
        ("a‎b", False),
    ],
)
def test_display_name_rule(text: str, ok: bool) -> None:
    assert (display_name_error(text) == "") is ok


def test_clamp_stops_input_at_thirty_two_clusters() -> None:
    assert m.clamp("x" * 40) == "x" * 32
    assert m.clamp("🚀" * 40) == "🚀" * 32
    assert m.clamp("short") == "short"


def test_counter_text_and_tone() -> None:
    assert m.counter_text("🚀🚀") == "2 / 32"
    assert m.counter_tone("a" * 27) == "muted"
    assert m.counter_tone("a" * 28) == "warn"
    assert m.counter_tone("a" * 32) == "warn"
    assert m.counter_tone("a" * 33) == "danger"


@pytest.mark.parametrize(
    ("text", "current", "taken", "code"),
    [
        ("", "web", [], m.EMPTY),
        ("   ", "web", [], m.EMPTY),
        ("x" * 33, "web", [], m.TOO_LONG),
        ("a\nb", "web", [], m.CONTROL),
        ("web", "web", [], m.UNCHANGED),
        ("  web  ", "web", [], m.UNCHANGED),
        ("Web", "web", [], m.OK),
        ("docs writer", "web", ["Docs Writer"], m.DUPLICATE),
        ("DOCS  WRITER", "web", ["docs writer"], m.DUPLICATE),
        ("Docs Writer", "web", ["Build Helper"], m.OK),
    ],
)
def test_validation(text: str, current: str, taken: list[str], code: str) -> None:
    verdict = m.validate(text, current, taken, "Harbor Docs")
    assert verdict.code == code and verdict.saveable == (code == m.OK)
    assert bool(verdict.text) == (code not in (m.OK, m.UNCHANGED))


def test_duplicate_message_names_the_space() -> None:
    verdict = m.validate("Docs Writer", "web", ["docs writer"], "Harbor Docs")
    assert verdict.text == "Another agent in Harbor Docs is called that"
    assert m.validate("Docs Writer", "web", ["docs writer"]).text.endswith(
        "this space is called that"
    )


def test_suggestions_drop_stop_words_repo_suffix_space_name_and_leading_verb() -> None:
    names = m.suggestions("Review the release notes for Harbor Docs.nosync", "Harbor Docs", "web")
    assert names[0] == "Release Notes"
    assert "Harbor Docs Reviewer" in names


def test_suggestions_use_the_verb_to_role_map() -> None:
    cases = {
        "Fixing flaky login tests": "Fixer",
        "Write onboarding copy": "Writer",
        "Debug the sync loop": "Debugger",
        "Refactor parser": "Refactorer",
    }
    for task, role in cases.items():
        assert f"Atlas {role}" in m.suggestions(task, "Atlas", "x"), task


def test_suggestions_have_no_duplicates_and_skip_current_and_taken_names() -> None:
    got = m.suggestions("Fix login", "Atlas", "Login", ["Atlas Fixer"])
    assert got == ()
    got = m.suggestions("Fix login flow", "Atlas", "web", ["login flow"])
    assert [n.casefold() for n in got] == list(dict.fromkeys(n.casefold() for n in got))
    assert "Login Flow" not in got and "Atlas Fixer" in got


def test_suggestions_offer_a_spelling_fix_from_known_words() -> None:
    got = m.suggestions("Writer of release notes", "Harbor Docs", "Docs Wrtier", ["Docs"])
    assert "Docs Writer" in got


def test_suggestions_are_capped_and_valid() -> None:
    got = m.suggestions("Review parser output quality tests", "Atlas", "Atlas Reviewr", [])
    assert len(got) <= m.MAX_SUGGESTIONS
    assert all(not display_name_error(name) for name in got)
    assert m.suggestions("", "", "web") == ()
    assert re.fullmatch(r"[A-Z][a-z]+( [A-Z][a-z]+)*", got[0])


@pytest.mark.parametrize(
    ("text", "count"),
    [
        ("\u1100\u1161" * 17, 17),
        ("\u1100\u1161\u11a8", 1),
        ("\uac00\u11a8", 1),
        ("\u1100\uac00", 1),
        ("\u0915\u094d\u0937", 1),
        ("\u0915\u094d\u0937\u093f", 1),
        ("\u0915\u094d a", 3),
        ("a\u200db", 2),
        ("a\u200d\U0001f680", 2),
        ("\U0001f680\u200d\U0001f680", 1),
        ("\U0001f468\u200d\U0001f469\u200d\U0001f467", 1),
        ("\U0001f44b\U0001f3fd", 1),
        ("\U0001f3f4\U000e0067\U000e0062\U000e007f", 1),
        ("\U0001f1e9\U0001f1ea\U0001f1eb\U0001f1f7\U0001f1fa", 3),
        ("\r\n", 1),
        ("\u0e01\u0e33", 1),
        ("e\u0301e\u0301", 2),
    ],
)
def test_grapheme_segmentation_follows_the_standard_rules(text: str, count: int) -> None:
    assert grapheme_count(text) == count
    assert "".join(graphemes(text)) == text


def test_hangul_names_clamp_and_validate_at_the_boundary_in_clusters() -> None:
    jamo = "\u1100\u1161"
    assert m.clamp(jamo * 40) == jamo * 32
    assert display_name_error(jamo * 32) == ""
    assert display_name_error(jamo * 33) != ""
    assert m.counter_text(jamo * 17) == "17 / 32"
    family = "\U0001f468\u200d\U0001f469\u200d\U0001f467"
    assert m.clamp(family * 33) == family * 32
    assert m.validate("\u0915\u094d\u0937" * 32, "web", []).saveable


def test_utf16_length_counts_supplementary_characters_twice() -> None:
    assert m.utf16_length("web") == 3
    assert m.utf16_length("\U0001f680 Plan") == 7
    assert m.utf16_length("\U0001f468\u200d\U0001f469\u200d\U0001f467") == 8
