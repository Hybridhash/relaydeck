"""Opt-in, locked limits-hook install/uninstall; never executes settings commands.

Stop other settings writers before installation. JSON is reformatted, but unrelated
values survive. A private prepared receipt precedes the settings rename, making a
crash reviewable/uninstallable without restoring a whole settings backup.
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import stat
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from relaydeck.usage import hook_files as fs
from relaydeck.usage.hook_resource import hook_bytes
from relaydeck.usage.hook_setup import (
    HOOK_FILENAME,
    HOOK_SHA256,
    HOOK_VERSION,
    PREVIOUS_HOOK_SHA256,
    HookSetupCheck,
    default_hook_path,
    managed_command,
)


class _Unset:
    """Marker: no expectation about the statusLine was given."""


ANY_STATUSLINE = _Unset()
ABSENT_STATUSLINE = object()  # the statusLine key was not present when diagnosed


@dataclass(frozen=True)
class HookResult:
    state: str
    changed: bool = False
    reformatted: bool = False


def _interpreter(value: Path | None) -> Path:
    interpreter = value or Path(sys.executable)
    fs.absolute(interpreter)  # Deliberately does not resolve virtualenv symlinks.
    info = interpreter.stat()
    if (
        not (3, 13) <= sys.version_info[:2] < (3, 14)
        or not stat.S_ISREG(info.st_mode)
        or not info.st_mode & 0o111
        or info.st_mode & 0o022
        or info.st_uid not in (0, os.getuid())
    ):
        raise fs.HookFileError("invalid")
    return interpreter


def _receipt_name(settings: Path) -> str:
    return "limits-" + hashlib.sha256(os.fsencode(settings)).hexdigest() + ".json"


def _paths(settings: Path, cache: Path, target: Path) -> None:
    for path in (settings, cache, target):
        fs.absolute(path)
        if not path.name:
            raise fs.HookFileError("invalid")
    # Cache writers must never overwrite installer artifacts or its settings lock.
    if (
        settings == cache
        or settings.is_relative_to(target.parent)
        or cache.is_relative_to(target.parent)
        or cache == settings.parent / ("." + _receipt_name(settings) + ".lock")
    ):
        raise fs.HookFileError("invalid")


def _receipt(fd: int, name: str) -> tuple[fs.Image, dict[str, Any] | None]:
    image = fs.read(fd, name, private=True)
    if image.raw is None:
        return image, None
    body = fs.document(image.raw)
    if (
        set(body)
        != {
            "version",
            "state",
            "settings",
            "cache",
            "script",
            "interpreter",
            "hash",
            "managed",
            "backup",
            "before_sha256",
        }
        or type(body["version"]) is not int
        or body["version"] != HOOK_VERSION
        or body["hash"] not in (HOOK_SHA256, *PREVIOUS_HOOK_SHA256)
        or body["state"] not in ("prepared", "installed", "uninstalled")
        or any(not isinstance(body[k], str) for k in ("settings", "cache", "script", "interpreter"))
        or any(
            not Path(body[k]).is_absolute() or ".." in Path(body[k]).parts
            for k in ("settings", "cache", "script", "interpreter")
        )
        or body["managed"]
        != {
            "type": "command",
            "command": managed_command(
                Path(body["script"]), Path(body["cache"]), Path(body["interpreter"])
            ),
        }
        or (
            body["backup"] is not None
            and (
                not isinstance(body["backup"], str)
                or re.fullmatch(r"limits-[a-f0-9]{32}\.bak", body["backup"]) is None
            )
        )
        or (
            body["before_sha256"] is not None
            and (
                not isinstance(body["before_sha256"], str)
                or re.fullmatch(r"[a-f0-9]{64}", body["before_sha256"]) is None
            )
        )
        or (body["backup"] is None) != (body["before_sha256"] is None)
    ):
        raise fs.HookFileError("invalid")
    return image, body


def _previous(fd: int, receipt: dict[str, Any]) -> dict[str, Any]:
    backup = receipt["backup"]
    if backup is None:
        return {}
    image = fs.read(fd, backup, private=True)
    if image.raw is None or image.digest != receipt["before_sha256"]:
        raise fs.HookFileError("invalid")
    return fs.document(image.raw)


def _upgradeable(
    digest: str, receipt: dict[str, Any] | None, before: dict[str, Any], managed: dict[str, Any]
) -> bool:
    """An installed receipt for our previous hook, and a settings line that is still ours."""
    return (
        digest in PREVIOUS_HOOK_SHA256
        and receipt is not None
        and receipt["state"] == "installed"
        and receipt["hash"] == digest
        and before.get("statusLine") == managed
    )


def _bound(
    receipt: dict[str, Any], settings: Path, cache: Path, script: Path, interpreter: Path
) -> None:
    if any(
        receipt[key] != str(value)
        for key, value in (
            ("settings", settings),
            ("cache", cache),
            ("script", script),
            ("interpreter", interpreter),
        )
    ):
        raise fs.HookFileError("conflict")


def _cache_claims(fd: int, settings: Path, cache: Path) -> None:
    # One private cache per configured source; never share account allowance evidence.
    with os.scandir(fd) as entries:
        for count, entry in enumerate(entries):
            if count >= 512:
                raise fs.HookFileError("capacity")
            if re.fullmatch(r"limits-[a-f0-9]{64}\.json", entry.name) is None:
                continue
            _, body = _receipt(fd, entry.name)
            if (
                body is not None
                and body["state"] != "uninstalled"
                and body["settings"] != str(settings)
                and (
                    body["cache"] == str(cache)
                    or body["cache"] == str(settings)
                    or body["settings"] == str(cache)
                )
            ):
                raise fs.HookFileError("conflict")


def _restore_line(current: dict[str, Any], previous: dict[str, Any]) -> dict[str, Any]:
    restored = dict(current)
    if "statusLine" in previous:
        restored["statusLine"] = previous["statusLine"]
    else:
        restored.pop("statusLine", None)
    return restored


def _error(exc: Exception) -> str:
    if isinstance(exc, fs.HookFileError):
        categories = {
            "invalid",
            "conflict",
            "concurrent_edit",
            "busy",
            "capacity",
            "resource_invalid",
            "create_confirmation_required",
            "changed_since_diagnosis",
        }
        if len(exc.args) == 1 and type(exc.args[0]) is str and exc.args[0] in categories:
            return exc.args[0]
        return "invalid"
    return "unreadable" if isinstance(exc, OSError) else "invalid"


def install_hook(
    settings: Path,
    cache: Path,
    *,
    confirmed: bool = False,
    create_settings: bool = False,
    replace_statusline: bool = False,
    hooks_dir: Path | None = None,
    interpreter: Path | None = None,
    expect_statusline: object = ANY_STATUSLINE,
) -> HookResult:
    """`confirmed` is separate from Serve --yes; creation/replacement are separate opt-ins.

    `expect_statusline` binds the consent to the statusLine value that was diagnosed (or
    ABSENT_STATUSLINE): if the file holds anything else when the transaction reads it, nothing is
    written and `changed_since_diagnosis` is returned, so a replace permission never applies to a
    field the user changed after confirming."""
    if not confirmed:
        return HookResult("confirmation_required")
    wrote = False
    try:
        target = (hooks_dir or default_hook_path().parent) / HOOK_FILENAME
        _paths(settings, cache, target)
        python = _interpreter(interpreter)
        body = hook_bytes()
        managed = {"type": "command", "command": managed_command(target, cache, python)}
        with fs.directory(
            settings.parent, create=create_settings, missing_state="create_confirmation_required"
        ) as settings_fd:
            original = fs.read(settings_fd, settings.name)
            before = fs.document(original.raw) if original.raw is not None else {}
            if original.raw is None and not create_settings:
                return HookResult("create_confirmation_required")
            if expect_statusline is not ANY_STATUSLINE:
                # `original` is verified unchanged under the locks below, so this is the
                # value the transaction commits against.
                found = before.get("statusLine", ABSENT_STATUSLINE)
                if found != expect_statusline:
                    return HookResult("changed_since_diagnosis")
            if (
                "statusLine" in before
                and before["statusLine"] != managed
                and not replace_statusline
            ):
                return HookResult("conflict")
            with (
                fs.directory(target.parent, private=True, create=True) as hooks_fd,
                fs.lock(hooks_fd, ".hook-install.lock"),
                fs.lock(settings_fd, "." + _receipt_name(settings) + ".lock"),
            ):
                if (os.fstat(settings_fd).st_dev, os.fstat(settings_fd).st_ino) == (
                    os.fstat(hooks_fd).st_dev,
                    os.fstat(hooks_fd).st_ino,
                ):
                    raise fs.HookFileError("invalid")
                if fs.read(settings_fd, settings.name) != original:
                    raise fs.HookFileError("concurrent_edit")
                existing_hook = fs.read(hooks_fd, target.name, private=True)
                receipt_path = target.parent / _receipt_name(settings)
                receipt_image, receipt = _receipt(hooks_fd, receipt_path.name)
                upgrade = False
                if existing_hook.raw is not None:
                    digest = hashlib.sha256(existing_hook.raw).hexdigest()
                    if digest != HOOK_SHA256:
                        # Only our previous version, recorded by our receipt, for these exact paths.
                        if not _upgradeable(digest, receipt, before, managed):
                            return HookResult("conflict")
                        upgrade = True
                if receipt is not None:
                    _bound(receipt, settings, cache, target, python)
                _cache_claims(hooks_fd, settings, cache)
                with fs.directory(cache.parent, private=True, create=True) as cache_fd:
                    cache_image = fs.read(cache_fd, cache.name, private=True, bound=64 * 1024)
                    if (os.fstat(cache_fd).st_dev, os.fstat(cache_fd).st_ino) == (
                        os.fstat(hooks_fd).st_dev,
                        os.fstat(hooks_fd).st_ino,
                    ) or (
                        cache_image.identity is not None
                        and original.identity is not None
                        and cache_image.identity[:2] == original.identity[:2]
                    ):
                        raise fs.HookFileError("invalid")
                if upgrade:
                    assert receipt is not None and existing_hook.raw is not None
                    fs.atomic(target, hooks_fd, body, existing_hook, private=True)
                    upgraded = {**receipt, "hash": HOOK_SHA256}
                    fs.atomic(
                        receipt_path, hooks_fd, fs.encode(upgraded), receipt_image, private=True
                    )
                    receipt = upgraded
                    existing_hook = fs.read(hooks_fd, target.name, private=True)
                if before.get("statusLine") == managed:
                    if receipt is None or receipt["state"] == "uninstalled":
                        return HookResult(
                            "conflict"
                        )  # No adoption of an unowned managed-looking line.
                    _previous(hooks_fd, receipt)
                    if existing_hook.raw is None:
                        fs.atomic(target, hooks_fd, body, existing_hook, private=True)
                    if receipt["state"] != "installed":
                        fs.atomic(
                            receipt_path,
                            hooks_fd,
                            fs.encode({**receipt, "state": "installed"}),
                            receipt_image,
                            private=True,
                        )
                    # True only when the upgrade above replaced the script, so callers can
                    # tell a performed upgrade from an already-current install.
                    return HookResult("installed", upgrade)
                if existing_hook.raw is None:
                    fs.atomic(target, hooks_fd, body, existing_hook, private=True)
                backup = (
                    "limits-" + secrets.token_hex(16) + ".bak" if original.raw is not None else None
                )
                if backup is not None:
                    fs.atomic(
                        target.parent / backup,
                        hooks_fd,
                        original.raw or b"",
                        fs.Image(),
                        private=True,
                    )
                receipt = {
                    "version": HOOK_VERSION,
                    "state": "prepared",
                    "settings": str(settings),
                    "cache": str(cache),
                    "script": str(target),
                    "interpreter": str(python),
                    "hash": HOOK_SHA256,
                    "managed": managed,
                    "backup": backup,
                    "before_sha256": original.digest,
                }
                if _previous(hooks_fd, receipt) != before:
                    raise fs.HookFileError("invalid")
                fs.atomic(receipt_path, hooks_fd, fs.encode(receipt), receipt_image, private=True)

                def committed() -> None:
                    nonlocal wrote
                    wrote = True

                try:
                    fs.atomic(
                        settings,
                        settings_fd,
                        fs.encode({**before, "statusLine": managed}),
                        original,
                        private=False,
                        committed=committed,
                    )
                    prepared = fs.read(hooks_fd, receipt_path.name, private=True)
                    fs.atomic(
                        receipt_path,
                        hooks_fd,
                        fs.encode({**receipt, "state": "installed"}),
                        prepared,
                        private=True,
                    )
                except (OSError, ValueError, TypeError, RecursionError, fs.HookFileError):
                    if wrote:
                        try:
                            current = fs.read(settings_fd, settings.name)
                            current_body = fs.document(current.raw or b"{}")
                            if current_body.get("statusLine") != managed:
                                return HookResult("recovery_required", True, True)
                            fs.atomic(
                                settings,
                                settings_fd,
                                fs.encode(_restore_line(current_body, before)),
                                current,
                                private=False,
                            )
                        except (OSError, ValueError, TypeError, RecursionError, fs.HookFileError):
                            return HookResult("recovery_required", True, True)
                    raise
                return HookResult("installed", True, True)
    except (OSError, ValueError, TypeError, RecursionError, fs.HookFileError) as exc:
        return HookResult(_error(exc), wrote, wrote)


def receipt_state(settings: Path, *, hooks_dir: Path | None = None) -> str | None:
    """The install record's state for these settings (read only), or None when there is none."""
    target = (hooks_dir or default_hook_path().parent) / HOOK_FILENAME
    try:
        with fs.directory(target.parent, private=True) as hooks_fd:
            _, receipt = _receipt(hooks_fd, _receipt_name(settings))
    except (OSError, ValueError, TypeError, RecursionError, fs.HookFileError):
        return None
    if receipt is None or receipt["settings"] != str(settings):
        return None
    return str(receipt["state"])


