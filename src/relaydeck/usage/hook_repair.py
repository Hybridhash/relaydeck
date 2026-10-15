"""Why a limits hook stopped working, and the one consented repair.

Diagnosis only reads. Repair re-adds RelayDeck's managed settings field and nothing else: a
custom field the user put there is reported, never overwritten without its own explicit
consent, and no other field is ever restored from an old backup.
"""

from __future__ import annotations

import shlex
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from relaydeck.usage import hook_files as fs
from relaydeck.usage import hook_install as install
from relaydeck.usage.hook_doctor import HookSource, cache_state
from relaydeck.usage.hook_install import (
    ABSENT_STATUSLINE,
    HookResult,
    install_hook,
    receipt_state,
    upgrade_available,
)
from relaydeck.usage.hook_setup import HOOK_FILENAME, HookSetupCheck, default_hook_path

LIMITS_FORMAT = "json-statusline-limits-v1"

# reason -> (what happened, what repair would change)
_TEXT = {
    "ok": ("The hook is in place.", ""),
    "settings_replaced": (
        "The settings file was replaced after RelayDeck installed its hook; the managed "
        "field is gone.",
        "add RelayDeck's statusLine field to {settings}; every other field is kept",
    ),
    "managed_field_missing": (
        "The settings file has no RelayDeck status line.",
        "add RelayDeck's statusLine field to {settings}; every other field is kept",
    ),
    "managed_field_edited": (
        "RelayDeck's status line field was edited and no longer runs its hook as installed.",
        "set the statusLine field in {settings} back to RelayDeck's command; "
        "every other field is kept",
    ),
    "custom_statusline_present": (
        "Another status line is configured; RelayDeck does not overwrite it.",
        "replace that status line with RelayDeck's (only with --replace-statusline)",
    ),
    "settings_missing": (
        "The settings file does not exist.",
        "create {settings} containing only RelayDeck's statusLine field "
        "(only with --create-settings)",
    ),
    "hook_script_missing": (
        "RelayDeck's hook script is missing from its private folder.",
        "write the reviewed hook script again; the settings file is not changed",
    ),
    "hook_script_outdated": (
        "The hook script was changed outside RelayDeck; it is not a version RelayDeck ships.",
        "",
    ),
    "settings_unreadable": ("The settings file cannot be read safely.", ""),
    "not_configured": ("No settings file is configured for this source.", ""),
    "cache_stale": (
        "The hook is installed but has not reported recently; the CLI runs it only while in use.",
        "",
    ),
}

_STATE_REASON = {
    "script_missing": "hook_script_missing",
    "script_outdated": "hook_script_outdated",
    "settings_missing": "settings_missing",
    "invalid": "settings_unreadable",
    "unreadable": "settings_unreadable",
    "not_configured": "not_configured",
}


@dataclass(frozen=True)
class HookDiagnosis:
    kind: str
    format: str
    reason: str
    summary: str
    change: str
    fix: str
    repairable: bool
    needs: str | None = None  # "create_settings" | "replace_statusline" when consent is separate
    statusline: Any = None  # the statusLine value diagnosed (ABSENT_STATUSLINE when none)

    def public(self) -> dict[str, Any]:
        return {
            "reason": self.reason,
            "summary": self.summary,
            "fix": self.fix,
            "repairable": self.repairable,
        }


def _fix_command(kind: str, format_id: str, needs: str | None) -> str:
    command = f"relaydeck usage repair --source {shlex.quote(kind + '/' + format_id)}"
    if needs == "create_settings":
        command += " --create-settings"
    elif needs == "replace_statusline":
        command += " --replace-statusline"
    return command


def _settings_image(settings: Path | None) -> bytes | None:
    """The settings bytes as one bounded, validated read; None when missing or unsafe."""
    if settings is None:
        return None
    try:
        with fs.directory(settings.parent) as fd:
            return fs.read(fd, settings.name).raw
    except (OSError, ValueError, TypeError, RecursionError, fs.HookFileError):
        return None


