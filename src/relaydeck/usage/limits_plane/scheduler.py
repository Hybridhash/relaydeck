"""Limits-plane scheduler.

One worker thread refreshes each configured provider outside the token-ledger
pass: it never takes the collector's pass lock and is not bound by the ledger
50 ms budget. Per provider it enforces `min_interval`, exponential backoff on
error (capped at 30 minutes) with jitter, and a throttle floor so visibility
refreshes and expiry re-fetches cannot hammer a backend.
"""

from __future__ import annotations

import fcntl
import os
import random
import secrets
import select
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypedDict

from relaydeck.usage.contracts import LimitError, LimitWindow, UsageUnavailable
from relaydeck.usage.limits_plane.contracts import (
    FetchContext,
    LimitsProvider,
    ProviderSpec,
    RefreshDecision,
)
from relaydeck.usage.limits_plane.refresh_state import RefreshState
from relaydeck.usage.limits_plane.trust_store import probe_folder
from relaydeck.usage.store import SqliteUsageStore

MAX_FETCH_BYTES = 1024 * 1024  # stdout kept per fetch; the rest is discarded
BACKOFF_BASE_MS = 60_000
BACKOFF_CAP_MS = 30 * 60_000
THROTTLE_FLOOR_MS = 60_000  # minimum gap between attempts forced by refresh/expiry
COOLDOWN_MS = 5 * 60_000  # gap between manual refresh commits, per kind
_IDLE_SLEEP_S = 60.0  # worker wake interval with nothing due (expiry re-check)
AUTO_CHECK_MS = 5 * 60_000  # how often the worker weighs a background read of a pane block


class LimitsState(TypedDict):
    state: str  # "ok" | "error"
    checked_ms: int  # last attempt, success or failure
    error: str | None  # LimitError value when state == "error"