def upgrade_available(
    settings: Path,
    cache: Path,
    *,
    hooks_dir: Path | None = None,
    interpreter: Path | None = None,
) -> bool:
    """Read-only: whether the outdated script is our previous version, recorded by our
    receipt, with our settings line still in place. Anything else must be left alone."""
    try:
        target = (hooks_dir or default_hook_path().parent) / HOOK_FILENAME
        _paths(settings, cache, target)
        python = _interpreter(interpreter)
        managed = {"type": "command", "command": managed_command(target, cache, python)}
        with fs.directory(settings.parent) as settings_fd:
            original = fs.read(settings_fd, settings.name)
            if original.raw is None:
                return False
            before = fs.document(original.raw)
        with fs.directory(target.parent, private=True) as hooks_fd:
            existing = fs.read(hooks_fd, target.name, private=True)
            if existing.raw is None:
                return False
            _, receipt = _receipt(hooks_fd, _receipt_name(settings))
            if receipt is not None:
                _bound(receipt, settings, cache, target, python)
            digest = hashlib.sha256(existing.raw).hexdigest()
            if receipt is None or not _upgradeable(digest, receipt, before, managed):
                return False
            _previous(hooks_fd, receipt)
            return True
    except (OSError, ValueError, TypeError, RecursionError, fs.HookFileError):
        return False


