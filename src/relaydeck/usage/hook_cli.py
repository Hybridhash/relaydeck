"""Standalone usage-hook CLI; the companion CLI wires it separately.

python -m relaydeck.usage.hook_cli --config <private TOML> doctor
Install requires --install-limits-hook; --yes from Serve is never consulted.
"""

from __future__ import annotations

import argparse
import json
import sys
import tomllib
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from relaydeck.usage import hook_files as fs
from relaydeck.usage.contracts import MAX_SOURCES
from relaydeck.usage.hook_doctor import HookSource, doctor_sources
from relaydeck.usage.hook_install import install_hook, uninstall_hook
from relaydeck.usage.probe_setup import (
    NOT_TRUSTED,
    ProbeTarget,
    ask_on_terminal,
    doctor_line,
    load_probe_targets,
    offer_trust,
    probe_state,
)
from relaydeck.usage.readers import READERS


@dataclass(frozen=True)
class ConfiguredHookSource:
    kind: str
    format: str
    limits_format: str | None
    limits_settings: Path | None
    limits_cache: Path | None


def _configured_path(row: dict[str, Any], key: str, *, required: bool = False) -> Path | None:
    value = row.get(key)
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value:
        raise fs.HookFileError("invalid")
    result = Path(value)
    fs.absolute(result)
    if not result.name:
        raise fs.HookFileError("invalid")
    return result


def load_hook_sources(path: Path) -> tuple[ConfiguredHookSource, ...]:
    """Read only hook metadata; full config loading may invoke auto-origin probes.

    Transcript-root/infra readiness is deliberately independent of hook diagnosis.
    Filesystem safety of settings/cache is reported by the installer/doctor itself.
    """
    from relaydeck.companion.config import USAGE_KEYS, USAGE_SOURCE_KEYS, agent_kinds_from

    with fs.directory(path.parent) as fd:
        image = fs.read(fd, path.name, private=True)
    if image.raw is None:
        raise fs.HookFileError("invalid")
    body = tomllib.loads(image.raw.decode())
    kinds = agent_kinds_from(body.get("agent_kinds", []))
    usage = body.get("usage")
    if usage is None:
        return ()
    if not isinstance(usage, dict) or set(usage) - USAGE_KEYS:
        raise fs.HookFileError("invalid")
    rows = usage.get("sources")
    if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_SOURCES:
        raise fs.HookFileError("invalid")
    sources: list[ConfiguredHookSource] = []
    keys: set[tuple[str, str]] = set()
    caches: set[Path] = set()
    limits_kinds: set[str] = set()
    for row in rows:
        if not isinstance(row, dict) or set(row) - USAGE_SOURCE_KEYS:
            raise fs.HookFileError("invalid")
        kind, format_id = row.get("kind"), row.get("format")
        if (
            not isinstance(kind, str)
            or kind not in kinds
            or not isinstance(format_id, str)
            or format_id not in READERS
            or (kind, format_id) in keys
        ):
            raise fs.HookFileError("invalid")
        keys.add((kind, format_id))
        limits = row.get("limits_format")
        if limits != "json-statusline-limits-v1":
            if "limits_settings" in row or "limits_cache" in row:
                raise fs.HookFileError("invalid")
            continue

        settings = _configured_path(row, "limits_settings")
        cache = _configured_path(row, "limits_cache", required=True)
        assert cache is not None
        if kind in limits_kinds or cache in caches:
            raise fs.HookFileError("invalid")
        limits_kinds.add(kind)
        caches.add(cache)
        sources.append(ConfiguredHookSource(kind, format_id, limits, settings, cache))
    return tuple(sources)


def add_usage_parser(subparsers: Any) -> None:
    usage = subparsers.add_parser("usage", help="Offline limits-hook install/uninstall/doctor")
    add_actions(usage.add_subparsers(dest="usage_action", required=True))


def add_actions(subparsers: Any) -> None:
    subparsers.add_parser(
        "doctor", help="Read immediate installation/cache evidence; no probes or writes"
    )
    install = subparsers.add_parser(
        "install", help="Stop other settings writers; JSON will be reformatted"
    )
    install.add_argument(
        "--source", help="Configured kind/format; no directory discovery. Omit for probe folders"
    )
    install.add_argument(
        "--install-limits-hook", action="store_true", help="Separately authorize hook installation"
    )
    install.add_argument(
        "--create-settings", action="store_true", help="Also authorize creating missing settings"
    )
    install.add_argument(
        "--replace-statusline",
        action="store_true",
        help="Also replace a custom field, never chain it",
    )
    repair = subparsers.add_parser(
        "repair",
        help="Put back only RelayDeck's settings field where it went missing; asks first",
    )
    repair.add_argument("--source", help="One configured kind/format; default: every source")
    repair.add_argument("--yes", action="store_true", help="Apply without asking")
    repair.add_argument(
        "--create-settings", action="store_true", help="Also authorize creating missing settings"
    )
    repair.add_argument(
        "--replace-statusline",
        action="store_true",
        help="Also replace a status line you configured yourself",
    )
    uninstall = subparsers.add_parser(
        "uninstall", help="Restore only the compared managed field; preserve later edits"
    )
    uninstall.add_argument("--source", required=True, help="Configured kind/format")


