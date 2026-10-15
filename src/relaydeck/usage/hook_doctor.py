"""Immediate, offline, content-free installation/cache evidence; never writes."""

from __future__ import annotations

import math
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Protocol

from relaydeck.usage import hook_files as fs
from relaydeck.usage.contracts import LIMIT_STALE_MS
from relaydeck.usage.hook_setup import HOOK_FILENAME, HookSetupCheck, default_hook_path


class HookSource(Protocol):
    @property
    def kind(self) -> str: ...
    @property
    def format(self) -> str: ...
    @property
    def limits_format(self) -> str | None: ...
    @property
    def limits_settings(self) -> Path | None: ...
    @property
    def limits_cache(self) -> Path | None: ...


def cache_state(cache: Path | None, now_ms: int) -> str:
    if cache is None:
        return "missing"
    try:
        with fs.directory(cache.parent, private=True) as fd:
            image = fs.read(fd, cache.name, private=True, bound=64 * 1024)
        if image.raw is None:
            return "missing"
        body = fs.document(image.raw)
        if (
            set(body) != {"captured_ms", "windows"}
            or type(body["captured_ms"]) is not int
            or not 0 <= body["captured_ms"] <= now_ms
            or not isinstance(body["windows"], dict)
            or not body["windows"]
            or set(body["windows"]) - {"five_hour", "seven_day"}
        ):
            return "invalid"
        expired = False
        for row in body["windows"].values():
            if (
                not isinstance(row, dict)
                or set(row) != {"used_percentage", "resets_at"}
                or type(row["used_percentage"]) not in (int, float)
                or not math.isfinite(row["used_percentage"])
                or not 0 <= row["used_percentage"] <= 100
                or type(row["resets_at"]) is not int
                or not 0 <= row["resets_at"] <= 253402300799
            ):
                return "invalid"
            expired |= now_ms >= row["resets_at"] * 1000
        return (
            "expired"
            if expired
            else "stale"
            if now_ms - body["captured_ms"] > LIMIT_STALE_MS
            else "fresh"
        )
    except FileNotFoundError:
        return "missing"
    except (OSError, ValueError, TypeError, OverflowError, RecursionError, fs.HookFileError):
        return "invalid"


def doctor_hook(
    settings: Path | None,
    cache: Path | None,
    *,
    hooks_dir: Path | None = None,
    interpreter: Path | None = None,
    now_ms: int | None = None,
) -> dict[str, Any]:
    now = int(time.time() * 1000) if now_ms is None else now_ms
    script = (hooks_dir or default_hook_path().parent) / HOOK_FILENAME
    # New checker per invocation: immediate, independent of runtime's 60s throttle.
    check = HookSetupCheck(settings, cache, script, interpreter=interpreter)
    return {
        "installation": check.check(now),
        "cache": {"state": cache_state(cache, now), "checked_ms": now},
    }


def _diagnosis(source: HookSource, hooks_dir: Path | None, now: int | None) -> dict[str, Any]:
    from relaydeck.usage.hook_repair import diagnose_source

    return diagnose_source(source, hooks_dir=hooks_dir, now_ms=now).public()


def doctor_sources(
    sources: Iterable[HookSource],
    *,
    hooks_dir: Path | None = None,
    now_ms: int | None = None,
) -> list[dict[str, Any]]:
    return [
        {
            "kind": source.kind,
            "format": source.format,
            **doctor_hook(
                source.limits_settings, source.limits_cache, hooks_dir=hooks_dir, now_ms=now_ms
            ),
            "diagnosis": _diagnosis(source, hooks_dir, now_ms),
        }
        for source in sources
        if source.limits_format == "json-statusline-limits-v1"
    ]
