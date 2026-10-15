"""Read-only CLI detection for `relaydeck setup --detect`.

Four separate facts per catalog entry, never merged:

1. supported by Herdr (from the text of `herdr integration status`, read-only, 5 s timeout);
2. installed (an absolute executable path was found in PATH or a standard bin directory);
3. a RelayDeck usage adapter is available (the catalog's reader format, or "not yet");
4. configured (the owner's companion configuration names the entry).

Nothing is executed except the single read-only Herdr command, nothing inside a root is opened
(roots are
checked with `os.path.isdir` only), and nothing is written. The catalog is versioned data; this
module
holds no product names.
"""

from __future__ import annotations

import os
import re
import subprocess
import tomllib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

from relaydeck.companion.trusted import probe_environment, trusted_executable
from relaydeck.usage.limits_plane.contracts import TuiRecipe, tui_recipe_from

CATALOG_VERSION = 1
KNOWN_READER_FORMATS = frozenset(
    {
        "jsonl-message-usage-v1",
        "jsonl-cumulative-token-count-v1",
        "jsonl-event-usage-v1",
        "json-session-turns-v1",
        "json-session-credits-v1",
    }
)
TIERS = ("A", "B", "C", "D", "E")
LIMITS = ("statusline-hook", "jsonrpc-rate-limits-stdio-v1", "tui-usage-pane-v1", "none")
STANDARD_BIN_DIRS = ("~/.local/bin", "/opt/homebrew/bin", "/usr/local/bin")
HERDR_TIMEOUT_SECONDS = 5
_ID = re.compile(r"^[a-z][a-z0-9-]*$")
_HERDR_LINE = re.compile(
    r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)(?: \((?P<tag>[^()]*)\))?: (?P<state>.+)$"
)
_HERDR_PATH_SUFFIX = re.compile(r" \((?:/|~)[^()]*\)$")


class CatalogError(ValueError):
    """The catalog failed validation. Raised before any detection runs."""


@dataclass(frozen=True)
class CatalogEntry:
    id: str
    display_name: str
    aliases: tuple[str, ...]
    binaries: tuple[str, ...]
    default_roots: tuple[str, ...]
    env_override: str | None
    env_subdir: str
    reader_format: str | None
    limits: str
    tier: str
    evidence: str
    herdr_integration: str | None
    herdr_detects: bool
    reader_root: str | None = (
        None  # the directory the reader enumerates; what setup writes (tier A only)
    )
    # the validated TUI recipe when limits == "tui-usage-pane-v1".
    tui: TuiRecipe | None = None


@dataclass(frozen=True)
class Catalog:
    version: int
    entries: tuple[CatalogEntry, ...]


def _validate(data: Mapping[str, object]) -> Catalog:
    if data.get("catalog_version") != CATALOG_VERSION:
        raise CatalogError("unsupported catalog_version")
    raw_entries = data.get("cli")
    if not isinstance(raw_entries, list) or not raw_entries:
        raise CatalogError("the catalog has no entries")
    entries: list[CatalogEntry] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_entries):
        if not isinstance(raw, dict):
            raise CatalogError(f"entry {index} is not a table")
        entry_id = raw.get("id")
        if not isinstance(entry_id, str) or not _ID.match(entry_id):
            raise CatalogError(f"entry {index} has an invalid id")
        if entry_id in seen:
            raise CatalogError(f"duplicate id: {entry_id}")
        seen.add(entry_id)
        tier = raw.get("tier")
        if tier not in TIERS:
            raise CatalogError(f"{entry_id}: unknown tier")
        reader = raw.get("reader_format")
        if reader is not None:
            if tier != "A":
                raise CatalogError(f"{entry_id}: reader_format is only allowed on tier A")
            if reader not in KNOWN_READER_FORMATS:
                raise CatalogError(f"{entry_id}: unknown reader_format")
        limits = raw.get("limits", "none")
        if limits not in LIMITS:
            raise CatalogError(f"{entry_id}: unknown limits source")
        binaries = _strings(raw.get("binaries"), f"{entry_id}: binaries")
        roots = _strings(raw.get("default_roots"), f"{entry_id}: default_roots")
        for root in roots:
            if not os.path.isabs(os.path.expanduser(root)):
                raise CatalogError(f"{entry_id}: root is not absolute after ~ expansion")
        env_override = raw.get("env_override")
        if env_override is not None and not isinstance(env_override, str):
            raise CatalogError(f"{entry_id}: env_override must be a name")
        env_subdir = raw.get("env_subdir", "")
        if not isinstance(env_subdir, str) or os.path.isabs(env_subdir):
            raise CatalogError(f"{entry_id}: env_subdir must be a relative path")
        display = raw.get("display_name")
        if not isinstance(display, str) or not display:
            raise CatalogError(f"{entry_id}: display_name is required")
        reader_root = raw.get("reader_root")
        if reader_root is not None:
            if reader is None:  # reader_root is only meaningful with a reader
                raise CatalogError(f"{entry_id}: reader_root needs a reader_format")
            if not isinstance(reader_root, str) or not os.path.isabs(
                os.path.expanduser(reader_root)
            ):
                raise CatalogError(f"{entry_id}: reader_root must be absolute after ~ expansion")
        recipe = raw.get("tui")
        if limits == "tui-usage-pane-v1":
            if recipe is None:
                raise CatalogError(f"{entry_id}: tui-usage-pane-v1 needs a [cli.tui] recipe")
            try:
                tui = tui_recipe_from(recipe)
            except ValueError as exc:
                raise CatalogError(f"{entry_id}: bad tui recipe ({exc})") from exc
        elif recipe is not None:
            raise CatalogError(f"{entry_id}: tui recipe needs limits tui-usage-pane-v1")
        else:
            tui = None
        detects = raw.get("herdr_detects", False)
        if type(detects) is not bool:
            raise CatalogError(f"{entry_id}: herdr_detects must be true or false")
        integration = raw.get("herdr_integration")
        if integration is not None and not isinstance(integration, str):
            raise CatalogError(f"{entry_id}: herdr_integration must be a name")
        entries.append(
            CatalogEntry(
                id=entry_id,
                display_name=display,
                aliases=_strings(raw.get("aliases", []), f"{entry_id}: aliases", allow_empty=True),
                binaries=binaries,
                default_roots=roots,
                env_override=env_override,
                env_subdir=env_subdir,
                reader_format=reader if isinstance(reader, str) else None,
                limits=limits,
                tier=str(tier),
                evidence=str(raw.get("evidence", "")),
                herdr_integration=integration,
                herdr_detects=detects,
                reader_root=reader_root if isinstance(reader_root, str) else None,
                tui=tui,
            )
        )
    return Catalog(version=CATALOG_VERSION, entries=tuple(entries))


