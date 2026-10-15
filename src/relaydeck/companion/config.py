"""Mac private profile. Configuration references pre-provisioned secret files."""

from __future__ import annotations

import logging
import os
import re
import stat
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from relaydeck.companion.private import read_private
from relaydeck.contracts.catalog import AGENT_KIND, MAX_AGENT_KINDS
from relaydeck.contracts.endpoints import origin

DEFAULT_STATE = "~/Library/Application Support/RelayDeck"
DEFAULT_CONFIG = "~/.config/relaydeck/companion.toml"
LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class UsageSourceConfig:
    kind: str
    format: str
    root: Path
    display_name: str
    limits_format: str | None = None
    limits_settings: Path | None = None
    limits_cache: Path | None = None


@dataclass(frozen=True)
class UsageLimitsConfig:
    """One owner-configured `[[usage.limits]]` entry for the limits plane."""

    kind: str
    format: str
    command: tuple[str, ...]
    min_interval_seconds: int = 300
    timeout_seconds: int = 15
    enabled: bool = True
    # manual-only blocks are never time-due (min_interval ignored).
    on_demand: bool = False


@dataclass(frozen=True)
class UsageConfig:
    timezone: str
    sources: tuple[UsageSourceConfig, ...]
    price_tables: tuple[Path, ...] = ()
    retention_days: int = 90
    limits: tuple[UsageLimitsConfig, ...] = ()
    inferred_linking: bool = True


_LIMITS_FORMAT = re.compile(r"[A-Za-z0-9_.-]+")
_MAX_LIMITS_ARGS = 64
_MAX_LIMITS_ARG_CHARS = 4096


def _limits_command(value: Any) -> tuple[str, ...]:
    """Fixed argv for a limits provider: a list (never a shell string) whose
    executable is absolute, exists, is a regular file and is executable.
    Symlinks are followed (owner toolchains link their binaries)."""
    if not isinstance(value, list) or not 1 <= len(value) <= _MAX_LIMITS_ARGS:
        raise ValueError
    args: list[str] = []
    for item in value:
        if (
            not isinstance(item, str)
            or not 1 <= len(item) <= _MAX_LIMITS_ARG_CHARS
            or any(ord(c) < 32 for c in item)
        ):
            raise ValueError
        args.append(item)
    exe = Path(args[0])
    if not exe.is_absolute() or ".." in exe.parts:
        raise ValueError
    try:
        info = exe.stat()
    except OSError:
        raise ValueError from None
    if not stat.S_ISREG(info.st_mode) or not os.access(exe, os.X_OK):
        raise ValueError
    return tuple(args)


def _local_timezone() -> str:
    candidates = [os.environ.get("TZ", "").removeprefix(":")]
    try:
        candidates.append(str(Path("/etc/localtime").resolve()).split("zoneinfo/", 1)[1])
    except (OSError, IndexError):
        pass
    for candidate in candidates:
        try:
            return ZoneInfo(candidate).key
        except (ValueError, ZoneInfoNotFoundError):
            continue
    return "UTC"  # No identifiable system IANA zone; never guess a fixed-offset zone.


def _usage_path(value: Any, *, directory: bool = False, private: bool = False) -> Path:
    if not isinstance(value, str) or not value:
        raise ValueError
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError
    if directory or path.exists():
        info = path.stat()
        valid = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
        if (
            not valid
            or info.st_uid != os.getuid()
            or (private and (info.st_mode & 0o077 or info.st_nlink != 1))
        ):
            raise ValueError
    if private and path.parent.exists():
        info = path.parent.stat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError
    return path


# One definition of the owner-editable [usage] keys. usage_from and the hook CLI both read these,
# so a key added to the config model cannot be rejected by one reader and accepted by the other.
USAGE_KEYS = frozenset(
    {"retention_days", "timezone", "price_tables", "sources", "limits", "inferred_linking"}
)
USAGE_SOURCE_KEYS = frozenset(
    {"kind", "format", "root", "display_name", "limits_format", "limits_settings", "limits_cache"}
)
USAGE_LIMITS_KEYS = frozenset(
    {
        "kind",
        "format",
        "command",
        "min_interval_seconds",
        "timeout_seconds",
        "enabled",
        "on_demand",
    }
)