def run_usage(
    args: argparse.Namespace,
    sources: Iterable[HookSource],
    *,
    hooks_dir: Path | None = None,
    probes: Iterable[ProbeTarget] = (),
) -> int:
    entries = tuple(sources)
    targets = tuple(probes)
    if args.usage_action == "doctor":
        report: dict[str, Any] = {"sources": doctor_sources(entries, hooks_dir=hooks_dir)}
        if targets:
            states = [(t, probe_state(t)) for t in targets]
            report["probe_folders"] = [
                {"tool": t.kind, "path": str(t.folder), "state": state} for t, state in states
            ]
            for t, state in states:
                print(doctor_line(t, state), file=sys.stderr)
        print(json.dumps(report, indent=2))
        return 0  # Diagnostic states are data, not infrastructure/prerequisite probes.
    if args.usage_action == "repair":
        code = _run_repair(args, entries, hooks_dir)
        for t in targets:
            if probe_state(t) == NOT_TRUSTED:
                offer_trust(t, ask=ask_on_terminal)
        return code
    if args.usage_action == "install":
        for t in targets:
            offer_trust(t, ask=ask_on_terminal)
        if args.source is None:
            if targets:
                return 0
            print(json.dumps({"state": "not_configured"}))
            return 1
    selected = [s for s in entries if f"{s.kind}/{s.format}" == args.source]
    if (
        len(selected) != 1
        or selected[0].limits_format != "json-statusline-limits-v1"
        or selected[0].limits_settings is None
        or selected[0].limits_cache is None
    ):
        # A tool that has only a probe folder has no hook to install.
        probe_only = len(selected) == 1 and any(t.kind == selected[0].kind for t in targets)
        if probe_only:
            return 0
        print(json.dumps({"state": "not_configured"}))
        return 1
    source = selected[0]
    assert source.limits_settings is not None and source.limits_cache is not None
    if args.usage_action == "install":
        result = install_hook(
            source.limits_settings,
            source.limits_cache,
            confirmed=args.install_limits_hook,
            create_settings=args.create_settings,
            replace_statusline=args.replace_statusline,
            hooks_dir=hooks_dir,
        )
    else:
        result = uninstall_hook(
            source.limits_settings, source.limits_cache, confirmed=True, hooks_dir=hooks_dir
        )
    print(json.dumps({"kind": source.kind, "format": source.format, **asdict(result)}))
    return 0 if result.state in ("installed", "uninstalled", "not_installed") else 1


def _ask(diagnoses: Any) -> bool:
    """The single confirmation: print each planned change on stderr, then ask once."""
    import sys

    for d in diagnoses:
        print(f"{d.kind}/{d.format}: {d.summary}", file=sys.stderr)
        print(f"  will: {d.change}", file=sys.stderr)
    if not sys.stdin.isatty():
        return False
    print("Apply these changes? [y/N] ", end="", file=sys.stderr, flush=True)
    return sys.stdin.readline().strip().lower() in ("y", "yes")


def _run_repair(
    args: argparse.Namespace, entries: tuple[HookSource, ...], hooks_dir: Path | None
) -> int:
    from relaydeck.usage.hook_repair import repair_sources

    selected = [s for s in entries if args.source is None or f"{s.kind}/{s.format}" == args.source]
    if args.source is not None and not selected:
        print(json.dumps({"state": "not_configured"}))
        return 1
    outcomes = repair_sources(
        selected,
        confirm=(lambda _planned: True) if args.yes else _ask,
        create_settings=args.create_settings,
        replace_statusline=args.replace_statusline,
        hooks_dir=hooks_dir,
    )
    print(json.dumps({"repairs": [o.public() for o in outcomes]}, indent=2))
    pending = {
        "needs_consent",
        "not_repairable",
        "confirmation_declined",
        "failed",
        "changed_since_diagnosis",
    }
    return 1 if any(o.action in pending for o in outcomes) else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True, help="Explicit owner-configured TOML")
    add_actions(parser.add_subparsers(dest="usage_action", required=True))
    args = parser.parse_args(argv)
    return run_configured_usage(args)


def run_configured_usage(args: argparse.Namespace) -> int:
    """Companion alias must call this BEFORE loading full config/preflight/secrets."""
    try:
        sources = load_hook_sources(Path(args.config).expanduser().absolute())
    except (OSError, ValueError, TypeError, RecursionError, fs.HookFileError):
        print(json.dumps({"state": "config_unavailable"}))
        return 2
    return run_usage(args, sources, probes=load_probe_targets(Path(args.config).expanduser()))


if __name__ == "__main__":
    raise SystemExit(main())