UpgradeOutcome = Literal["upgraded", "not_upgraded", "uncertain"]


def upgrade_outdated_hook(
    settings: Path,
    cache: Path,
    *,
    hooks_dir: Path | None = None,
    interpreter: Path | None = None,
) -> UpgradeOutcome:
    """A locked script-only upgrade: the shipped script plus its receipt hash, nothing else.

    This transaction has no settings, fresh-install, creation or repair branch: every
    path below that is not the two-file commit returns without writing. Under the
    locks it requires an existing allow-listed script, a matching installed receipt,
    a valid backup and the exact managed status line bound here; anything missing or
    changed aborts. "uncertain" means the script rename committed but a later write
    step failed, so the caller must report that honestly instead of claiming either
    a clean upgrade or no change.
    """
    script = (hooks_dir or default_hook_path().parent) / HOOK_FILENAME
    check = HookSetupCheck(settings, cache, script, interpreter=interpreter).check(
        int(time.time() * 1000)
    )
    if check is None or check["state"] != "script_outdated":
        return "not_upgraded"
    script_done = False
    try:
        target = script
        _paths(settings, cache, target)
        python = _interpreter(interpreter)
        body = hook_bytes()
        managed = {"type": "command", "command": managed_command(target, cache, python)}
        with fs.directory(settings.parent) as settings_fd:
            original = fs.read(settings_fd, settings.name)
            if original.raw is None:
                return "not_upgraded"
            before = fs.document(original.raw)
            if before.get("statusLine") != managed:
                return "not_upgraded"
            with fs.directory(target.parent, private=True) as hooks_fd:
                end = time.monotonic() + 0.2
                with (
                    fs.lock(hooks_fd, ".hook-install.lock", deadline=end),
                    fs.lock(settings_fd, "." + _receipt_name(settings) + ".lock", deadline=end),
                ):
                    if fs.read(settings_fd, settings.name) != original:
                        return "not_upgraded"
                    existing = fs.read(hooks_fd, target.name, private=True)
                    if existing.raw is None:
                        return "not_upgraded"
                    digest = hashlib.sha256(existing.raw).hexdigest()
                    if digest == HOOK_SHA256:
                        return "not_upgraded"
                    receipt_path = target.parent / _receipt_name(settings)
                    receipt_image, receipt = _receipt(hooks_fd, receipt_path.name)
                    if receipt is None:
                        return "not_upgraded"
                    _bound(receipt, settings, cache, target, python)
                    if not _upgradeable(digest, receipt, before, managed):
                        return "not_upgraded"
                    _previous(hooks_fd, receipt)
                    if fs.read(settings_fd, settings.name) != original:
                        return "not_upgraded"

                    def _script_done() -> None:
                        nonlocal script_done
                        script_done = True

                    try:
                        fs.atomic(
                            target, hooks_fd, body, existing, private=True, committed=_script_done
                        )
                    except (OSError, ValueError, TypeError, RecursionError, fs.HookFileError):
                        return "uncertain" if script_done else "not_upgraded"
                    try:
                        fs.atomic(
                            receipt_path,
                            hooks_fd,
                            fs.encode({**receipt, "hash": HOOK_SHA256}),
                            receipt_image,
                            private=True,
                        )
                    except (OSError, ValueError, TypeError, RecursionError, fs.HookFileError):
                        return "uncertain"
                    return "upgraded"
    except (OSError, ValueError, TypeError, RecursionError, fs.HookFileError):
        return "uncertain" if script_done else "not_upgraded"