def usage_from(value: Any, kinds: tuple[str, ...]) -> UsageConfig | None:
    """Optional collection fails closed independently of the companion profile."""
    if value is None:
        return None
    try:
        from relaydeck.usage.contracts import MAX_KEY_CHARS, MAX_SOURCES, RETENTION_DAYS
        from relaydeck.usage.limits import LIMITS_READERS
        from relaydeck.usage.readers import READERS

        if not isinstance(value, dict) or set(value) - USAGE_KEYS:
            raise ValueError
        retention = value.get("retention_days", RETENTION_DAYS)
        if type(retention) is not int or retention != RETENTION_DAYS:
            raise ValueError
        timezone = value.get("timezone", _local_timezone())
        if not isinstance(timezone, str):
            raise ValueError
        ZoneInfo(timezone)
        tables = value.get("price_tables", [])
        if not isinstance(tables, list) or len(tables) > 128:
            raise ValueError
        paths = tuple(_usage_path(p) for p in tables)
        rows = value.get("sources")
        if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_SOURCES:
            raise ValueError
        sources: list[UsageSourceConfig] = []
        keys: set[tuple[str, str]] = set()
        caches: set[Path] = set()
        names: dict[str, str] = {}
        limits_kinds: set[str] = set()
        for row in rows:
            if not isinstance(row, dict) or set(row) - USAGE_SOURCE_KEYS:
                raise ValueError
            kind, format_id = row.get("kind"), row.get("format")
            # a usage source needs a valid kind name, not a start permission.
            # `agent_kinds` alone authorizes starting an agent; reading usage never does.
            if (
                not isinstance(kind, str)
                or AGENT_KIND.fullmatch(kind) is None
                or not isinstance(format_id, str)
                or format_id not in READERS
            ):
                raise ValueError
            if (kind, format_id) in keys:
                raise ValueError
            keys.add((kind, format_id))
            root = _usage_path(row.get("root"), directory=True)
            name = row.get("display_name", kind)
            if (
                not isinstance(name, str)
                or not 1 <= len(name) <= 128
                or any(ord(c) < 32 for c in name)
                or "/" in name
                or "\\" in name
                or (kind in names and names[kind] != name)
            ):
                raise ValueError
            names[kind] = name
            limits = row.get("limits_format")
            if limits is not None and limits not in LIMITS_READERS:
                raise ValueError
            settings, cache = None, None
            if "limits_settings" in row or "limits_cache" in row:
                if limits != "json-statusline-limits-v1":
                    raise ValueError
            if limits == "json-statusline-limits-v1":
                # This cache format is explicitly single-account per configured kind.
                if kind in limits_kinds:
                    raise ValueError
                limits_kinds.add(kind)
                cache = _usage_path(row.get("limits_cache"), private=True)
                if cache in caches:
                    raise ValueError
                caches.add(cache)
            if "limits_settings" in row:
                settings = _usage_path(row["limits_settings"])
            sources.append(UsageSourceConfig(kind, format_id, root, name, limits, settings, cache))
        plane = value.get("limits", [])
        if not isinstance(plane, list):
            raise ValueError
        scheduled: list[UsageLimitsConfig] = []
        seen: set[tuple[str, str]] = set()
        for row in plane:
            if not isinstance(row, dict) or set(row) - USAGE_LIMITS_KEYS:
                raise ValueError
            limit_kind, limit_format = row.get("kind"), row.get("format")
            if (
                not isinstance(limit_kind, str)
                or limit_kind not in kinds
                or not isinstance(limit_format, str)
                or not 1 <= len(limit_format) <= MAX_KEY_CHARS
                or _LIMITS_FORMAT.fullmatch(limit_format) is None
            ):
                raise ValueError
            if (limit_kind, limit_format) in seen:
                raise ValueError
            seen.add((limit_kind, limit_format))
            on_demand = row.get("on_demand", False)
            if type(on_demand) is not bool:
                raise ValueError
            # on-demand blocks resolve the herdr binary at
            # runtime, so no baked-in command is needed; timer blocks keep
            # requiring one.
            raw_command = row.get("command")
            if raw_command is None and on_demand:
                command: tuple[str, ...] = ()
            else:
                command = _limits_command(raw_command)
            interval = row.get("min_interval_seconds", 300)
            if type(interval) is not int or interval < 60:
                raise ValueError
            timeout = row.get("timeout_seconds", 15)
            if type(timeout) is not int or timeout < 1:
                raise ValueError
            enabled = row.get("enabled", True)
            if type(enabled) is not bool:
                raise ValueError
            # a full TUI run needs room (observed tool startup
            # alone can take ~12 s); the approved bound is 90 s within 30..300.
            if on_demand and not 30 <= timeout <= 300:
                raise ValueError
            scheduled.append(
                UsageLimitsConfig(
                    limit_kind, limit_format, command, interval, timeout, enabled, on_demand
                )
            )
        inferred_linking = value.get("inferred_linking", True)
        if type(inferred_linking) is not bool:
            raise ValueError
        return UsageConfig(
            timezone, tuple(sources), paths, retention, tuple(scheduled), inferred_linking
        )
    except (OSError, ValueError, TypeError, KeyError, ZoneInfoNotFoundError):
        LOG.warning("usage_disabled reason=invalid_configuration")
        return None


