"""Web bundle repair markers and the per-user installed bundle store.

The marker table and its pure checks used to live only in `scripts/build_manifest.py`,
which is not packaged. They move here so an installed `relaydeck` tool can verify a
bundle the same way the build gate does (`scripts/build_manifest.py` imports them).

A bundle is installed under `~/Library/Application Support/RelayDeck/web/<build-id>/`
only after its repair markers verify on the copy; `installed.json` records what was
installed. Nothing is ever half-copied: a failed install removes its staging directory.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MAIN_JS = "main.dart.js"
# A build id is a bounded hexadecimal revision plus a hash prefix, or the hash prefix alone; it is
# the only part of a bundle's metadata that ever becomes a path.
BUILD_ID = re.compile(r"[0-9a-f]{12}-[0-9a-f]{12}|[0-9a-f]{16}")
_REVISION = re.compile(r"[0-9a-f]{12,64}")
MAX_FILES = 20_000
MAX_DEPTH = 32
MAX_BYTES = 1 << 30
MANIFEST_SUFFIX = ".build-manifest.json"
INSTALLED_NAME = "installed.json"

# Compiled-output evidence of a repair. String literals survive minification; counts were calibrated
# on real bundles (pristine: touchend 3, touchcancel 1, abort 1; with the backport: touchend 4,
# touchcancel 2; with the file_picker repair: abort 3).
REPAIR_MARKERS: dict[str, dict[str, int]] = {
    "flet-reconnect-merge-patch": {"flet_reconnect_merge_rd1": 1, "flet_reconnect_wake_rd1": 1},
    "flutter-ios-pointer-patch": {"touchend": 4, "touchcancel": 2},
    "flutter-file-upload-patch": {"abort": 3},
}


class BundleError(RuntimeError):
    """A bundle cannot be installed or is not usable; the message is user-facing."""


def repair_markers(js: str, only: tuple[str, ...] = ()) -> dict[str, object]:
    """Count the calibrated string literals per repair; `ok` needs them all."""
    result: dict[str, object] = {}
    for patch, wanted in REPAIR_MARKERS.items():
        if only and patch not in only:
            continue
        counts = {literal: js.count(f'"{literal}"') for literal in wanted}
        result[patch] = {
            "counts": counts,
            "required": wanted,
            "ok": all(counts[lit] >= need for lit, need in wanted.items()),
        }
    return result


def markers_ok(markers: dict[str, object]) -> bool:
    return all(isinstance(record, dict) and record.get("ok") is True for record in markers.values())


def default_root() -> Path:
    return Path("~/Library/Application Support/RelayDeck/web").expanduser()


def _read_js(bundle: Path) -> str:
    try:
        return (bundle / MAIN_JS).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise BundleError(f"cannot read {bundle / MAIN_JS}: {exc}") from exc


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_id(source: Path, digest: str) -> str:
    """`<commit12>-<hash12>` when the sibling build manifest names a commit, else the
    hash prefix. The manifest is `<source>.build-manifest.json` next to the bundle dir."""
    manifest = source.with_name(source.name + MANIFEST_SUFFIX)
    commit = ""
    if manifest.is_file():
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
            app = data.get("app") if isinstance(data, dict) else None
            revision = app.get("revision") if isinstance(app, dict) else None
            commit = revision if isinstance(revision, str) else ""
        except (OSError, ValueError):
            commit = ""
    if not _REVISION.fullmatch(commit):
        commit = ""  # anything else is not a revision and never reaches a path
    return f"{commit[:12]}-{digest[:12]}" if commit else digest[:16]


@dataclass(frozen=True)
class Installed:
    build_id: str
    path: Path
    installed_at: str
    markers: dict[str, object]


_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


def _same(first: os.stat_result, second: os.stat_result) -> bool:
    return (first.st_dev, first.st_ino) == (second.st_dev, second.st_ino)


def _open_store(store: Path) -> int:
    """Open the store as a pinned directory descriptor: a real directory, owned by this user,
    that others cannot write. Everything below is addressed relative to this descriptor."""
    store.mkdir(mode=0o700, parents=True, exist_ok=True)
    real = store.resolve()
    try:
        fd = os.open(real, _DIR_FLAGS)
    except OSError as exc:
        raise BundleError(f"{store} is not a usable directory; not installed") from exc
    info = os.fstat(fd)
    if info.st_uid != os.getuid() or info.st_mode & 0o022 or not _same(info, real.lstat()):
        os.close(fd)
        raise BundleError(f"{store} is not a private directory owned by you; not installed")
    return fd


def _copy_dir(src_fd: int, dst_fd: int, depth: int, budget: dict[str, int]) -> None:
    """Copy regular files and directories only, addressed through pinned descriptors.

    Every directory is opened `O_DIRECTORY|O_NOFOLLOW` relative to its parent and must be the
    object that was listed; every file is opened relative to its directory descriptor with no
    follow and must be that same regular file. A symlink, device, socket or fifo, or an entry
    replaced after it was listed, refuses the install. Bytes are counted as they are copied."""
    if depth > MAX_DEPTH:
        raise BundleError("the bundle is nested too deeply; not installed")
    for name in os.listdir(src_fd):
        info = os.stat(name, dir_fd=src_fd, follow_symlinks=False)
        if stat.S_ISDIR(info.st_mode):
            child = os.open(name, _DIR_FLAGS, dir_fd=src_fd)
            try:
                if not _same(os.fstat(child), info):
                    raise BundleError(f"{name} changed while copying; not installed")
                os.mkdir(name, 0o700, dir_fd=dst_fd)
                out_dir = os.open(name, _DIR_FLAGS, dir_fd=dst_fd)
                try:
                    _copy_dir(child, out_dir, depth + 1, budget)
                finally:
                    os.close(out_dir)
            finally:
                os.close(child)
            continue
        if not stat.S_ISREG(info.st_mode):
            raise BundleError(f"{name} is a link or special file; a bundle may hold only files")
        budget["files"] += 1
        if budget["files"] > MAX_FILES:
            raise BundleError("the bundle is too large; not installed")
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=src_fd)
        with os.fdopen(fd, "rb") as handle:
            opened = os.fstat(handle.fileno())
            if not stat.S_ISREG(opened.st_mode) or not _same(opened, info):
                raise BundleError(f"{name} changed while copying; not installed")
            out = os.open(
                name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=dst_fd
            )
            with os.fdopen(out, "wb") as sink:
                while chunk := handle.read(1 << 20):
                    budget["bytes"] += len(chunk)
                    if budget["bytes"] > MAX_BYTES:
                        raise BundleError("the bundle is too large; not installed")
                    sink.write(chunk)


def install(source: Path, *, root: Path | None = None) -> Installed:
    """Copy a built web bundle into the store after its markers verify on the copy.

    Refuses (BundleError) when the source lacks the markers or the copy fails
    verification; in both cases nothing is left in the store.
    """
    origin = Path(source).expanduser().resolve()
    if not (origin / MAIN_JS).is_file():
        raise BundleError(f"no {MAIN_JS} in {origin}")
    if (origin / MAIN_JS).is_symlink():
        raise BundleError(f"{MAIN_JS} must be a regular file; not installed")
    markers = repair_markers(_read_js(origin))
    if not markers_ok(markers):
        raise BundleError(
            "repair markers missing: the patches are not in this bundle; not installed"
        )
    digest = _sha256(origin / MAIN_JS)
    bid = build_id(origin, digest)
    if BUILD_ID.fullmatch(bid) is None:
        raise BundleError("the bundle identity is not valid; not installed")
    store = root or default_root()
    store_fd = _open_store(store)
    try:
        dest = store.resolve() / bid
        try:
            present = os.stat(bid, dir_fd=store_fd, follow_symlinks=False)
        except FileNotFoundError:
            present = None
        if present is not None:
            existing = read_installed(dest)
            if stat.S_ISDIR(present.st_mode) and existing is not None and verify(dest):
                return existing
            raise BundleError(f"{dest} exists but does not verify; remove it by hand first")
        return _install_copy(origin, store.resolve(), store_fd, bid, digest, store)
    finally:
        os.close(store_fd)


def _install_copy(
    origin: Path, store: Path, store_fd: int, bid: str, digest: str, shown: Path
) -> Installed:
    # An independent, unpredictable staging directory created through the pinned store
    # descriptor; only it is ever removed.
    token = ".staging-" + secrets.token_hex(8)
    os.mkdir(token, 0o700, dir_fd=store_fd)
    try:
        stage_fd = os.open(token, _DIR_FLAGS, dir_fd=store_fd)
        try:
            os.mkdir("bundle", 0o700, dir_fd=stage_fd)
            bundle_fd = os.open("bundle", _DIR_FLAGS, dir_fd=stage_fd)
            try:
                try:
                    src_fd = os.open(origin, _DIR_FLAGS)
                except OSError as exc:
                    raise BundleError(f"cannot open {origin}: {exc}") from exc
                try:
                    _copy_dir(src_fd, bundle_fd, 0, {"files": 0, "bytes": 0})
                except OSError as exc:
                    # A refused no-follow open means an entry was replaced under the copy.
                    raise BundleError(
                        f"the bundle changed while it was being copied ({exc.strerror}); "
                        "not installed"
                    ) from exc
                finally:
                    os.close(src_fd)
                staged = store / token / "bundle"
                copied = repair_markers(_read_js(staged))
                if not markers_ok(copied) or _sha256(staged / MAIN_JS) != digest:
                    raise BundleError("the copy did not verify; nothing was installed")
                installed_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                record = {
                    "build_id": bid,
                    "source": str(origin),
                    "main_dart_js_sha256": digest,
                    "markers": copied,
                    "installed_at": installed_at,
                }
                out = os.open(
                    INSTALLED_NAME,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=bundle_fd,
                )
                with os.fdopen(out, "w", encoding="utf-8") as sink:
                    sink.write(json.dumps(record, indent=2, sort_keys=True) + "\n")
            finally:
                os.close(bundle_fd)
            try:
                os.rename("bundle", bid, src_dir_fd=stage_fd, dst_dir_fd=store_fd)
            except OSError as exc:
                raise BundleError(f"cannot place the bundle in the store: {exc}") from exc
        finally:
            os.close(stage_fd)
    except BaseException:
        shutil.rmtree(token, ignore_errors=True, dir_fd=store_fd)
        raise
    shutil.rmtree(token, ignore_errors=True, dir_fd=store_fd)
    return Installed(bid, shown / bid, installed_at, copied)


def read_installed(bundle: Path) -> Installed | None:
    try:
        data = json.loads((bundle / INSTALLED_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("build_id"), str):
        return None
    markers = data.get("markers")
    return Installed(
        build_id=data["build_id"],
        path=bundle,
        installed_at=str(data.get("installed_at", "")),
        markers=markers if isinstance(markers, dict) else {},
    )


def verify(bundle: Path) -> bool:
    """True when the installed copy still carries its markers and its recorded hash."""
    record = read_installed(bundle)
    if record is None or not (bundle / MAIN_JS).is_file():
        return False
    markers = repair_markers(_read_js(bundle))
    if not markers_ok(markers):
        return False
    try:
        data: dict[str, Any] = json.loads((bundle / INSTALLED_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return data.get("main_dart_js_sha256") == _sha256(bundle / MAIN_JS)


def list_bundles(root: Path | None = None) -> list[dict[str, object]]:
    """Installed bundles newest first; `current` marks the newest verified one."""
    store = root or default_root()
    rows: list[dict[str, object]] = []
    if store.is_dir():
        for child in store.iterdir():
            if child.name.startswith(".") or child.is_symlink() or not child.is_dir():
                continue
            record = read_installed(child)
            if record is None:
                continue
            rows.append(
                {
                    "build_id": record.build_id,
                    "path": str(child),
                    "installed_at": record.installed_at,
                    "verified": verify(child),
                }
            )
    rows.sort(key=lambda row: str(row["installed_at"]), reverse=True)
    current_set = False
    for row in rows:
        row["current"] = bool(row["verified"]) and not current_set
        current_set = current_set or bool(row["current"])
    return rows


def current_bundle(root: Path | None = None) -> Path | None:
    """The newest verified installed bundle, or None when nothing verified is installed."""
    for row in list_bundles(root):
        if row["current"]:
            return Path(str(row["path"]))
    return None