def _script_digest(script: Path) -> str | None:
    """The on-disk script digest, or None when it cannot be read safely."""
    try:
        with fs.directory(script.parent, private=True) as fd:
            return fs.read(fd, script.name, private=True).digest
    except (OSError, ValueError, TypeError, RecursionError, fs.HookFileError):
        return None


def _statusline_of(raw: bytes | None) -> Any:
    """The statusLine value of one settings image, ABSENT_STATUSLINE when there is none."""
    if raw is None:
        return ABSENT_STATUSLINE
    try:
        return fs.document(raw).get("statusLine", ABSENT_STATUSLINE)
    except (ValueError, TypeError, RecursionError, fs.HookFileError):
        return ABSENT_STATUSLINE


def is_managed_shape(line: Any, script: Path, cache: Path | None) -> bool:
    """RelayDeck's own field with only its interpreter changed: exactly
    `{type: command, command: "<python> <our script> --cache-path <our cache>"}`.
    A wrapper, pipeline, extra argument or another script is never ours."""
    if not isinstance(line, dict) or set(line) != {"type", "command"} or line["type"] != "command":
        return False
    command = line["command"]
    if not isinstance(command, str) or cache is None:
        return False
    try:
        parts = shlex.split(command)
    except ValueError:
        return False
    return (
        len(parts) == 4
        and Path(parts[0]).is_absolute()
        and Path(parts[0]).name.startswith("python")
        and parts[1] == str(script)
        and parts[2] == "--cache-path"
        and parts[3] == str(cache)
    )


def diagnose_source(
    source: HookSource,
    *,
    hooks_dir: Path | None = None,
    interpreter: Path | None = None,
    now_ms: int | None = None,
) -> HookDiagnosis:
    """Read-only: the reason the hook is not working for this source, or `ok`."""
    settings, cache = source.limits_settings, source.limits_cache
    now = int(time.time() * 1000) if now_ms is None else now_ms
    script = (hooks_dir or default_hook_path().parent) / HOOK_FILENAME
    # Classify and capture the expectation from one snapshot: the settings image must be the same
    # before and after the check, so the value bound into the repair is the value classified.
    image: bytes | None = None
    state = "invalid"
    for _ in range(3):
        before = _settings_image(settings)
        result = HookSetupCheck(settings, cache, script, interpreter=interpreter).check(now)
        image = _settings_image(settings)
        state = result["state"] if result else "not_configured"
        if before == image:
            break
    else:
        state = "invalid"  # the file kept changing; nothing is classified or bound
    statusline = _statusline_of(image)
    needs: str | None = None
    repairable = False
    if state == "installed":
        reason = "ok" if cache_state(cache, now) != "stale" else "cache_stale"
    elif state == "statusline_missing":
        receipt = receipt_state(settings, hooks_dir=hooks_dir) if settings else None
        reason = "settings_replaced" if receipt == "installed" else "managed_field_missing"
        repairable = True
    elif state == "conflict":
        ours = is_managed_shape(statusline, script, cache)
        reason = "managed_field_edited" if ours else "custom_statusline_present"
        repairable = ours
        needs = None if ours else "replace_statusline"
    elif state == "settings_missing":
        reason, needs = "settings_missing", "create_settings"
    else:
        reason = _STATE_REASON.get(state, "settings_unreadable")
        if reason == "hook_script_outdated" and settings is not None and cache is not None:
            repairable = upgrade_available(
                settings, cache, hooks_dir=hooks_dir, interpreter=interpreter
            )
        else:
            repairable = reason == "hook_script_missing"
    if reason == "hook_script_outdated" and repairable:
        what = "RelayDeck was updated; the hook script on disk is a previous shipped version."
        change = (
            "replace the hook script with the shipped version; the settings file is not changed"
        )
    elif (
        reason == "hook_script_outdated" and _script_digest(script) in install.PREVIOUS_HOOK_SHA256
    ):
        what = (
            "The hook script is a previous RelayDeck version, but its settings line or "
            "install record changed outside RelayDeck, so it is left alone."
        )
        change = ""
    else:
        what, change = _TEXT[reason]
    return HookDiagnosis(
        kind=source.kind,
        format=source.format,
        reason=reason,
        summary=what,
        change=change.format(settings=settings),
        fix=""
        if reason in ("ok", "cache_stale")
        else _fix_command(source.kind, source.format, needs),
        repairable=repairable,
        needs=needs,
        statusline=statusline,
    )