def _strings(value: object, label: str, *, allow_empty: bool = False) -> tuple[str, ...]:
    if not isinstance(value, list) or (not value and not allow_empty):
        raise CatalogError(f"{label} must be a non-empty list of strings")
    if not all(isinstance(item, str) and item for item in value):
        raise CatalogError(f"{label} must contain only non-empty strings")
    return tuple(value)


def load_catalog(text: str | None = None) -> Catalog:
    """The shipped catalog (read with importlib.resources), or a given TOML text for tests."""
    if text is None:
        text = (
            resources.files("relaydeck.companion")
            .joinpath("data")
            .joinpath("cli_catalog.toml")
            .read_text(encoding="utf-8")
        )
    return _validate(tomllib.loads(text))


# -- detection -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class DetectRow:
    id: str
    display_name: str
    tier: str
    herdr_detects: bool  # Herdr has a detection manifest for this CLI (the catalog's list)
    herdr_hook: str  # "current v10", "outdated v12<v13", "not installed", "none" or "unknown"
    binary: str | None  # the absolute path found, never executed
    adapter: str  # the reader format id, or "not yet"
    configured: bool
    roots_present: tuple[str, ...]
    next_step: str
    reader_root: str | None = None  # where a usage source would be written (tier A); None otherwise
    limits: str = "none"  # the catalog limits source for setup offers


@dataclass(frozen=True)
class DetectReport:
    rows: tuple[DetectRow, ...]
    herdr_status: str  # "ok" or "unknown"
    config_status: str  # "ok", "missing" or "invalid"
    notes: tuple[str, ...] = field(default=())


HerdrReader = Callable[[], str | None]


def search_directories(path_value: str, home: str) -> tuple[str, ...]:
    """PATH entries first, then the standard bin directories; absolute and de-duplicated."""
    candidates = [
        *path_value.split(os.pathsep),
        *(os.path.expanduser(d) for d in STANDARD_BIN_DIRS),
    ]
    seen: list[str] = []
    for item in candidates:
        if item and os.path.isabs(item) and item not in seen:
            seen.append(item)
    return tuple(seen)


def find_binary(names: Sequence[str], directories: Sequence[str]) -> str | None:
    """The first absolute path of an executable named in `names`. Uses os.access; never runs it."""
    for directory in directories:
        for name in names:
            candidate = os.path.join(directory, name)
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
    return None


def _reader_path(entry: CatalogEntry, home: str) -> str | None:
    """The reader root with ~ replaced by the home directory, or None when the catalog has none."""
    if entry.reader_root is None:
        return None
    if entry.reader_root.startswith("~/"):
        return os.path.normpath(os.path.join(home, entry.reader_root[2:]))
    return os.path.normpath(os.path.expanduser(entry.reader_root))


def root_candidates(entry: CatalogEntry, home: str, environ: Mapping[str, str]) -> tuple[str, ...]:
    """The default roots with `~` expanded, plus the CLI's own environment override when it is an
    absolute path. Aggregator variables are never read: the catalog names only CLI-native ones."""
    roots = [
        os.path.join(home, root[2:]) if root.startswith("~/") else os.path.expanduser(root)
        for root in entry.default_roots
    ]
    if entry.env_override:
        value = environ.get(entry.env_override, "")
        if value and os.path.isabs(value):
            roots.append(os.path.join(value, entry.env_subdir) if entry.env_subdir else value)
    unique: list[str] = []
    for root in roots:
        if root not in unique:
            unique.append(root)
    return tuple(unique)


