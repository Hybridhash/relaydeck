"""Readiness for `relaydeck setup --check`: ten items in a fixed order, read-only.

Nothing here writes, prompts or starts anything. Every external read is an argument (the
prerequisite
report, the companion state, the installed web bundle, the usage detection report), so tests inject
fakes and the real command uses the same read paths the companion already has. Output never includes
a
secret value, a token, an e-mail address or a full home path: secrets are reported by presence,
length
and permissions only.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from relaydeck import webbundle
from relaydeck.companion import prereqs
from relaydeck.companion.config import CompanionConfig, load_config
from relaydeck.companion.detect import DetectReport
from relaydeck.companion.lifecycle import status as lifecycle_status
from relaydeck.companion.private import check_private, read_private

OK = "ok"
WARN = "warn"
FAIL = "fail"
MIN_SECRET_BYTES = 32
PLACEHOLDER_ORIGIN = "your-mac.your-tailnet.ts.net"
PREREQ_CHECKS = ("tailscale", "herdr", "uv", "python")
PAIRING_ITEMS = range(2, 8)  # items 2 to 7 must all pass before "ready to pair"

TITLES = {
    1: "Prerequisites",
    2: "Configuration file",
    3: "Secrets",
    4: "Connector enrollment",
    5: "Web app",
    6: "Companion running",
    7: "Tailscale Serve",
    8: "Usage sources",
    9: "Limits",
    10: "Notifications",
    11: "Pairing",
}


@dataclass(frozen=True)
class Item:
    number: int
    title: str
    state: str  # OK, WARN or FAIL
    reason: str
    next_step: str = ""


@dataclass(frozen=True)
class Readiness:
    items: tuple[Item, ...]

    @property
    def ready_to_pair(self) -> bool:
        return all(item.state == OK for item in self.items if item.number in PAIRING_ITEMS)

    @property
    def exit_code(self) -> int:
        states = {item.state for item in self.items}
        if FAIL in states:
            return 1
        if WARN in states:
            return 2
        return 0


def _worst(states: list[str]) -> str:
    if FAIL in states:
        return FAIL
    if WARN in states:
        return WARN
    return OK


def _item(number: int, state: str, reason: str, next_step: str = "") -> Item:
    return Item(number, TITLES[number], state, reason, "" if state == OK else next_step)


def _prerequisites(report: prereqs.Report | None) -> Item:
    if report is None:
        return _item(
            1, WARN, "not checked", "run relaydeck setup --check again with Tailscale and Herdr"
        )
    results = [result for result in report.results if result.check in PREREQ_CHECKS]
    states = []
    reasons = []
    steps = []
    for result in results:
        state = (
            OK
            if result.state == prereqs.STATE_OK
            else (WARN if result.state == prereqs.STATE_UNKNOWN else FAIL)
        )
        states.append(state)
        if state != OK:
            reasons.append(f"{result.check}: {result.state.replace('_', ' ')}")
            steps.append(
                result.guidance or "install or start it, then check again (see the runbook)"
            )
    if not results:
        return _item(1, WARN, "not checked", "run relaydeck setup --check again")
    if not reasons:
        return _item(1, OK, "Tailscale, Herdr, uv and Python 3.13 are ready")
    return _item(1, _worst(states), "; ".join(reasons), steps[0] if steps else "")


def _load(config_path: Path) -> tuple[CompanionConfig | None, str]:
    """(config, status). Status: missing, private (not owner-only), template, invalid, ok."""
    if not config_path.exists():
        return None, "missing"
    try:
        check_private(config_path)
    except (ValueError, OSError):
        return None, "private"
    text = read_private(config_path)
    if PLACEHOLDER_ORIGIN in text:
        return None, "template"
    try:
        return load_config(config_path), "ok"
    except (ValueError, OSError):
        return None, "invalid"


def _config_item(config_path: Path, status: str) -> Item:
    if status == "missing":
        return _item(
            2, FAIL, "no configuration file yet", "run relaydeck setup in a terminal to create one"
        )
    if status == "private":
        return _item(
            2,
            FAIL,
            "the configuration file is not owner-only",
            "make it owner-only (chmod 600), then check again",
        )
    if status == "template":
        return _item(
            2,
            FAIL,
            "the template origin is still in place",
            "replace the placeholder origin with your Tailscale HTTPS address",
        )
    if status == "invalid":
        return _item(
            2,
            FAIL,
            "the configuration does not pass validation",
            "run relaydeck-companion doctor, then fix the reported field",
        )
    return _item(2, OK, "present, owner-only and valid")


def _secret_file(config: CompanionConfig, path: Path, label: str) -> tuple[str, str]:
    """(state, reason) for one secret file. Never reads out or prints the value."""
    if not path.exists():
        return FAIL, f"{label} is missing"
    try:
        check_private(path)
    except (ValueError, OSError):
        return FAIL, f"{label} is not owner-only"
    length = len(read_private(path).strip().encode())
    if length < MIN_SECRET_BYTES:
        return FAIL, f"{label} is shorter than {MIN_SECRET_BYTES} bytes"
    return OK, f"{label} is present and owner-only"


def _secrets_item(config: CompanionConfig | None) -> Item:
    if config is None:
        return _item(3, FAIL, "not checked until the configuration is valid", "fix item 2 first")
    results = []
    cookie = _secret_file(config, config.cookie_secret_file, "cookie secret")
    cursor = _secret_file(config, config.cursor_secret_file, "cursor secret")
    results += [cookie, cursor]
    if cookie[0] == OK and cursor[0] == OK and config.secrets()[0] == config.secrets()[1]:
        results.append((FAIL, "cookie and cursor secrets are the same"))
    states = [state for state, _reason in results]
    reasons = [reason for state, reason in results if state != OK]
    if not reasons:
        return _item(3, OK, "cookie and cursor secrets are present and owner-only")
    return _item(
        3,
        _worst(states),
        "; ".join(reasons),
        "create owner-only secret files of at least 32 random bytes (see the companion runbook)",
    )


def _enrollment_item(config: CompanionConfig | None) -> Item:
    if config is None:
        return _item(4, FAIL, "not checked until the configuration is valid", "fix item 2 first")
    path = config.connector_credentials
    if not path.exists():
        return _item(
            4,
            FAIL,
            "this Mac's connector is not enrolled",
            "enroll it with a public-key invitation (docs/runbooks/connector.md)",
        )
    try:
        check_private(path)
        doc: Any = json.loads(read_private(path))
    except (ValueError, OSError):
        return _item(
            4,
            FAIL,
            "the connector credentials are not readable as owner-only JSON",
            "re-enroll the connector (docs/runbooks/connector.md)",
        )
    if not isinstance(doc, dict) or not all(
        isinstance(doc.get(key), str) and doc.get(key)
        for key in ("gateway_url", "host_id", "host_token")
    ):
        return _item(
            4,
            FAIL,
            "the connector credentials are incomplete",
            "re-enroll the connector (docs/runbooks/connector.md)",
        )
    if doc["host_id"] != config.host_id:
        return _item(
            4,
            FAIL,
            "the enrollment belongs to another host id",
            "re-enroll this Mac, or fix host_id in the configuration",
        )
    if str(doc["gateway_url"]).rstrip("/") != config.advertised_origin.rstrip("/"):
        return _item(
            4,
            FAIL,
            "the enrollment points at another origin",
            "re-enroll the connector for this companion's origin",
        )
    return _item(
        4, OK, "matches this companion; the token is accepted only while the companion runs"
    )


def _web_item(config: CompanionConfig | None, web_current: Callable[[], Path | None]) -> Item:
    if config is not None and not config.web_ui:
        return _item(5, OK, "not needed: this companion serves the bridge only")
    if web_current() is None:
        return _item(
            5,
            FAIL,
            "no web app bundle is installed",
            "build the web client and install it: relaydeck web install <bundle-dir>",
        )
    return _item(5, OK, "a verified web bundle is installed")


def _running_item(config: CompanionConfig | None, companion_state: Callable[[Path], str]) -> Item:
    if config is None:
        return _item(
            6,
            FAIL,
            "the companion has no configuration, so it is not running",
            "after item 2 passes, start it: relaydeck-companion start",
        )
    state = companion_state(config.state_dir)
    if state == "running":
        return _item(6, OK, "running and healthy")
    if state == "stopped":
        return _item(6, FAIL, "not running", "start it: relaydeck-companion start")
    if state in ("starting", "unresponsive"):
        return _item(6, WARN, f"{state}", "wait a moment, then check again")
    return _item(6, WARN, "its state could not be read", "run relaydeck-companion status")


def _serve_item(report: prereqs.Report | None) -> Item:
    if report is None:
        return _item(7, WARN, "not checked", "run relaydeck setup --check again")
    results = [result for result in report.results if result.check == "tailscale_serve"]
    if not results:
        return _item(7, WARN, "not checked", "run relaydeck setup --check again")
    result = results[0]
    if result.state == prereqs.STATE_OK:
        return _item(
            7, OK, f"the Serve mapping matches the configured port ({result.detail}); no Funnel"
        )
    state = WARN if result.state == prereqs.STATE_UNKNOWN else FAIL
    step = (
        result.guidance or "make the Serve mapping match this companion (see the companion runbook)"
    )
    return _item(7, state, result.state.replace("_", " "), step)


def _usage_item(config: CompanionConfig | None, usage: DetectReport | None) -> Item:
    sources = config.usage.sources if config is not None and config.usage is not None else ()
    if not sources:
        return _item(8, OK, "no usage sources configured (optional)")
    if usage is None:
        return _item(8, WARN, "not checked", "run relaydeck setup --detect")
    rows = {row.id: row for row in usage.rows}
    states = []
    reasons = []
    for source in sources:
        row = rows.get(source.kind)
        if row is None:
            states.append(WARN)
            reasons.append(f"{source.kind}: not in the catalog")
            continue
        if row.binary is None:
            states.append(WARN)
            reasons.append(f"{row.display_name}: CLI not installed")
        elif not source.root.is_dir():
            states.append(WARN)
            reasons.append(f"{row.display_name}: its log folder is missing")
        else:
            states.append(OK)
    if not reasons:
        return _item(8, OK, f"{len(sources)} usage source(s) readable")
    return _item(
        8, _worst(states), "; ".join(reasons), "run relaydeck setup --detect for each source"
    )


def _limits_item(config: CompanionConfig | None) -> Item:
    count = len(config.usage.limits) if config is not None and config.usage is not None else 0
    sources = config.usage.sources if config is not None and config.usage is not None else ()
    problems = []
    healthy = 0
    from relaydeck.usage.hook_repair import diagnose_source, limits_sources

    for source in limits_sources(sources):
        diagnosis = diagnose_source(source)
        if diagnosis.reason in ("ok", "cache_stale"):
            healthy += 1
        else:
            problems.append(diagnosis)
    if problems:
        first = problems[0]
        return _item(
            9,
            WARN,
            "; ".join(f"{d.kind}: {d.summary}" for d in problems),
            first.fix or "run relaydeck usage doctor",
        )
    if count == 0 and healthy == 0:
        return _item(9, OK, "no limits sources configured (optional)")
    detail = f"{count} limits source(s) configured"
    if healthy:
        detail += f"; {healthy} status-line hook(s) in place"
    return _item(9, OK, detail + "; relaydeck usage doctor shows hook detail")


def _notifications_item(config: CompanionConfig | None) -> Item:
    """Informational: queued and retrying web push messages. It never blocks pairing. Failures are
    logged by the companion with the provider host and status; they are not stored here."""
    if config is None:
        return _item(10, OK, "not checked until the configuration is valid")
    if not (config.web_ui and config.notifications.web_push):
        return _item(10, OK, "web push is off (set [notifications] web_push or the web app)")
    from relaydeck.gateway.webpush_store import outbox_counts

    counts = outbox_counts(config.state_dir)
    if counts is None or counts[0] == 0:
        return _item(10, OK, "no web push messages waiting")
    queued, retrying = counts
    return _item(
        10,
        WARN,
        f"{queued} web push message(s) waiting, {retrying} retrying; "
        "the companion log names the provider host and status",
        "check the companion log; messages expire after one hour",
    )


def _pairing_item(items: tuple[Item, ...]) -> Item:
    blocked = [item.number for item in items if item.number in PAIRING_ITEMS and item.state != OK]
    if not blocked:
        return _item(11, OK, "ready to pair: run relaydeck-companion pairing open")
    return _item(
        11,
        FAIL,
        f"not ready: fix items {', '.join(str(n) for n in blocked)} first",
        "fix the items above, then check again",
    )


def _lifecycle_state(state_dir: Path) -> str:
    try:
        state = lifecycle_status(state_dir).get("state")
    except (OSError, ValueError):
        return "unknown"
    if state == "stopped":
        return "stopped"
    if state in ("starting", "unresponsive"):
        return str(state)
    return "running" if state else "unknown"


def run_check(
    config_path: Path,
    *,
    prereq_report: prereqs.Report | None,
    companion_state: Callable[[Path], str] = _lifecycle_state,
    web_current: Callable[[], Path | None] = lambda: webbundle.current_bundle(),
    usage: DetectReport | None = None,
) -> Readiness:
    """The eleven items. Callers pass the prerequisite report (see prereqs.run_prereqs) and,
    optionally, the
    usage detection report (see detect.run_detect); nothing else is read beyond the files listed
    above."""
    config, status = _load(config_path)
    items = (
        _prerequisites(prereq_report),
        _config_item(config_path, status),
        _secrets_item(config),
        _enrollment_item(config),
        _web_item(config, web_current),
        _running_item(config, companion_state),
        _serve_item(prereq_report),
        _usage_item(config, usage),
        _limits_item(config),
        _notifications_item(config),
    )
    return Readiness(items=(*items, _pairing_item(items)))


def pending_decisions(config_path: Path) -> Readiness:
    """The read-only summary a non-terminal `setup` prints instead of prompting. No probes, no
    writes."""
    return run_check(config_path, prereq_report=None, companion_state=lambda _state_dir: "unknown")


def relative_to_home(path: Path, home: str) -> str:
    """A display path with the home directory shown as ~."""
    text = str(path)
    if home and text.startswith(home):
        return "~" + text[len(home) :]
    return text


__all__ = [
    "FAIL",
    "OK",
    "WARN",
    "Item",
    "Readiness",
    "pending_decisions",
    "relative_to_home",
    "run_check",
]
