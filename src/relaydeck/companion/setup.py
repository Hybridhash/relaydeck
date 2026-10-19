"""`relaydeck setup`: interactive, idempotent first-run checks.

Read-only prerequisite checks come first. Every change asks first: creating the
owner-only config directory (0700) and file (0600) from the packaged template
when they are missing, the optional usage hook, source additions, and the
Tailscale Serve mapping; an existing config is never rewritten. `--yes`
answers only the Serve mapping question. Serve uses only the configured
private HTTPS port, never 8443 or 8444, and refuses Funnel. Connector enrollment
is not done here; it stays the explicit flow in the README (Install, step 3).

It never prints or reads secrets, and tests inject the Tailscale runner and the probe.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

from relaydeck.companion import config_edit, prereqs, tailscale
from relaydeck.companion.config import load_config
from relaydeck.companion.detect import DetectReport, run_detect
from relaydeck.companion.detect import load_catalog as load_detect_catalog
from relaydeck.usage.hook_doctor import HookSource
from relaydeck.usage.hook_install import install_hook
from relaydeck.usage.hook_repair import diagnose_source

RESERVED_HTTPS_PORTS = (8443, 8444)
DEFAULT_HTTPS_PORT = 8445  # the template's tailscale_https_port
DEFAULT_LOCAL_PORT = 8125  # the template's port
STOPPED_LINE = (
    "Setup stopped: a prerequisite is not ready. Nothing was written and no Serve change was made."
)


def template_text() -> str:
    return (
        resources.files("relaydeck.companion")
        .joinpath("companion.template.toml")
        .read_text(encoding="utf-8")
    )


def ensure_config(path: Path) -> str:
    """Create the owner-only config from the template when missing; never overwrite."""
    if path.is_symlink() or path.exists():
        return f"Config present: {path} (not changed)."
    try:
        dir_fd = config_edit.pinned_dir_fd(path.parent, create_missing=True)
    except (ValueError, OSError) as exc:
        return f"Config not created: {exc}"
    try:
        try:
            fd = os.open(
                path.name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=dir_fd,
            )
        except FileExistsError:
            return f"Config present: {path} (not changed)."
        except OSError as exc:
            return f"Config not created: {exc}"
        try:
            # Exclusively created by us, so tightening it cannot affect anyone else's file.
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(template_text())
                handle.flush()
                os.fsync(handle.fileno())
            os.fsync(dir_fd)
        except OSError as exc:
            try:
                os.unlink(path.name, dir_fd=dir_fd)
            except OSError:
                pass
            return f"Config not created: {exc}"
    finally:
        os.close(dir_fd)
    return f"Config created from the template: {path} (owner-only). Edit it before start."


def choose_https_port(configured: int, used: set[int], public: set[int]) -> int | None:
    """Only the configured port; changing it requires an explicit config edit."""
    return (
        None
        if configured in RESERVED_HTTPS_PORTS or configured in used or configured in public
        else configured
    )


@dataclass(frozen=True)
class SetupReport:
    lines: tuple[str, ...]
    exit_code: int


def _config_or_none(path: Path, runner: tailscale.Runner) -> Any:
    try:
        return load_config(path, resolve_origin=lambda port: tailscale.private_origin(port, runner))
    except (ValueError, OSError):
        return None


def preflight(
    config_path: Path,
    probes: prereqs.Probes | None = None,
    runner: tailscale.Runner = tailscale.default_runner,
) -> prereqs.Report:
    """The read-only prerequisite report. Shared by setup and `doctor`; writes nothing."""
    current_probes = probes or prereqs.default_probes(runner)
    config = _config_or_none(config_path, current_probes.tailscale)
    usage = getattr(config, "usage", None)
    return prereqs.run_prereqs(
        socket=prereqs.probe_socket(config.herdr_socket if config else None),
        https_port=config.tailscale_https_port if config else DEFAULT_HTTPS_PORT,
        expected_target=f"http://127.0.0.1:{config.port if config else DEFAULT_LOCAL_PORT}",
        probes=current_probes,
        usage_roots=tuple(source.root for source in usage.sources) if usage else (),
    )


HOOK_FAILURE_GUIDANCE = {
    "conflict": (
        "Review statusLine, hook and source cache with usage doctor. Custom replacement requires "
        "separate explicit --replace-statusline consent; setup does not replace it."
    ),
    "create_confirmation_required": (
        "Settings creation was not authorized. Re-run and separately confirm creating the settings "
        "file/directory, or create it yourself."
    ),
    "recovery_required": (
        "Settings may have changed. Stop settings writers; review the private prepared "
        "receipt/backup. "
        "Use usage doctor and the field-only usage uninstall before retrying; do not restore the "
        "whole backup."
    ),
    "unreadable": (
        "Check owner-only settings/hook permissions and access; run usage doctor before retrying."
    ),
    "invalid": (
        "Check JSON, file ownership and private paths with usage doctor; correct invalid or unsafe "
        "files before retrying."
    ),
    "busy": "Stop other settings/install writers, then retry the explicit install.",
    "concurrent_edit": (
        "Stop other settings writers and review their changes with usage doctor before retrying."
    ),
    "resource_invalid": (
        "Reinstall the reviewed package resource in the supported Python 3.13 tool environment."
    ),
    "capacity": (
        "Review private hook receipts before another install; do not remove active source receipts "
        "blindly."
    ),
    "changed_since_diagnosis": (
        "The settings file changed while setup was asking. Nothing was written; review it with "
        "usage doctor and run relaydeck usage repair."
    ),
    "not_configured": (
        "Configure limits_settings and a separate private limits_cache for a supported limits "
        "source before requesting installation."
    ),
}


def _limits_hook_setup(
    sources: Iterable[HookSource], *, explicit: bool, confirm: Callable[[str], bool]
) -> SetupReport:
    entries = [
        s
        for s in sources
        if s.limits_format == "json-statusline-limits-v1"
        and s.limits_settings is not None
        and s.limits_cache is not None
    ]
    lines: list[str] = []
    if explicit and not entries:
        return SetupReport(
            (
                "Limits hook: not_configured. " + HOOK_FAILURE_GUIDANCE["not_configured"],
                "Setup stopped before any Serve change.",
            ),
            1,
        )
    for source in entries:
        assert source.limits_settings is not None and source.limits_cache is not None
        diagnosis = diagnose_source(source)
        if diagnosis.reason in ("ok", "cache_stale"):
            lines.append(f"Limits hook for {source.kind}: in place; nothing to change.")
            continue
        repairing = diagnosis.repairable
        if repairing:
            question = (
                f"{diagnosis.summary} Repair it now? RelayDeck will {diagnosis.change}. [y/N] "
            )
        else:
            question = (
                "Stop settings writers. Install offline limits hook (JSON reformatted)? [y/N] "
            )
        allowed = explicit or confirm(question)
        if not allowed:
            lines.append(
                "Limits hook installation skipped (not authorized); setup continues. "
                f"Later: {diagnosis.fix or 'relaydeck usage repair'}"
            )
            continue
        result = install_hook(
            source.limits_settings,
            source.limits_cache,
            confirmed=True,
            replace_statusline=diagnosis.reason == "managed_field_edited",
            expect_statusline=diagnosis.statusline,
        )
        if result.state == "create_confirmation_required" and confirm(
            "Also create missing settings file/directories? [y/N] "
        ):
            result = install_hook(
                source.limits_settings,
                source.limits_cache,
                confirmed=True,
                create_settings=True,
                expect_statusline=diagnosis.statusline,
            )
        if result.state != "installed":
            reason = result.state if result.state in HOOK_FAILURE_GUIDANCE else "failed"
            guidance = HOOK_FAILURE_GUIDANCE.get(
                reason,
                "Review settings and private hook receipts with usage doctor before retrying.",
            )
            return SetupReport(
                (
                    *lines,
                    f"Limits hook: {reason}. {guidance}",
                    "Setup stopped before any Serve change.",
                ),
                1,
            )
        lines.append("Limits hook: installed (invocation is not yet proven).")
    return SetupReport(tuple(lines), 0)


def detect_report(config_path: Path) -> DetectReport:
    """`relaydeck setup --detect`: the read-only CLI report.

    Writes nothing and runs no agent CLI.
    """
    return run_detect(
        load_detect_catalog(),
        home=os.path.expanduser("~"),
        environ=os.environ,
        config_path=config_path,
    )


def run_setup(
    *,
    config_path: Path,
    assume_yes: bool,
    confirm: Callable[[str], bool],
    runner: tailscale.Runner = tailscale.default_runner,
    probe: Callable[[], int] | None = None,
    probes: prereqs.Probes | None = None,
    install_limits_hook: bool = False,
    source_confirm: Callable[[str], bool] | None = None,
    source_detect: Callable[[Path], DetectReport] | None = None,
    emit_line: Callable[[str], None] | None = None,
) -> SetupReport:
    # the read-only preflight runs first. Any prerequisite that is not ok stops
    # setup before a config is created or a Serve call is made. `--yes` does not bypass it.
    report = preflight(config_path, probes, runner)
    if not report.ready:
        return SetupReport((*report.lines(), STOPPED_LINE), 1)
    if not (config_path.is_symlink() or config_path.exists()) and not confirm(
        f"Create the owner-only config at {config_path} from the template? [y/N] "
    ):
        return SetupReport(
            (
                *report.lines(),
                "Config creation was not authorized; nothing was written.",
            ),
            1,
        )
    lines: list[str] = [*report.lines(), ensure_config(config_path)]
    exit_code = 0
    try:
        config = load_config(
            config_path, resolve_origin=lambda port: tailscale.private_origin(port, runner)
        )
    except (ValueError, OSError) as exc:
        lines.append(f"Config needs editing before setup can continue: {exc}")
        return SetupReport(tuple(lines), 1)
    if emit_line is not None:
        # Known output goes out now, so each source preview lands before its own question.
        for line in lines:
            emit_line(line)
        lines = []
    source_lines, source_exit = offer_sources(
        config_path,
        confirm=source_confirm or confirm,
        detect_fn=source_detect or detect_report,
        retention_days=config.usage.retention_days if config.usage else None,
        emit=emit_line,
    )
    if emit_line is None:
        lines.extend(source_lines)
    if source_exit:
        return SetupReport(tuple(lines), source_exit)
    if source_lines and source_exit == 0:
        # The file may have changed: the rest of setup uses the configuration as it is now.
        config = load_config(
            config_path, resolve_origin=lambda port: tailscale.private_origin(port, runner)
        )
    limits_lines, limits_exit = offer_limits(
        config_path,
        confirm=source_confirm or confirm,
        detect_fn=source_detect or detect_report,
        emit=emit_line,
    )
    if emit_line is None:
        lines.extend(limits_lines)
    if limits_exit:
        return SetupReport(tuple(lines), limits_exit)
    if limits_lines:
        config = load_config(
            config_path, resolve_origin=lambda port: tailscale.private_origin(port, runner)
        )
    hooks = _limits_hook_setup(
        config.usage.sources if config.usage else (), explicit=install_limits_hook, confirm=confirm
    )
    lines.extend(hooks.lines)
    if hooks.exit_code:
        return SetupReport(tuple(lines), hooks.exit_code)
    if not config.agent_kinds:
        # setup never guesses or detects kinds; the owner adds them.
        lines.append(f"Add agent_kinds to {config_path} to allow starting agents from RelayDeck")
    socket = Path(config.herdr_socket).expanduser()
    if socket.exists():
        lines.append(f"Herdr socket present: {socket} (protocol is checked when start runs).")
    else:
        lines.append(f"Herdr socket missing: {socket}. Start Herdr separately, then re-run setup.")
        exit_code = 1
    try:
        route = tailscale.PrivateRoute(config.tailscale_https_port, config.port, runner)
        configured_proxy, public = route.inspect()
        target = route.target
        if public or (configured_proxy is not None and configured_proxy != target):
            lines.append(prereqs.GUIDANCE["serve_funnel" if public else "serve_conflict"])
            return SetupReport(tuple(lines), 1)
        port = choose_https_port(config.tailscale_https_port, set(), set())
        if port is None:
            lines.append(
                "Configured HTTPS port is reserved; explicitly change tailscale_https_port before "
                "setup. No Serve change was made."
            )
            return SetupReport(tuple(lines), 1)
        if configured_proxy == target:
            lines.append(
                f"HTTPS port {config.tailscale_https_port} already maps to this companion; "
                "nothing to apply."
            )
        if configured_proxy != target:
            command = f"tailscale serve --https={port} --bg {target}"
            lines.append(f"Serve command (tailnet-only, no Funnel): {command}")
            if assume_yes or confirm("Apply this serve mapping now? [y/N] "):
                route.ensure()  # Recheck after the prompt; never overwrite a raced mapping.
                lines.append(
                    "Serve mapping applied."
                    if route.created
                    else "Serve already maps to this companion; nothing to apply."
                )
            else:
                lines.append(
                    "Not applied. Re-run setup with --yes, or run the command above yourself."
                )
    except tailscale.TailscaleError:
        lines.append(
            "Tailscale Serve could not be confirmed. Recheck its status and configured HTTPS port "
            "before retrying."
        )
        exit_code = 1
    lines.append(
        "Connector enrollment is not performed here: follow the README (Install, step 3) once, "
        "with the owner's verified public key."
    )
    if probe is not None:
        lines.append("Running doctor --probe-private:")
        exit_code = exit_code or probe()
    return SetupReport(tuple(lines), exit_code)


# -- add detected CLIs as usage sources, each with the owner's consent ----------

SOURCE_HISTORY_DEFAULT_DAYS = 90
RECEIPT_DIR = "setup-receipts"


@dataclass(frozen=True)
class SourceCandidate:
    kind: str
    display_name: str
    format: str
    root: str  # absolute, as written to the file
    root_display: str  # with ~ for the home directory, as shown


def _tilde(path: str, home: str) -> str:
    return "~" + path[len(home) :] if home and path.startswith(home) else path


def source_candidates(report: DetectReport, home: str) -> tuple[list[SourceCandidate], list[str]]:
    """The CLIs that can be added (installed, with a reader format, not yet configured), and the
    CLIs that are installed but have no reader yet (listed only; never written)."""
    candidates: list[SourceCandidate] = []
    unsupported: list[str] = []
    for row in report.rows:
        if (
            row.next_step == "can be added"
            and row.reader_root is not None
            and os.path.isdir(row.reader_root)
            and row.adapter != "not yet"
        ):
            root = row.reader_root
            candidates.append(
                SourceCandidate(
                    kind=row.id,
                    display_name=row.display_name,
                    format=row.adapter,
                    root=root,
                    root_display=_tilde(root, home),
                )
            )
        elif row.next_step == "needs an adapter (not supported yet)":
            unsupported.append(row.display_name)
    return candidates, unsupported


def _preview(candidate: SourceCandidate, retention_days: int) -> list[str]:
    return [
        f"{candidate.display_name} ({candidate.kind})",
        f"  kind:    {candidate.kind}",
        f"  format:  {candidate.format}",
        f"  root:    {candidate.root_display}",
        f"  history: up to {retention_days} days",
        "  reads:   token counts and timestamps only; prompts and file contents are never",
        "           stored or sent",
    ]


def _receipt(
    path: Path, stamp: str, action: str, entries: list[dict[str, str]], backup: Path
) -> Path:
    directory = path.parent / RECEIPT_DIR
    directory.mkdir(mode=0o700, exist_ok=True)
    receipt = directory / f"sources-{stamp}-{action}.json"
    body = {
        "action": action,
        "time": stamp,
        "backup": backup.name,  # the file name only; the file itself is owner-only
        "sources": entries,  # kind, format and a ~ root: no secret values
    }
    fd = os.open(receipt, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        import json

        json.dump(body, stream, indent=2, sort_keys=True)
        stream.write("\n")
    return receipt


def _stamp() -> str:
    """Unique per write: two writes in one second must not collide on a backup name."""
    import time

    now = time.time()
    return time.strftime("%Y%m%dT%H%M%S", time.gmtime(now)) + f"{int((now % 1) * 1_000_000):06d}"


def apply_text(
    path: Path, expected_text: str, new_text: str, *, action: str, entries: list[dict[str, str]]
) -> list[str]:
    """The only write: concurrent-edit check, private backup, verify, atomic replace, receipt.
    Any failure leaves the original file as it was."""
    from relaydeck.companion.private import check_private, read_private

    try:
        check_private(path)
        current = read_private(path)
    except (ValueError, OSError):
        return [
            "The configuration changed or became unreadable; no source was written.",
            "Run relaydeck setup again to review it.",
        ]
    if config_edit.sha256_of(current) != config_edit.sha256_of(expected_text):
        return [
            "The configuration changed while setup was running; no source was written.",
            "Run relaydeck setup again to review the current file.",
        ]
    stamp = _stamp()
    temp = path.with_name(f".{path.name}.check-{stamp}")
    try:
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(new_text)
        load_config(temp)  # the whole file must still load before anything is replaced
    except (ValueError, OSError):
        return ["The new configuration does not validate; no source was written."]
    finally:
        if temp.exists():
            temp.unlink()
    backup_name = f"{path.name}.bak-{stamp}"
    try:
        config_edit.replace_if_match(path, expected_text, new_text, backup_name=backup_name)
    except config_edit.ConcurrentEdit:
        return [
            "The configuration changed while setup was running; no source was written.",
            "Run relaydeck setup again to review the current file.",
        ]
    except config_edit.ConfigLocked:
        return [
            "The configuration is locked by another writer; no source was written.",
            "Stop other writers and run relaydeck setup again.",
        ]
    except config_edit.CommitUncertain:
        return [
            "The configuration was changed, but the directory sync failed; "
            f"the previous file is kept at {backup_name}.",
            "Inspect the file and the backup before retrying; nothing was restored automatically.",
        ]
    except OSError:
        return ["The configuration could not be replaced; the original file is unchanged."]
    backup = path.with_name(backup_name)
    try:
        receipt = _receipt(path, stamp, action, entries, backup)
    except OSError as exc:
        return [
            f"Backup kept (owner-only): {backup.name}",
            f"Sources were written, but the receipt failed ({exc}); "
            "the backup above holds the previous file.",
        ]
    return [f"Backup kept (owner-only): {backup.name}", f"Receipt written: {receipt.name}"]


def offer_sources(
    config_path: Path,
    *,
    confirm: Callable[[str], bool],
    detect_fn: Callable[[Path], DetectReport],
    retention_days: int | None = None,
    emit: Callable[[str], None] | None = None,
) -> tuple[list[str], int]:
    """The interactive source step: one question per CLI, default No.

    `--yes` never reaches this step.
    """
    home = os.path.expanduser("~")
    report = detect_fn(config_path)
    candidates, unsupported = source_candidates(report, home)
    lines: list[str] = []

    def say(line: str) -> None:
        lines.append(line)
        if emit is not None:
            emit(line)

    if unsupported:
        say("Not supported yet (never written): " + ", ".join(unsupported))
    if not candidates:
        say("Usage sources: nothing to add.")
        return lines, 0
    days = retention_days or SOURCE_HISTORY_DEFAULT_DAYS
    try:
        text = config_path.read_text(encoding="utf-8")
        present = _present_sources(text)
    except (OSError, ValueError):
        say("Usage sources: the configuration could not be read; nothing added.")
        return lines, 1
    candidates = [c for c in candidates if (c.kind, c.format) not in present]
    if not candidates:
        say("Usage sources: nothing to add.")
        return lines, 0
    approved: list[SourceCandidate] = []
    for candidate in candidates:
        for line in _preview(candidate, days):
            say(line)
        if confirm(f"Add {candidate.display_name}? [y/N] "):
            approved.append(candidate)
            say("  -> approved")
        else:
            say("  -> not added")
    if not approved:
        return lines, 0
    entries = [config_edit.SourceEntry(c.kind, c.format, c.root, c.display_name) for c in approved]
    new_text = config_edit.append_sources(text, entries)
    result = apply_text(
        config_path,
        text,
        new_text,
        action="add",
        entries=[{"kind": c.kind, "format": c.format, "root": c.root_display} for c in approved],
    )
    failed = any("no source was written" in line or "could not" in line for line in result)
    for line in result:
        say(line)
    return lines, (1 if failed else 0)


def _present_sources(text: str) -> set[tuple[str, str]]:
    """(kind, format) of every usage source already in the file: a rerun never duplicates one."""
    import tomllib

    rows = (tomllib.loads(text).get("usage") or {}).get("sources") or []
    return {(row.get("kind"), row.get("format")) for row in rows}


# -- offer on-demand [[usage.limits]] blocks with the owner's consent -------


@dataclass(frozen=True)
class LimitsCandidate:
    kind: str
    display_name: str
    format: str


def limits_candidates(report: DetectReport, source_kinds: set[str]) -> list[LimitsCandidate]:
    """Installed CLIs with a TUI limits recipe whose kind is already a usage
    source. The tool binary must be present (its screen is read); the block
    itself resolves Herdr at runtime."""
    candidates: list[LimitsCandidate] = []
    for row in report.rows:
        if row.limits == "tui-usage-pane-v1" and row.binary is not None and row.id in source_kinds:
            candidates.append(
                LimitsCandidate(kind=row.id, display_name=row.display_name, format=row.limits)
            )
    return candidates


def _preview_limits(candidate: LimitsCandidate) -> list[str]:
    return [
        f"{candidate.display_name} ({candidate.kind}) plan limits",
        f"  kind:    {candidate.kind}",
        f"  format:  {candidate.format}",
        "  reads:   a manual refresh opens a temporary tool tab, reads the",
        "           /usage screen numbers, then quits and closes the tab. Only",
        "           numbers, reset times and the plan label are kept.",
    ]


def _present_limits(text: str) -> set[tuple[str, str]]:
    """(kind, format) of every limits block already in the file."""
    import tomllib

    rows = (tomllib.loads(text).get("usage") or {}).get("limits") or []
    return {(row.get("kind"), row.get("format")) for row in rows}


def offer_limits(
    config_path: Path,
    *,
    confirm: Callable[[str], bool],
    detect_fn: Callable[[Path], DetectReport],
    emit: Callable[[str], None] | None = None,
) -> tuple[list[str], int]:
    """The interactive limits step: one question per CLI, default No.

    `--yes` never reaches this step.
    """
    report = detect_fn(config_path)
    lines: list[str] = []

    def say(line: str) -> None:
        lines.append(line)
        if emit is not None:
            emit(line)

    try:
        text = config_path.read_text(encoding="utf-8")
        present = _present_limits(text)
        import tomllib

        rows = (tomllib.loads(text).get("usage") or {}).get("sources") or []
        source_kinds = {
            row["kind"]
            for row in rows
            if isinstance(row, dict) and isinstance(row.get("kind"), str)
        }
    except (OSError, ValueError):
        say("Plan limits: the configuration could not be read; nothing added.")
        return lines, 1
    candidates = [
        c for c in limits_candidates(report, source_kinds) if (c.kind, c.format) not in present
    ]
    if not candidates:
        say("Plan limits: nothing to add.")
        return lines, 0
    approved: list[LimitsCandidate] = []
    for candidate in candidates:
        for line in _preview_limits(candidate):
            say(line)
        if confirm(f"Add {candidate.display_name} plan limits? [y/N] "):
            approved.append(candidate)
            say("  -> approved")
        else:
            say("  -> not added")
    if not approved:
        return lines, 0
    entries = [config_edit.LimitsEntry(c.kind, c.format) for c in approved]
    new_text = config_edit.append_limits(text, entries)
    result = apply_text(
        config_path,
        text,
        new_text,
        action="add-limits",
        entries=[{"kind": c.kind, "format": c.format} for c in approved],
    )
    failed = any("no source was written" in line or "could not" in line for line in result)
    for line in result:
        say(line)
    return lines, (1 if failed else 0)


def undo_sources(config_path: Path) -> tuple[list[str], int]:
    """Removes only the sources setup added that are still unchanged. Owner's edits are kept."""
    try:
        text = config_path.read_text(encoding="utf-8")
    except OSError:
        return ["No configuration file: nothing to undo."], 0
    new_text, removed, kept = config_edit.remove_unchanged(text)
    lines = [f"Setup-added sources kept (changed since setup): {', '.join(kept)}"] if kept else []
    if not removed:
        return [*lines, "No unchanged setup-added sources to undo."], 0
    result = apply_text(
        config_path,
        text,
        new_text,
        action="undo",
        entries=[{"kind": kind} for kind in removed],
    )
    failed = any("no source was written" in line or "could not" in line for line in result)
    return [*lines, f"Removed: {', '.join(removed)}", *result], (1 if failed else 0)