def _kill_group(proc: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except OSError:
        pass
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass  # untouchable child; never block shutdown on it


def run_bounded_command(
    argv: Sequence[str],
    *,
    timeout_s: float,
    extra_env: Mapping[str, str] | None = None,
    max_bytes: int = MAX_FETCH_BYTES,
) -> bytes:
    """Run argv without a shell and return up to `max_bytes` of stdout.

    Own process group, minimal env (HOME, PATH, LANG plus `extra_env`),
    stdin and stderr discarded. A timeout kills the whole process group,
    reaps the child, and raises TimeoutError: a thread timeout alone never
    orphans the child. argv[0] must be absolute; shell strings are rejected.
    """
    args, timeout, env, _ = _checked_launch(argv, timeout_s, extra_env, max_bytes)
    try:
        proc = subprocess.Popen(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=env,
            start_new_session=True,
        )
    except OSError as exc:
        raise UsageUnavailable("unavailable") from exc
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        _kill_group(proc)
        raise TimeoutError("limits_fetch_timeout") from exc
    except BaseException:
        _kill_group(proc)
        raise
    return bytes(out[:max_bytes])


def _checked_launch(
    argv: Sequence[str],
    timeout_s: float,
    extra_env: Mapping[str, str] | None,
    max_bytes: int,
) -> tuple[list[str], float, dict[str, str], int]:
    """Shared validation for both subprocess runners."""
    if isinstance(argv, str | bytes) or not isinstance(argv, Sequence):
        raise ValueError("argv_must_be_a_sequence")
    args = tuple(argv)
    if (
        not args
        or any(not isinstance(item, str) or not item or "\x00" in item for item in args)
        or not os.path.isabs(args[0])
    ):
        raise ValueError("argv_rejected")
    if not isinstance(timeout_s, int | float) or isinstance(timeout_s, bool) or not timeout_s > 0:
        raise ValueError("timeout_rejected")
    if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes < 1:
        raise ValueError("bound_rejected")
    extra = dict(extra_env) if extra_env is not None else {}
    for key, value in extra.items():
        if (
            not isinstance(key, str)
            or not isinstance(value, str)
            or not key
            or "=" in key
            or "\x00" in key + value
        ):
            raise ValueError("env_rejected")
    env: dict[str, str] = {}
    for name in ("HOME", "PATH", "LANG"):
        inherited = os.environ.get(name)
        if inherited is not None:
            env[name] = inherited
    env.update(extra)
    return list(args), float(timeout_s), env, max_bytes


class OutputCapExceeded(Exception):
    """Child stdout passed its byte cap. The session was killed and reaped."""


@dataclass(frozen=True)
class ExchangeStep:
    """One line-session round: write `send` (without newline), then read lines
    until `match` accepts one. `match=None` sends without reading."""

    send: bytes | None
    match: Callable[[bytes], bool] | None = None


def run_line_session(
    argv: Sequence[str],
    steps: Sequence[ExchangeStep],
    *,
    timeout_s: float,
    extra_env: Mapping[str, str] | None = None,
    max_bytes: int = MAX_FETCH_BYTES,
) -> list[bytes | None]:
    """Request/response exchange with a stdio child.

    Same guarantees as `run_bounded_command` (no shell, absolute argv[0],
    minimal env, own process group, stderr discarded), plus: at most
    `max_bytes` of stdout are read across the whole session, `timeout_s` is a
    total deadline, and the child is ALWAYS killed and reaped before return,
    success or failure. Returns the matched line per step (None for
    send-only steps); unmatched lines are dropped but count toward the cap.

    Raises TimeoutError on deadline overrun, OutputCapExceeded past the cap,
    EOFError when the child goes quiet early, UsageUnavailable when the child
    cannot spawn, and ValueError for bad input.
    """
    args, timeout, env, bound = _checked_launch(argv, timeout_s, extra_env, max_bytes)
    if isinstance(steps, str | bytes) or not isinstance(steps, Sequence) or not steps:
        raise ValueError("steps_rejected")
    for step in steps:
        if (
            not isinstance(step, ExchangeStep)
            or (step.send is not None and not isinstance(step.send, bytes))
            or (step.match is not None and not callable(step.match))
        ):
            raise ValueError("steps_rejected")
    try:
        proc = subprocess.Popen(
            args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=env,
            start_new_session=True,
        )
    except OSError as exc:
        raise UsageUnavailable("unavailable") from exc
    assert proc.stdin is not None and proc.stdout is not None
    deadline = time.monotonic() + timeout
    matched: list[bytes | None] = []
    buf = bytearray()
    total = 0
    fd = proc.stdout.fileno()
    flags = fcntl.fcntl(fd, fcntl.F_GETFL)
    fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
    try:
        for step in steps:
            if step.send is not None:
                try:
                    proc.stdin.write(step.send + b"\n")
                    proc.stdin.flush()
                except OSError as exc:
                    raise EOFError("limits_session_ended") from exc
            matcher = step.match
            if matcher is None:
                matched.append(None)
                continue
            while True:
                while True:
                    end = buf.find(b"\n")
                    if end >= 0:
                        break
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("limits_session_timeout")
                    ready, _, _ = select.select([fd], [], [], remaining)
                    if not ready:
                        raise TimeoutError("limits_session_timeout")
                    try:
                        chunk = os.read(fd, 65536)
                    except BlockingIOError:
                        continue
                    if not chunk:
                        raise EOFError("limits_session_ended")
                    buf += chunk
                    if total + len(buf) > bound:
                        raise OutputCapExceeded("limits_session_over_cap")
                line = bytes(buf[:end])
                del buf[: end + 1]
                total += len(line) + 1
                if total > bound:
                    raise OutputCapExceeded("limits_session_over_cap")
                if matcher(line):
                    matched.append(line)
                    break
        return matched
    finally:
        _kill_group(proc)
        try:
            proc.stdin.close()
        except OSError:
            pass
        try:
            proc.stdout.close()
        except OSError:
            pass


class _ProviderState:
    __slots__ = (
        "failures",
        "next_due_ms",
        "last_attempt_ms",
        "checked_ms",
        "last_error",
        "refresh",
        "windows",
        "attempt_id",
        "last_auto_ms",
        "last_auto_check_ms",
        "last_open_ms",
    )

    def __init__(self) -> None:
        self.failures = 0
        self.next_due_ms = 0
        self.last_attempt_ms: int | None = None
        self.checked_ms: int | None = None
        self.last_error: LimitError | None = None
        self.refresh = False
        self.windows: tuple[LimitWindow, ...] = ()
        self.attempt_id: str | None = None  # in-flight manual attempt
        # Background reads: the last one started, the last time the worker weighed one, and the
        # last time a page open did.
        self.last_auto_ms: int | None = None
        self.last_auto_check_ms: int | None = None
        self.last_open_ms: int | None = None


class LimitsScheduler:
    """Refreshes limits providers on one worker thread.

    `clock` returns epoch milliseconds; `sleep` waits seconds (wakeable);
    `jitter` returns 0..1 for backoff spread. A failure keeps the
    previous stored reading (last-good) only for a positively identified account and a transient
    error; an unknown identity or an `auth_required` failure replaces it with an error reading.
    Only a reading for the same account can replace a kept one (the store keys by account).
    """

    def __init__(
        self,
        store: SqliteUsageStore,
        specs: Mapping[str, ProviderSpec],
        providers: Mapping[str, LimitsProvider],
        clock: Callable[[], int] | None = None,
        sleep: Callable[[float], object] | None = None,
        jitter: Callable[[], float] | None = None,
        key_path: Path | None = None,
        refresh: RefreshState | None = None,
        run: Callable[[Sequence[str], float, Mapping[str, str] | None], bytes] | None = None,
    ) -> None:
        if set(specs) != set(providers):
            raise ValueError("limits_wiring_mismatch")
        for key, spec in specs.items():
            provider = providers[key]
            if (
                spec.provider_key != key
                or provider.format_id != spec.format
                or spec.min_interval_ms < 0
                or spec.timeout_s <= 0
            ):
                raise ValueError("limits_wiring_mismatch")
        self._store = store
        self._specs = dict(specs)
        self._providers = dict(providers)
        self._key_path = key_path
        self._refresh = refresh
        self._run_fn = run
        self._states = {key: _ProviderState() for key in specs}
        self._sources: dict[str, tuple[str, ...]] = {}  # source labels each block has written
        self._clock = clock or (lambda: int(time.time() * 1000))
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._open_requested = threading.Event()
        self._sleep = sleep or self._wake.wait
        self._jitter = jitter or random.random
        self._lock = threading.Lock()
        self._lifecycle = threading.Lock()
        self._thread: threading.Thread | None = None

    @property
    def spec_keys(self) -> tuple[str, ...]:
        """Configured provider keys (host/kind/format), for source retirement."""
        return tuple(self._specs)

    def start(self) -> None:
        with self._lifecycle:
            if self._thread is not None and self._thread.is_alive():
                if self._stop.is_set():
                    raise UsageUnavailable("busy")
                return
            self._stop.clear()
            self._wake.clear()
            self._thread = threading.Thread(target=self._run, name="limits-plane", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        with self._lifecycle:
            self._stop.set()
            self._wake.set()
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2)

    def join(self) -> None:
        """Final shutdown fence, called before closing the store."""
        with self._lifecycle:
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join()

    def request_refresh(self, provider_key: str | None = None) -> None:
        """Make a provider (or all) due now; the throttle floor still applies.

        On-demand specs are manual-only: visibility refreshes never trigger them.
        """
        with self._lock:
            if provider_key is None:
                for key, state in self._states.items():
                    if not self._specs[key].on_demand:
                        state.refresh = True
            elif provider_key in self._states and not self._specs[provider_key].on_demand:
                self._states[provider_key].refresh = True
        self._wake.set()

    def request_on_demand(self, provider_key: str) -> RefreshDecision:
        """Start (or join) one manual refresh, enforcing single-flight and the
        persisted cooldown. The attempt runs on the worker; a client disconnect
        never cancels it."""
        now_ms = self._clock()
        with self._lock:
            spec = self._specs.get(provider_key)
            state = self._states.get(provider_key)
            if spec is None or state is None or not spec.on_demand or spec.tui is None:
                return RefreshDecision("not_configured")
            if state.attempt_id is not None:
                return RefreshDecision("in_progress", state.attempt_id)
            until = self._refresh.cooldown_until(spec.kind) if self._refresh else 0
            if now_ms < until:
                retry = (until - now_ms + 999) // 1000
                return RefreshDecision("cooldown", None, retry)
            attempt_id = secrets.token_hex(8)
            state.attempt_id = attempt_id
            state.refresh = True
        self._wake.set()
        return RefreshDecision("started", attempt_id)

    def request_auto_on_open(self) -> int:
        """A client opened the Usage page: read each pane block whose tool was used since its last
        reading, without waiting out the background interval. Each block is weighed at most once per
        cooldown however many devices open the page. Returns how many reads were started."""
        return self._auto_sweep(self._clock(), on_open=True)

    def notify_usage_open(self) -> None:
        """Coalesce HTTP polls without taking scheduler/store locks on their threads."""
        if not self._stop.is_set():
            self._open_requested.set()
            self._wake.set()

    def _auto_sweep(self, now_ms: int, *, on_open: bool = False) -> int:
        started = 0
        for key in sorted(self._specs):
            spec = self._specs[key]
            if not (spec.on_demand and spec.tui is not None and spec.auto_interval_ms > 0):
                continue
            with self._lock:
                state = self._states[key]
                gate = state.last_open_ms if on_open else state.last_auto_check_ms
                if state.attempt_id is not None or now_ms < state.next_due_ms:
                    continue  # one read at a time, and a failure's backoff is honoured
                if gate is not None and now_ms - gate < (COOLDOWN_MS if on_open else AUTO_CHECK_MS):
                    continue
                if on_open:
                    state.last_open_ms = now_ms
                else:
                    state.last_auto_check_ms = now_ms
                last_auto = state.last_auto_ms
            if not on_open and last_auto is not None and now_ms - last_auto < spec.auto_interval_ms:
                continue
            if not self._auto_useful(key, now_ms, wait=not on_open):
                continue
            # The button's own path: single flight, cooldown, journal.
            if self.request_on_demand(key).state == "started":
                with self._lock:
                    self._states[key].last_auto_ms = now_ms
                started += 1
        return started

    def _auto_useful(self, key: str, now_ms: int, *, wait: bool) -> bool:
        """The tool was used after its last reading, and (for a timed read) the interval passed."""
        spec = self._specs[key]
        host = key.split("/", 1)[0]
        try:
            used = self._store.kind_activity(host).get(spec.kind)
            seen = self._store.limits_observed(host, spec.kind)
        except Exception:
            return False
        if used is None or (seen is not None and used <= seen):
            return False
        return not wait or seen is None or now_ms - seen >= spec.auto_interval_ms

    def on_demand_key(self, kind: str) -> str | None:
        """First on-demand provider key for a kind (deterministic order), if any."""
        for key in sorted(self._specs):
            spec = self._specs[key]
            if spec.kind == kind and spec.on_demand and spec.tui is not None:
                return key
        return None

    def refresh_status(self) -> dict[str, dict[str, Any]]:
        """Per-kind manual-refresh state for `limits_refresh`."""
        with self._lock:
            out: dict[str, dict[str, Any]] = {}
            for key in sorted(self._specs):
                spec = self._specs[key]
                if not spec.on_demand or spec.kind in out:
                    continue
                state = self._states[key]
                if spec.tui is None:
                    available, reason = False, "not_configured"
                elif not spec.tui.herdr:
                    available, reason = False, "no_herdr"
                else:
                    available, reason = True, None
                until = self._refresh.cooldown_until(spec.kind) if self._refresh else 0
                last = self._refresh.last_attempt(spec.kind) if self._refresh else None
                probe = (
                    probe_folder(spec.tui.recipe.probe_dir, spec.tui.state_path.parent)
                    if spec.tui is not None
                    else None
                )
                out[spec.kind] = {
                    "available": available,
                    "reason": reason,
                    "in_progress": state.attempt_id is not None,
                    "cooldown_until_ms": until or None,
                    "last_error": state.last_error.value if state.last_error else None,
                    "last_attempt": (
                        {
                            "id": last.attempt_id,
                            "finished_ms": last.finished_ms,
                            "result": last.result,
                        }
                        if last is not None
                        else None
                    ),
                    # Named only when the last read stopped at the trust question.
                    "probe_dir": str(probe)
                    if probe is not None
                    and last is not None
                    and last.result == "workspace_untrusted"
                    else None,
                }
            return out

    def status(self) -> dict[str, LimitsState]:
        """Per-kind fetch state for `kinds[].limits_state`; error wins over ok."""
        with self._lock:
            out: dict[str, LimitsState] = {}
            for key, spec in self._specs.items():
                state = self._states[key]
                if state.checked_ms is None:
                    continue
                entry: LimitsState = {
                    "state": "error" if state.last_error is not None else "ok",
                    "checked_ms": state.checked_ms,
                    "error": state.last_error.value if state.last_error is not None else None,
                }
                prev = out.get(spec.kind)
                if prev is None:
                    out[spec.kind] = entry
                elif entry["state"] == "error" and prev["state"] != "error":
                    out[spec.kind] = entry
                elif entry["state"] == prev["state"] and entry["checked_ms"] >= prev["checked_ms"]:
                    out[spec.kind] = entry
            return out

    def run_once(self, now_ms: int) -> int:
        """One sweep over due providers. Returns the number of fetch attempts."""
        with self._lock:
            due = [key for key in self._specs if self._due_locked(key, now_ms)]
        fetched = 0
        for key in due:
            if self._stop.is_set() and threading.current_thread() is self._thread:
                break
            self._fetch(key, now_ms)
            fetched += 1
        return fetched

    def _due_locked(self, key: str, now_ms: int) -> bool:
        state = self._states[key]
        if self._specs[key].on_demand:
            return state.refresh  # manual-only: never time-due, no first-check run
        if state.last_attempt_ms is None:
            return True  # first check runs immediately
        if now_ms - state.last_attempt_ms >= THROTTLE_FLOOR_MS and (
            state.refresh or self._expired(state.windows, now_ms)
        ):
            return True
        return now_ms >= state.next_due_ms

    @staticmethod
    def _expired(windows: tuple[LimitWindow, ...], now_ms: int) -> bool:
        return any(
            window.resets_at_ms is not None and now_ms >= window.resets_at_ms for window in windows
        )

    def _jittered(self, delay_ms: int) -> int:
        spread = min(1.0, max(0.0, self._jitter()))
        return max(0, delay_ms + int(delay_ms * (spread - 0.5) * 0.4))

    def _fetch(self, key: str, now_ms: int) -> None:
        spec = self._specs[key]
        with self._lock:
            state = self._states[key]
            state.refresh = False
            state.last_attempt_ms = now_ms
            state.checked_ms = now_ms
            attempt_id = (state.attempt_id or "") if spec.on_demand else ""
        linked: str | None = None
        if spec.on_demand and spec.tui is not None:
            host = key.split("/", 1)[0] if "/" in key else ""
            try:
                linked = self._store.recent_linked_workspace(host, spec.kind)
            except Exception:
                linked = None
        ctx = FetchContext(
            key,
            spec.kind,
            spec.format,
            now_ms,
            spec.timeout_s,
            self._run_command,
            command=spec.command,
            key_path=self._key_path,
            attempt_id=attempt_id,
            tui=spec.tui,
            linked_workspace=linked,
            refresh=self._refresh,
        )
        try:
            result = self._providers[key].fetch(ctx)
        except TimeoutError:
            self._failed_attempt(key, now_ms, LimitError.TIMEOUT, "timeout", attempt_id)
            return
        except Exception:
            self._failed_attempt(key, now_ms, LimitError.UNAVAILABLE, "unavailable", attempt_id)
            return
        if result.error is not None:
            code = result.note or result.error.value
            self._failed_attempt(key, now_ms, result.error, code, attempt_id)
            return
        try:
            self._store.apply_limits(spec.kind, key, result.snapshots, now_ms)
        except Exception:
            # Storage failure keeps last-good; it never invents a reading.
            self._failed_attempt(key, now_ms, LimitError.UNAVAILABLE, "unavailable", attempt_id)
            return
        with self._lock:
            state.failures = 0
            state.last_error = None
            state.next_due_ms = now_ms + self._jittered(spec.min_interval_ms)
            state.windows = tuple(
                window for snapshot in result.snapshots for window in snapshot.windows
            )
            self._sources[key] = tuple(
                sorted({snapshot.source for snapshot in result.snapshots if snapshot.source})
            )
            if spec.on_demand:
                state.attempt_id = None
        if spec.on_demand and self._refresh is not None:
            # Cooldown only after the snapshot commit; failures never start it.
            self._refresh.set_cooldown(spec.kind, now_ms + COOLDOWN_MS)
            self._refresh.record_attempt(spec.kind, attempt_id, now_ms, result.note or "ok")

    def _failed_attempt(
        self, key: str, now_ms: int, error: LimitError, code: str, attempt_id: str
    ) -> None:
        self._failed(key, now_ms, error)
        if not self._specs[key].on_demand:
            return
        if self._refresh is not None and attempt_id:
            self._refresh.record_attempt(self._specs[key].kind, attempt_id, now_ms, code)
        with self._lock:
            if self._states[key].attempt_id == attempt_id:
                self._states[key].attempt_id = None

    def _failed(self, key: str, now_ms: int, error: LimitError) -> None:
        # Last-good is reused only for a positively identified account and a transient error. An
        # unknown identity or an authentication failure suppresses the previous reading, so a
        # logged-out or switched account never keeps showing the earlier percentage.
        spec = self._specs[key]
        try:
            self._store.record_limits_failure(
                spec.kind, key, error, now_ms, sources=self._sources.get(key, ())
            )
        except Exception:  # noqa: BLE001 - the failure state below still applies
            pass
        with self._lock:
            state = self._states[key]
            state.failures += 1
            state.last_error = error
            state.checked_ms = now_ms
            delay = min(BACKOFF_CAP_MS, BACKOFF_BASE_MS * 2 ** min(state.failures - 1, 10))
            state.next_due_ms = now_ms + self._jittered(delay)

    def _run_command(
        self, argv: Sequence[str], timeout_s: float, extra: Mapping[str, str] | None
    ) -> bytes:
        if self._run_fn is not None:
            return self._run_fn(argv, timeout_s, extra)
        return run_bounded_command(argv, timeout_s=timeout_s, extra_env=extra)

    def _run_loop_sleep_s(self, now_ms: int) -> float:
        with self._lock:
            # On-demand specs are never time-due (a press wakes the worker), so their unset
            # due time must not make the worker spin.
            dues = [
                state.next_due_ms
                for key, state in self._states.items()
                if not self._specs[key].on_demand
            ]
        if not dues:
            return _IDLE_SLEEP_S
        return min(_IDLE_SLEEP_S, max(0.0, (min(dues) - now_ms) / 1000))

    def _run(self) -> None:
        while not self._stop.is_set():
            self._wake.clear()
            if self._open_requested.is_set():
                self._open_requested.clear()
                self.request_auto_on_open()
            self._auto_sweep(self._clock())
            self.run_once(self._clock())
            if self._stop.is_set():
                break
            self._sleep(self._run_loop_sleep_s(self._clock()))