def uninstall_hook(
    settings: Path,
    cache: Path,
    *,
    confirmed: bool = False,
    hooks_dir: Path | None = None,
    interpreter: Path | None = None,
) -> HookResult:
    """Compare the managed field and restore just that field; never replay a backup document."""
    if not confirmed:
        return HookResult("confirmation_required")
    changed = False
    try:
        target = (hooks_dir or default_hook_path().parent) / HOOK_FILENAME
        _paths(settings, cache, target)
        python = _interpreter(interpreter)
        with (
            fs.directory(target.parent, private=True) as hooks_fd,
            fs.directory(settings.parent) as settings_fd,
            fs.lock(hooks_fd, ".hook-install.lock"),
            fs.lock(settings_fd, "." + _receipt_name(settings) + ".lock"),
        ):
            receipt_path = target.parent / _receipt_name(settings)
            image, receipt = _receipt(hooks_fd, receipt_path.name)
            if receipt is None:
                return HookResult("not_installed")
            _bound(receipt, settings, cache, target, python)
            previous = _previous(hooks_fd, receipt)
            current = fs.read(settings_fd, settings.name)
            if current.raw is None:
                return HookResult("conflict")
            body = fs.document(current.raw)
            if ("statusLine" in body, body.get("statusLine")) == (
                "statusLine" in previous,
                previous.get("statusLine"),
            ):
                if receipt["state"] != "uninstalled":
                    fs.atomic(
                        receipt_path,
                        hooks_fd,
                        fs.encode({**receipt, "state": "uninstalled"}),
                        image,
                        private=True,
                    )
                return HookResult("uninstalled")
            if receipt["state"] == "uninstalled" or body.get("statusLine") != receipt["managed"]:
                return HookResult("conflict")

            def committed() -> None:
                nonlocal changed
                changed = True

            fs.atomic(
                settings,
                settings_fd,
                fs.encode(_restore_line(body, previous)),
                current,
                private=False,
                committed=committed,
            )
            fs.atomic(
                receipt_path,
                hooks_fd,
                fs.encode({**receipt, "state": "uninstalled"}),
                image,
                private=True,
            )
            return HookResult("uninstalled", True, True)
    except FileNotFoundError:
        return HookResult("unreadable" if changed else "not_installed", changed, changed)
    except (OSError, ValueError, TypeError, RecursionError, fs.HookFileError) as exc:
        return HookResult(_error(exc), changed, changed)