def hook_text(state: str | None, herdr_available: bool) -> str:
    """Herdr's integration state in short form.

    `none`: Herdr has no hook for this CLI. `unknown`: Herdr status could not be read at all.
    """
    if not herdr_available:
        return "unknown"
    if state is None:
        return "none"
    match = re.match(r"^current \(v(?P<v>[^)]+)\)$", state)
    if match:
        return f"current v{match.group('v')}"
    match = re.match(r"^outdated \(v(?P<old>[^ ]+) < v(?P<new>[^)]+)\)$", state)
    if match:
        return f"outdated v{match.group('old')}<v{match.group('new')}"
    return state


def parse_herdr_status(text: str) -> dict[str, str]:
    """Herdr's `integration status` lines, `<name>: <state> (<detail>) (<path>)`, as name ->
    state."""
    states: dict[str, str] = {}
    for line in text.splitlines():
        match = _HERDR_LINE.match(line.strip())
        if not match:
            continue
        name = match.group("name")
        state = _HERDR_PATH_SUFFIX.sub("", match.group("state")).strip()
        states[name] = state
    return states


def herdr_status_text(directories: Sequence[str]) -> str | None:
    """Run `herdr integration status` read-only, through argv, with a 5 s timeout. Any failure is
    None."""
    binary = trusted_executable("herdr", directories=directories)
    if binary is None:
        return None
    try:
        result = subprocess.run(
            [binary, "integration", "status"],
            capture_output=True,
            text=True,
            timeout=HERDR_TIMEOUT_SECONDS,
            check=False,
            env=probe_environment(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def read_configured(config_path: Path | None) -> tuple[set[str], str, set[str]]:
    """The Herdr agent kinds and usage-source kinds the owner's companion configuration names.

    Read-only, through the existing loader. A missing or invalid file is a status, never an
    error."""
    if config_path is None:
        return set(), "missing", set()
    from relaydeck.companion.config import load_config

    try:
        config = load_config(config_path)
    except FileNotFoundError:
        return set(), "missing", set()
    except (OSError, ValueError, tomllib.TOMLDecodeError):
        return set(), "invalid", set()
    kinds = set(config.agent_kinds)
    roots: set[str] = set()
    if config.usage is not None:
        kinds.update(source.kind for source in config.usage.sources)
        roots.update(os.path.normpath(str(source.root)) for source in config.usage.sources)
    return kinds, "ok", roots


def _next_step(installed: bool, adapter_available: bool, configured: bool) -> str:
    if not installed:
        return "not installed"
    if not adapter_available:
        return "needs an adapter (not supported yet)"
    if configured:
        return "already configured"
    return "can be added"


def run_detect(
    catalog: Catalog,
    *,
    home: str,
    environ: Mapping[str, str],
    config_path: Path | None,
    herdr_reader: HerdrReader | None = None,
) -> DetectReport:
    """The whole read-only detection. `herdr_reader` returns the Herdr status text or None; the
    default
    runs the one allowed read-only command."""
    directories = search_directories(environ.get("PATH", ""), home)
    herdr_text = herdr_reader() if herdr_reader is not None else herdr_status_text(directories)
    herdr_states = parse_herdr_status(herdr_text) if herdr_text is not None else {}
    configured_kinds, config_status, configured_roots = read_configured(config_path)
    rows: list[DetectRow] = []
    for entry in catalog.entries:
        integration = entry.herdr_integration or entry.id
        state = herdr_states.get(integration) if entry.herdr_integration else None
        binary = find_binary(entry.binaries, directories)
        present = tuple(
            root for root in root_candidates(entry, home, environ) if os.path.isdir(root)
        )
        adapter = entry.reader_format or "not yet"
        reader_path = _reader_path(entry, home)
        configured = bool({entry.id, *entry.aliases} & configured_kinds) or (
            reader_path is not None and os.path.normpath(reader_path) in configured_roots
        )
        rows.append(
            DetectRow(
                id=entry.id,
                display_name=entry.display_name,
                tier=entry.tier,
                herdr_detects=entry.herdr_detects,
                herdr_hook=hook_text(state, herdr_text is not None),
                binary=binary,
                adapter=adapter,
                configured=configured,
                roots_present=present,
                reader_root=reader_path,
                next_step=_next_step(
                    binary is not None, entry.reader_format is not None, configured
                ),
                limits=entry.limits,
            )
        )
    notes: list[str] = []
    if herdr_text is None:
        notes.append("Herdr status unknown: `herdr integration status` was not available.")
    return DetectReport(
        rows=tuple(rows),
        herdr_status="ok" if herdr_text is not None else "unknown",
        config_status=config_status,
        notes=tuple(notes),
    )
