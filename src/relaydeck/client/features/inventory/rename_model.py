"""Pure rename rules: validation, suggestions and the counter. No Flet."""

from __future__ import annotations

import re
from collections.abc import Collection, Iterable
from dataclasses import dataclass

from relaydeck.contracts.graphemes import grapheme_count, graphemes
from relaydeck.contracts.launch import DISPLAY_NAME_MAX, display_name_clean, display_name_error

WARN_AT = 28
MAX_SUGGESTIONS = 3
STOP_WORDS = frozenset(
    "a an the of to for and or in on at by with from into onto about my our your this that "
    "these those is are be it its as please can you we i all any some new old more less "
    "just then also again still now".split()
)
VERB_ROLES = {
    "review": "Reviewer",
    "fix": "Fixer",
    "write": "Writer",
    "build": "Builder",
    "test": "Tester",
    "refactor": "Refactorer",
    "debug": "Debugger",
    "implement": "Implementer",
    "document": "Writer",
    "design": "Designer",
    "plan": "Planner",
    "research": "Researcher",
    "migrate": "Migrator",
    "update": "Maintainer",
    "audit": "Auditor",
    "deploy": "Deployer",
}
_WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9'_-]*")
_REPO_SUFFIX = re.compile(r"\.(?:nosync|git|local|lock)\b", re.IGNORECASE)

OK = ""
EMPTY = "empty"
TOO_LONG = "too_long"
CONTROL = "control"
DUPLICATE = "duplicate"
UNCHANGED = "unchanged"


@dataclass(frozen=True)
class Verdict:
    """`code` is empty when the name can be saved; `text` is the helper line to show."""

    code: str
    text: str

    @property
    def saveable(self) -> bool:
        return not self.code

    @property
    def blocking_error(self) -> bool:
        return self.code in (EMPTY, TOO_LONG, CONTROL, DUPLICATE)


def clamp(text: str) -> str:
    """Stop input at the maximum length, counted in grapheme clusters."""
    clusters = graphemes(text)
    return text if len(clusters) <= DISPLAY_NAME_MAX else "".join(clusters[:DISPLAY_NAME_MAX])


def utf16_length(text: str) -> int:
    """Length in UTF-16 code units, the offsets a Flutter text selection uses."""
    return len(text.encode("utf-16-le")) // 2


def counter_text(text: str) -> str:
    return f"{grapheme_count(text)} / {DISPLAY_NAME_MAX}"


def counter_tone(text: str) -> str:
    count = grapheme_count(text)
    if count > DISPLAY_NAME_MAX:
        return "danger"
    return "warn" if count >= WARN_AT else "muted"


def validate(text: str, current: str, taken: Collection[str], space: str = "") -> Verdict:
    """Save rule for a typed name. `taken` holds other agents' names in the same space."""
    problem = display_name_error(text)
    if problem:
        if problem.startswith("Name can't be empty"):
            return Verdict(EMPTY, problem)
        if "control" in problem:
            return Verdict(CONTROL, problem)
        return Verdict(TOO_LONG, problem)
    cleaned = display_name_clean(text)
    if cleaned == display_name_clean(current):
        return Verdict(UNCHANGED, "")
    if cleaned.casefold() in {display_name_clean(name).casefold() for name in taken}:
        where = space or "this space"
        return Verdict(DUPLICATE, f"Another agent in {where} is called that")
    return Verdict(OK, "")


def _title(word: str) -> str:
    return word[:1].upper() + word[1:]


def _words(text: str, drop: Collection[str]) -> list[str]:
    cleaned = _REPO_SUFFIX.sub("", text)
    blocked = {item.casefold() for item in drop}
    return [
        word
        for word in _WORD.findall(cleaned)
        if len(word) > 1
        and not word.isdigit()
        and word.casefold() not in STOP_WORDS
        and word.casefold() not in blocked
    ]


def _verb_role(word: str) -> str:
    lowered = word.casefold()
    stem = lowered.removesuffix("ing")
    for candidate in (
        lowered,
        stem,
        stem + "e",
        lowered.removesuffix("s"),
        lowered.removesuffix("ed"),
    ):
        if candidate in VERB_ROLES:
            return VERB_ROLES[candidate]
    return ""


def _project(space: str) -> str:
    words = _WORD.findall(_REPO_SUFFIX.sub("", space))
    return " ".join(_title(word) for word in words[:2])


def _correction(current: str, vocabulary: Collection[str]) -> str:
    """Swap adjacent letters in a word the context does not know, when that makes a known one."""
    known = {word.casefold() for word in vocabulary}
    fixed: list[str] = []
    changed = False
    for word in current.split(" "):
        replacement = word
        if word and word.casefold() not in known:
            for i in range(len(word) - 1):
                swapped = word[:i] + word[i + 1] + word[i] + word[i + 2 :]
                if swapped.casefold() in known:
                    replacement = (
                        swapped[:1].upper() + swapped[1:] if word[:1].isupper() else swapped
                    )
                    changed = True
                    break
        fixed.append(replacement)
    return " ".join(fixed) if changed else ""


def suggestions(
    task: str,
    space: str,
    current: str,
    taken: Iterable[str] = (),
    *,
    limit: int = MAX_SUGGESTIONS,
) -> tuple[str, ...]:
    """Up to three names: distinctive task words, `{Project} {Role}`, a spelling fix."""
    others = [display_name_clean(name) for name in taken]
    project = _project(space)
    space_words = _WORD.findall(_REPO_SUFFIX.sub("", space))
    task_words = _words(task, space_words)
    first_verb = next(iter(_WORD.findall(task)), "")
    role = _verb_role(first_verb)
    distinct = [word for word in task_words if not (_verb_role(word) and word == task_words[0])]
    candidates: list[str] = []
    if distinct:
        candidates.append(" ".join(_title(word) for word in distinct[:3]))
    if project and role:
        candidates.append(f"{project} {role}")
    vocabulary = [*task_words, *space_words, *(w for name in others for w in name.split())]
    fixed = _correction(display_name_clean(current), vocabulary)
    if fixed:
        candidates.append(fixed)
    blocked = {display_name_clean(current).casefold(), *(name.casefold() for name in others)}
    result: list[str] = []
    for name in candidates:
        cleaned = display_name_clean(name)
        if cleaned and not display_name_error(cleaned) and cleaned.casefold() not in blocked:
            blocked.add(cleaned.casefold())
            result.append(cleaned)
    return tuple(result[:limit])