def limits_sources(sources: Iterable[HookSource]) -> list[HookSource]:
    return [
        s
        for s in sources
        if s.limits_format == LIMITS_FORMAT
        and s.limits_settings is not None
        and s.limits_cache is not None
    ]


@dataclass(frozen=True)
class RepairOutcome:
    kind: str
    format: str
    reason: str
    action: str  # repaired | healthy | needs_consent | not_repairable | confirmation_declined
    result: str | None = None
    fix: str = ""

    def public(self) -> dict[str, Any]:
        return asdict(self)


def repair_sources(
    sources: Sequence[HookSource],
    *,
    confirm: Callable[[Sequence[HookDiagnosis]], bool],
    create_settings: bool = False,
    replace_statusline: bool = False,
    hooks_dir: Path | None = None,
    interpreter: Path | None = None,
) -> list[RepairOutcome]:
    """Diagnose every source, ask once for the ones that can be repaired, then repair them.

    A source that needs separate consent (create settings, replace a custom status line) is
    repaired only when that flag was passed; otherwise it is reported with its fix command.
    """
    diagnoses = [
        diagnose_source(s, hooks_dir=hooks_dir, interpreter=interpreter)
        for s in limits_sources(sources)
    ]
    by_key = {(s.kind, s.format): s for s in limits_sources(sources)}
    planned: list[HookDiagnosis] = []
    outcomes: list[RepairOutcome] = []
    for d in diagnoses:
        if d.reason in ("ok", "cache_stale"):
            outcomes.append(RepairOutcome(d.kind, d.format, d.reason, "healthy"))
        elif d.needs == "create_settings" and create_settings:
            planned.append(d)
        elif d.needs == "replace_statusline" and replace_statusline:
            planned.append(d)
        elif d.repairable:
            planned.append(d)
        elif d.needs is not None:
            outcomes.append(RepairOutcome(d.kind, d.format, d.reason, "needs_consent", fix=d.fix))
        else:
            outcomes.append(RepairOutcome(d.kind, d.format, d.reason, "not_repairable", fix=d.fix))
    if planned and not confirm(planned):
        outcomes.extend(
            RepairOutcome(d.kind, d.format, d.reason, "confirmation_declined", fix=d.fix)
            for d in planned
        )
        return outcomes
    for d in planned:
        source = by_key[(d.kind, d.format)]
        assert source.limits_settings is not None and source.limits_cache is not None
        result: HookResult = install_hook(
            source.limits_settings,
            source.limits_cache,
            confirmed=True,
            create_settings=d.needs == "create_settings" and create_settings,
            replace_statusline=d.reason == "managed_field_edited"
            or (d.needs == "replace_statusline" and replace_statusline),
            hooks_dir=hooks_dir,
            interpreter=interpreter,
            expect_statusline=d.statusline,
        )
        if result.state == "changed_since_diagnosis":
            # The file moved after the diagnosis or the confirmation: nothing was written, and
            # the permission is not carried forward. Report what is there now.
            current = diagnose_source(source, hooks_dir=hooks_dir, interpreter=interpreter)
            outcomes.append(
                RepairOutcome(
                    d.kind,
                    d.format,
                    current.reason,
                    "changed_since_diagnosis",
                    result.state,
                    current.fix,
                )
            )
            continue
        outcomes.append(
            RepairOutcome(
                d.kind,
                d.format,
                d.reason,
                "repaired" if result.state == "installed" else "failed",
                result.state,
                d.fix if result.state != "installed" else "",
            )
        )
    return outcomes
