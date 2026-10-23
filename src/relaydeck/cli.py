"""`relaydeck`: one entry point for the Mac companion and its install tools.

Companion commands (start, stop, status, doctor, pairing) are delegated unchanged to
`relaydeck.companion.cli`, so their behaviour and options stay the companion's. The install
commands (setup, service, web) live here. `relaydeck-companion` and the gateway/connector
scripts are unchanged.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from relaydeck import webbundle
from relaydeck.companion import cli as companion_cli
from relaydeck.companion import prereqs, readiness, tailscale
from relaydeck.companion import service as service_agent
from relaydeck.companion import setup as setup_flow
from relaydeck.companion.config import DEFAULT_CONFIG, load_config
from relaydeck.companion.detect import DetectReport
from relaydeck.companion.setup import detect_report, run_setup
from relaydeck.version import __version__

COMPANION_COMMANDS = ("start", "stop", "status", "doctor", "pairing", "usage")
INSTALL_COMMANDS = ("setup", "service", "web")


def _first_command(argv: Sequence[str]) -> str | None:
    """The first positional token, skipping `--config` and its value."""
    skip_value = False
    for token in argv:
        if skip_value:
            skip_value = False
            continue
        if token == "--config":  # noqa: S105 - CLI flag, not a credential
            skip_value = True
            continue
        if token.startswith("-"):
            continue
        return token
    return None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="relaydeck",
        description=(
            "RelayDeck Mac companion and install tools. Companion commands: "
            + ", ".join(COMPANION_COMMANDS)
            + " (see relaydeck-companion --help)."
        ),
    )
    parser.add_argument("--config", default=DEFAULT_CONFIG, help="Owner-only companion TOML")
    parser.add_argument("--version", action="version", version=f"relaydeck {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)
    setup = sub.add_parser("setup", help="First-run checks; creates the owner-only config")
    setup.add_argument(
        "--install-limits-hook",
        action="store_true",
        help="Separately authorize limits-hook install",
    )
    setup.add_argument("--yes", action="store_true", help="Apply the serve mapping without asking")
    setup.add_argument(
        "--detect",
        action="store_true",
        help="Read-only: list the agent CLIs, their states and next steps; writes nothing",
    )
    setup.add_argument(
        "--check",
        action="store_true",
        help="Read-only readiness report: what is ready, what is not, and the next step",
    )
    setup.add_argument(
        "--undo-sources",
        action="store_true",
        help="Remove only the usage sources setup added that are still unchanged",
    )
    setup.add_argument("--no-probe", action="store_true", help="Skip doctor --probe-private")
    agent = sub.add_parser("service", help="Opt-in per-user launchd agent")
    agent_actions = agent.add_subparsers(dest="action", required=True)
    install = agent_actions.add_parser("install", help="Write the LaunchAgent (needs --yes)")
    install.add_argument("--yes", action="store_true")
    install.add_argument("--load", action="store_true", help="Also bootstrap it (needs --yes)")
    uninstall = agent_actions.add_parser("uninstall", help="Remove the LaunchAgent (needs --yes)")
    uninstall.add_argument("--yes", action="store_true")
    uninstall.add_argument("--unload", action="store_true", help="Also bootout (needs --yes)")
    agent_actions.add_parser("status", help="Read the LaunchAgent file (no launchctl)")
    web = sub.add_parser("web", help="Installed web bundles")
    web_actions = web.add_subparsers(dest="action", required=True)
    bundle = web_actions.add_parser("install", help="Copy and verify a built web bundle")
    bundle.add_argument("dir", help="Built web directory (holds main.dart.js)")
    web_actions.add_parser("list", help="Installed bundles; the newest verified is current")
    for name in COMPANION_COMMANDS:
        sub.add_parser(name, help="Companion command (delegated; see relaydeck-companion --help)")
    return parser


def _emit_line(line: str) -> None:
    """Prints one setup line at once: a source preview lands right before its own question."""
    print(line, flush=True)


def _confirm(prompt: str) -> bool:
    try:
        return input(prompt).strip().lower() in ("y", "yes")
    except EOFError:
        return False


def _yes_no(value: bool | None) -> str:
    return "unknown" if value is None else ("yes" if value else "no")


def detect_lines(report: DetectReport) -> list[str]:
    """The `setup --detect` table, at most 100 columns: one row per CLI, its next step below it.

    The states are kept apart: Herdr detects, Herdr hook, installed, adapter, configured.
    Printing only.
    """
    lines = [
        f"{'CLI':<13} {'Detects':<7} {'Hook':<14} {'Installed':<9} {'Adapter':<32} "
        f"{'Configured':<10}",
    ]
    for row in report.rows:
        lines.append(
            f"{row.id:<13} {_yes_no(row.herdr_detects):<7} {row.herdr_hook:<14} "
            f"{_yes_no(row.binary is not None):<9} {row.adapter:<32} "
            f"{_yes_no(row.configured):<10}"
        )
        lines.append(f"  -> {row.next_step}")
    lines.extend(
        [
            "Legend:",
            "  Detects  Herdr has a detection manifest for the CLI.",
            "  Hook     Herdr integration hook: current, outdated, not installed.",
            "           none = Herdr has no hook for it; unknown = Herdr status unavailable.",
            "  Adapter  RelayDeck usage reader format, or not yet.",
        ]
    )
    counts = {
        step: sum(1 for row in report.rows if row.next_step == step)
        for step in sorted({row.next_step for row in report.rows})
    }
    lines.append("Summary: " + ", ".join(f"{count} {step}" for step, count in counts.items()))
    for note in report.notes:
        lines.append(note)
    if report.config_status != "ok":
        lines.append(f"Configuration: {report.config_status} (configured column is unknown)")
    lines.append("Nothing was written. Run `relaydeck setup` to add sources, with your consent.")
    return lines


_SYMBOL = {readiness.OK: "✓", readiness.WARN: "!", readiness.FAIL: "✗"}


def readiness_lines(report: readiness.Readiness, home: str) -> list[str]:
    """The `setup --check` report: one line per item, its next step below it. Printing only; every
    path
    is shown with ~ for the home directory."""
    lines: list[str] = []
    for item in report.items:
        lines.append(f"{_SYMBOL[item.state]} {item.number:>2}. {item.title}: {item.reason}")
        if item.next_step:
            lines.append(f"       next: {item.next_step}")
    lines.append(
        "Ready to pair."
        if report.ready_to_pair
        else "Not ready to pair yet; fix the items marked above."
    )
    return [line.replace(home, "~") if home else line for line in lines]


def _check_report(config_path: Path) -> readiness.Readiness:
    """The real read-only readiness: the prerequisite probes, the Serve mapping, the companion
    state, the
    web bundle and the usage detection. The same reads the companion and the detection already
    use."""
    try:
        config = load_config(config_path)
    except (OSError, ValueError):
        config = None
    socket = prereqs.probe_socket(config.herdr_socket if config else None)
    https_port = config.tailscale_https_port if config else 8445
    target = None
    if config is not None:
        target = tailscale.PrivateRoute(
            config.tailscale_https_port, config.port, tailscale.default_runner
        ).target
    report = prereqs.run_prereqs(
        socket=socket,
        https_port=https_port,
        probes=prereqs.default_probes(),
        expected_target=target,
    )
    usage = detect_report(config_path) if config is not None and config.usage is not None else None
    return readiness.run_check(config_path, prereq_report=report, usage=usage)


def _run_install(args: argparse.Namespace) -> int:
    config_path = Path(args.config).expanduser().absolute()
    home_text = os.path.expanduser("~")
    if args.command == "setup" and getattr(args, "detect", False):
        for line in detect_lines(detect_report(config_path)):
            print(line)
        return 0
    if args.command == "setup" and getattr(args, "check", False):
        check = _check_report(config_path)
        print(f"relaydeck {__version__}")
        print("\n".join(readiness_lines(check, home_text)))
        return check.exit_code
    if args.command == "setup" and getattr(args, "undo_sources", False):
        undo_lines, undo_code = setup_flow.undo_sources(config_path)
        print("\n".join(undo_lines))
        return undo_code
    if args.command == "setup" and not sys.stdin.isatty():
        # No terminal means no prompt can be answered; --yes does not change this.
        # Nothing is written.
        pending = readiness.pending_decisions(config_path)
        print("\n".join(readiness_lines(pending, home_text)))
        candidates, _unsupported = setup_flow.source_candidates(
            detect_report(config_path), home_text
        )
        if candidates:
            print("Pending source choices (answer them in a terminal):")
            for candidate in candidates:
                print(
                    f"  - {candidate.display_name} ({candidate.kind}) at {candidate.root_display}"
                )
        print("Pending decisions: run relaydeck setup in a terminal to answer its prompts.")
        print("Nothing was written.")
        return 1
    if args.command == "setup":

        def probe() -> int:
            return companion_cli.main(["--config", str(config_path), "doctor", "--probe-private"])

        report = run_setup(
            config_path=config_path,
            assume_yes=args.yes,
            install_limits_hook=args.install_limits_hook,
            confirm=_confirm,
            probe=None if args.no_probe else probe,
            emit_line=_emit_line,
        )
        if report.lines:
            print("\n".join(report.lines), flush=True)
        return report.exit_code
    if args.command == "service":
        home = Path.home()
        uid = os.getuid()
        if args.action == "status":
            print(json.dumps(service_agent.status(home), indent=2, sort_keys=True))
            return 0
        if args.action == "install":
            executable = Path(sys.argv[0]).expanduser().resolve()
            outcome = service_agent.install(
                executable=executable, home=home, uid=uid, yes=args.yes, load=args.load
            )
        else:
            outcome = service_agent.uninstall(home=home, uid=uid, yes=args.yes, unload=args.unload)
        print("\n".join(outcome.lines))
        return outcome.exit_code
    if args.action == "list":
        rows = webbundle.list_bundles()
        if not rows:
            print("No installed web bundles.")
        for row in rows:
            flag = (
                "current" if row["current"] else ("verified" if row["verified"] else "unverified")
            )
            print(f"{row['build_id']}  {flag}  installed {row['installed_at']}  {row['path']}")
        return 0
    try:
        record = webbundle.install(Path(args.dir))
    except webbundle.BundleError as exc:
        print(f"web install refused: {exc}")
        return 1
    print(f"Installed {record.build_id} at {record.path} (repair markers verified on the copy).")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args_list = list(sys.argv[1:] if argv is None else argv)
    if _first_command(args_list) in COMPANION_COMMANDS:
        return companion_cli.main(args_list)
    args = build_parser().parse_args(args_list)
    if args.command in INSTALL_COMMANDS:
        return _run_install(args)
    return companion_cli.main(args_list)


if __name__ == "__main__":
    raise SystemExit(main())