def agent_kinds_from(value: Any) -> tuple[str, ...]:
    """the owner's launchable kinds. Shape-checked and deduplicated; the
    connector still requires each kind to be on PATH before it starts anything."""
    if not isinstance(value, list):
        raise ValueError("agent_kinds must be a list of kind names")
    kinds: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or AGENT_KIND.fullmatch(item) is None:
            raise ValueError(f"agent_kinds entry {index} is not a valid kind name")
        if item not in kinds:
            kinds.append(item)
    if len(kinds) > MAX_AGENT_KINDS:
        raise ValueError(f"agent_kinds allows at most {MAX_AGENT_KINDS} kinds")
    return tuple(kinds)


# A reserved, non-personal example domain. Push services reject placeholder domains such as
# .invalid and localhost (a live check against web.push.apple.com returned BadJwtToken for them).
VAPID_SUBJECT_DEFAULT = "https://example.com/relaydeck"
# TLDs and names that are never valid VAPID subjects (placeholders or local-only names).
_UNROUTABLE_SUFFIXES = (".invalid", ".localhost", ".local", ".test")


def valid_vapid_subject(value: object) -> bool:
    """A mailto: address on a routable domain, or an https: URL. Placeholder domains are refused."""
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 256
        or any(char.isspace() for char in value)
    ):
        return False
    if value.startswith("https://"):
        host = value.removeprefix("https://").split("/", 1)[0].split("?", 1)[0].lower()
        return "@" not in host and "." in host and not host.endswith(_UNROUTABLE_SUFFIXES)
    if not value.startswith("mailto:"):
        return False
    address = value.removeprefix("mailto:")
    local, at, domain = address.partition("@")
    if not at or not local or "@" in domain or "." not in domain:
        return False
    domain = domain.lower()
    if domain == "localhost" or domain.endswith(_UNROUTABLE_SUFFIXES):
        return False
    return all(label and not label.startswith("-") for label in domain.split("."))


@dataclass(frozen=True)
class NotificationsConfig:
    """`[notifications]`: web push is the default route; the lock screen stays generic."""

    web_push: bool = True
    lock_screen_detail: bool = False
    vapid_subject: str = VAPID_SUBJECT_DEFAULT


def notifications_from(value: Any) -> NotificationsConfig:
    if value is None:
        return NotificationsConfig()
    if not isinstance(value, dict):
        raise ValueError("notifications must be a table")
    allowed = {"web_push", "lock_screen_detail", "vapid_subject"}
    if set(value) - allowed:
        raise ValueError("unknown notifications field")
    for key in ("web_push", "lock_screen_detail"):
        if key in value and type(value[key]) is not bool:
            raise ValueError(f"notifications.{key} must be true or false")
    subject = value.get("vapid_subject", VAPID_SUBJECT_DEFAULT)
    if not valid_vapid_subject(subject):
        raise ValueError("notifications.vapid_subject must be a mailto: or https: address")
    return NotificationsConfig(
        web_push=value.get("web_push", True),
        lock_screen_detail=value.get("lock_screen_detail", False),
        vapid_subject=subject,
    )


@dataclass(frozen=True)
class CompanionConfig:
    advertised_origin: str
    cookie_secret_file: Path
    cursor_secret_file: Path
    state_dir: Path
    connector_credentials: Path
    herdr_socket: str
    host_id: str = "home-mac"
    bind_host: str = "127.0.0.1"
    port: int = 8125
    fcm_credentials: Path | None = None
    identity_config: Path | None = None
    # Browser UI is optional: false serves only the gateway API the Android
    # app uses (plus the Herdr connector). Chosen at install time.
    web_ui: bool = True
    # Automatic tailnet-only route: derive this Mac's own private origin
    # from Tailscale and map tailscale_https_port -> the loopback backend.
    tailscale_auto: bool = False
    tailscale_https_port: int = 8445
    # Herdr agent kinds this Mac may start; each must also be on PATH (see connector).
    agent_kinds: tuple[str, ...] = ()
    usage: UsageConfig | None = None
    notifications: NotificationsConfig = field(default_factory=NotificationsConfig)

    @property
    def local_endpoint(self) -> str:
        host = f"[{self.bind_host}]" if ":" in self.bind_host else self.bind_host
        return origin(f"http://{host}:{self.port}", loopback=True)

    def secrets(self) -> tuple[bytes, bytes]:
        cookie = read_private(self.cookie_secret_file).strip().encode()
        cursor = read_private(self.cursor_secret_file).strip().encode()
        if min(len(cookie), len(cursor)) < 32 or cookie == cursor:
            raise ValueError("provide separate cookie and cursor secret files (each >=32 bytes)")
        return cookie, cursor


def load_config(path: Path, resolve_origin: Callable[[int], str] | None = None) -> CompanionConfig:
    """`resolve_origin(https_port)` derives the private origin when
    `tailscale_auto` is on and no advertised_origin is configured."""
    raw: dict[str, Any] = tomllib.loads(read_private(path))
    permitted = {
        "advertised_origin",
        "cookie_secret_file",
        "cursor_secret_file",
        "state_dir",
        "connector_credentials",
        "herdr_socket",
        "host_id",
        "bind_host",
        "port",
        "fcm_credentials",
        "identity_config",
        "web_ui",
        "tailscale_auto",
        "tailscale_https_port",
        "agent_kinds",
        "usage",
        "notifications",
    }
    if set(raw) - permitted:
        raise ValueError("unknown companion configuration field")
    flags = {}
    for name, default in (("web_ui", True), ("tailscale_auto", False)):
        flags[name] = raw.get(name, default)
        if type(flags[name]) is not bool:
            raise ValueError(f"{name} must be true or false")
    https_port = raw.get("tailscale_https_port", 8445)
    if type(https_port) is not int or not 1 <= https_port <= 65535:
        raise ValueError("tailscale_https_port must be an integer from 1 to 65535")
    if flags["tailscale_auto"] and "advertised_origin" not in raw:
        if resolve_origin is None:
            from relaydeck.companion.tailscale import private_origin

            resolve_origin = private_origin
        raw["advertised_origin"] = resolve_origin(https_port)
    for key in ("advertised_origin", "cookie_secret_file", "cursor_secret_file"):
        if not isinstance(raw.get(key), str) or not raw[key]:
            raise ValueError(f"{key} is required")
    for key in permitted - {
        "port",
        "web_ui",
        "tailscale_auto",
        "tailscale_https_port",
        "agent_kinds",
        "usage",
        "notifications",
    }:
        if key in raw and (not isinstance(raw[key], str) or not raw[key]):
            raise ValueError(f"{key} must be nonempty text")
    port = raw.get("port", 8125)
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("port must be an integer from 1 to 65535")

    def file(key: str, default: str = "") -> Path:
        value = Path(raw.get(key, default)).expanduser()
        return value if value.is_absolute() else path.parent / value

    kinds = agent_kinds_from(raw.get("agent_kinds", []))
    config = CompanionConfig(
        advertised_origin=origin(raw["advertised_origin"], tailscale=True),
        cookie_secret_file=file("cookie_secret_file"),
        cursor_secret_file=file("cursor_secret_file"),
        state_dir=file("state_dir", DEFAULT_STATE),
        connector_credentials=file("connector_credentials", "~/.config/relaydeck/connector.json"),
        herdr_socket=str(file("herdr_socket", "~/.config/herdr/herdr.sock")),
        host_id=raw.get("host_id", "home-mac"),
        bind_host=raw.get("bind_host", "127.0.0.1"),
        port=port,
        fcm_credentials=file("fcm_credentials") if "fcm_credentials" in raw else None,
        identity_config=file("identity_config") if "identity_config" in raw else None,
        web_ui=flags["web_ui"],
        tailscale_auto=flags["tailscale_auto"],
        tailscale_https_port=https_port,
        agent_kinds=kinds,
        usage=usage_from(raw.get("usage"), kinds),
        notifications=notifications_from(raw.get("notifications")),
    )
    _ = config.local_endpoint  # Strict loopback validation, including manual configs.
    return config
