"""Out-of-band PTY attachment sessions.

Attachment spawns `herdr agent attach` / `herdr terminal attach` under a
local PTY. The connector pumps bytes between the PTY and the gateway
terminal stream; this module owns only process lifecycle,
sizing, byte transport, explicit takeover planning, and the retained
attachment pool. POSIX-only: the connector runs on the Mac.

Upstream attachment changes ownership and may resize the PTY. Shell
attach first attempts WITHOUT takeover; agent attach always takes
ownership, and the planner requires an explicit takeover=True so the
caller discloses first. Detaching stops
only the local attach client: the remote pane/process survives, and
closing one is a separate explicit action (pane.close via the adapter).
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import selectors
import signal
import struct
import subprocess
import sys
import time
from collections.abc import Callable, Collection
from dataclasses import dataclass, field
from typing import Protocol

if sys.platform == "win32":  # pragma: no cover
    raise ImportError("relaydeck.connector.attach is POSIX-only")

import fcntl
import pty
import termios

# retention baseline: five live terminals per
# host, five-minute idle expiry, visible terminals protected.
RETENTION_SLOTS = 5
RETENTION_IDLE_MS = 5 * 60 * 1000

MAX_REFUSAL_CHARS = 128
MAX_GEOMETRY_CELLS = 1024


class AttachError(RuntimeError):
    code = "attach_failed"


class AttachRefused(AttachError):
    """Upstream refused the attach (terminal owned elsewhere). The hint
    is the sanitized first line of the client's own output: upstream
    refusal text is upstream-defined and never parsed for protocol."""

    code = "lease_conflict"

    def __init__(self, owner_hint: str) -> None:
        detail = f": {owner_hint}" if owner_hint else ""
        super().__init__(f"attach refused{detail}")
        self.owner_hint = owner_hint


class PoolFullError(AttachError):
    """Retention admission failed: every slot is visible, nothing may be
    evicted. Connector-local code; the gateway surfaces this
    as lease_conflict."""

    code = "admission_failed"

    def __init__(self, visible: int) -> None:
        super().__init__(f"terminal retention full: {visible} slots visible, none evictable")
        self.visible = visible


def attach_argv(
    binary: str,
    *,
    target: str | None = None,
    terminal_id: str | None = None,
    takeover: bool = False,
) -> list[str]:
    """Build the argv for exactly one attach mode. No shell involved."""
    if bool(target) == bool(terminal_id):
        raise AttachError("exactly one of target/terminal_id is required")
    if target is not None:
        argv = [binary, "agent", "attach", target]
    else:
        assert terminal_id is not None
        argv = [binary, "terminal", "attach", terminal_id]
    if takeover:
        argv.append("--takeover")
    return argv


def _check_geometry(cols: int, rows: int) -> None:
    if not (1 <= cols <= MAX_GEOMETRY_CELLS and 1 <= rows <= MAX_GEOMETRY_CELLS):
        raise AttachError(f"bad size {cols}x{rows}")


def plan_shell_attach(
    binary: str, terminal_id: str, *, cols: int = 80, rows: int = 24
) -> list[str]:
    """Existing-shell attach WITHOUT takeover (the default). When the
    terminal is already owned this is refused; the caller surfaces the
    holder and asks for an explicit Take Over (attach_argv takeover)."""
    _check_geometry(cols, rows)
    return attach_argv(binary, terminal_id=terminal_id, takeover=False)


def plan_agent_attach(
    binary: str, target: str, *, takeover: bool, cols: int = 80, rows: int = 24
) -> list[str]:
    """Agent attach takes upstream ownership: the caller must pass
    takeover=True explicitly (after disclosing it) — there is no silent
    default that seizes someone else's agent."""
    _check_geometry(cols, rows)
    if takeover is not True:
        raise AttachError("agent attach requires explicit takeover=True")
    return attach_argv(binary, target=target, takeover=True)


def parse_refusal(output: bytes) -> str:
    """Format-agnostic refusal hint: first printable line, truncated.
    Never raises; unknown output yields an empty hint."""
    for raw in output.splitlines():
        text = raw.decode("utf-8", "replace").strip()
        text = "".join(ch for ch in text if ch.isprintable()).strip()
        if text:
            return text[:MAX_REFUSAL_CHARS]
    return ""


@dataclass
class AttachSession:
    argv: list[str]
    cols: int = 80
    rows: int = 24
    _proc: subprocess.Popen[bytes] | None = field(default=None, repr=False)
    _fd: int | None = field(default=None, repr=False)
    exit_status: int | None = None
    capture_stderr: bool = False
    _stderr_prefix: bytearray = field(default_factory=bytearray, repr=False)
    _stderr_seen: bool = False
    _stderr_eof: bool = False
    _stderr_category: str = "unavailable"

    def diagnostic_fileno(self) -> int | None:
        if (
            self.capture_stderr
            and not self._stderr_eof
            and self._proc is not None
            and self._proc.stderr is not None
        ):
            return self._proc.stderr.fileno()
        return None

    def collect_diagnostics(self) -> str:
        """Nonblocking bounded stderr drain; return only a fixed category.

        stdout remains the exact PTY stream. Never interpret/log it as a
        diagnostic. Retain at most 4 KiB of stderr, drain at most 64 KiB
        per readiness turn, and never expose message text or arguments.
        """
        fd = self.diagnostic_fileno()
        if fd is None:
            return self._stderr_category
        drained = 0
        while drained < 65536:
            try:
                chunk = os.read(fd, 65536 - drained)
            except BlockingIOError:
                break
            except OSError:
                self._stderr_eof = True
                break
            if not chunk:
                self._stderr_eof = True
                break
            self._stderr_seen = True
            drained += len(chunk)
            self._stderr_prefix.extend(chunk[: max(0, 4096 - len(self._stderr_prefix))])
        if not self._stderr_seen:
            self._stderr_category = "empty"
            return self._stderr_category
        # Only an exact structured code has a more specific category.
        # Unknown/free-form Herdr diagnostics are not guessed into a cause.
        try:
            doc = json.loads(self._stderr_prefix)
            code = doc["error"]["code"]
        except (ValueError, TypeError, KeyError):
            code = None
        self._stderr_category = (
            code
            if code
            in (
                "agent_not_found",
                "terminal_not_found",
                "terminal_owned",
                "protocol_mismatch",
                "connection_failed",
            )
            else "diagnostic_output"
        )
        return self._stderr_category

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def spawn(self) -> None:
        if self._proc is not None:
            raise AttachError("already spawned")
        master, slave = pty.openpty()
        try:
            self._set_size_fd(slave, self.cols, self.rows)
            self._proc = subprocess.Popen(
                self.argv,
                stdin=slave,
                stdout=slave,
                stderr=subprocess.PIPE if self.capture_stderr else slave,
                close_fds=True,
            )
        except Exception:
            os.close(master)
            os.close(slave)
            raise
        os.close(slave)
        self._fd = master
        self._set_nonblocking(master)
        diagnostic_fd = self.diagnostic_fileno()
        if diagnostic_fd is not None:
            self._set_nonblocking(diagnostic_fd)
            self._stderr_category = "empty"

    @staticmethod
    def _set_nonblocking(fd: int) -> None:
        flags = fcntl.fcntl(fd, fcntl.F_GETFL)
        fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

    @staticmethod
    def _set_size_fd(fd: int, cols: int, rows: int) -> None:
        if not (1 <= cols <= 1024 and 1 <= rows <= 1024):
            raise AttachError(f"bad size {cols}x{rows}")
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

    def resize(self, cols: int, rows: int) -> None:
        if self._fd is None:
            raise AttachError("not spawned")
        self._set_size_fd(self._fd, cols, rows)
        self.cols, self.rows = cols, rows
        if self._proc is not None and self._proc.poll() is None:
            try:
                self._proc.send_signal(signal.SIGWINCH)
            except Exception:  # noqa: BLE001 - best-effort signal
                pass

    def read(self, max_bytes: int = 65536, timeout: float = 5.0) -> bytes:
        if self._fd is None:
            raise AttachError("not spawned")
        with selectors.DefaultSelector() as selector:
            selector.register(self._fd, selectors.EVENT_READ)
            ready = selector.select(timeout)
            if not ready:
                return b""
        return self.read_available(max_bytes)

    def fileno(self) -> int:
        """Live descriptor for event-loop readability; never transferred/owned."""
        if self._fd is None:
            raise AttachError("not spawned")
        return self._fd

    def read_available(self, max_bytes: int = 65536) -> bytes:
        """One nonblocking read; b'' means EAGAIN/EOF, not a wait interval."""
        if self._fd is None:
            raise AttachError("not spawned")
        try:
            data = os.read(self._fd, max_bytes)
        except OSError:
            data = b""
        if data == b"" and self._proc is not None and self._proc.poll() is not None:
            self.exit_status = self._proc.returncode
        return data

    def write(self, data: bytes) -> int:
        if self._fd is None:
            raise AttachError("not spawned")
        if self._proc is not None and self._proc.poll() is not None:
            raise AttachError("process exited")
        total = 0
        view = memoryview(data)
        deadline = time.monotonic() + 5.0
        while total < len(view):
            try:
                written = os.write(self._fd, view[total:])
            except BlockingIOError:
                if time.monotonic() > deadline:
                    raise AttachError("pty write timeout") from None
                time.sleep(0.005)
                continue
            total += written
        return total

    async def write_async(self, data: bytes) -> int:
        """One bounded, cancellable PTY write; partial failures are not replayed."""
        if self._fd is None or not self.running:
            raise AttachError("process not running")
        total = 0
        with_timeout = asyncio.timeout(5.0)
        async with with_timeout:
            while total < len(data):
                try:
                    written = os.write(self._fd, memoryview(data)[total:])
                except BlockingIOError:
                    await asyncio.sleep(0.005)
                    continue
                if written <= 0:
                    raise AttachError("PTY write stopped; outcome uncertain")
                total += written
        return total

    def expect_ready(self, marker: bytes, timeout: float = 5.0) -> bytes:
        """Read until the attach banner `marker`. If the attach client
        exits first, raise AttachRefused with the sanitized output hint;
        if it stays silent past `timeout`, raise AttachError."""
        if self._fd is None:
            raise AttachError("not spawned")
        out = bytearray()
        deadline = time.monotonic() + timeout
        while marker not in out:
            if self._proc is not None and self._proc.poll() is not None:
                self.exit_status = self._proc.returncode
                return self._refuse(bytes(out))
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AttachError("attach timed out waiting for ready marker")
            out += self.read(timeout=min(remaining, 0.2))
        return bytes(out)

    def _refuse(self, prefix: bytes) -> bytes:
        tail = bytearray()
        for _ in range(16):
            chunk = self.read(timeout=0)
            if not chunk:
                break
            tail += chunk
            if len(tail) > 65536:
                break
        raise AttachRefused(parse_refusal(bytes(prefix) + bytes(tail)))

    def detach(self, timeout: float = 5.0) -> int | None:
        """Detach the local attach client. The remote pane/process
        survives: this only stops `herdr ... attach`, never issues
        pane.close (explicit close only)."""
        return self.close(timeout=timeout)

    def close(self, timeout: float = 5.0) -> int | None:
        proc, self._proc = self._proc, None
        fd, self._fd = self._fd, None
        try:
            if proc is not None and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=timeout)
            if proc is not None:
                self.exit_status = proc.returncode
        finally:
            if proc is not None and proc.stderr is not None:
                proc.stderr.close()
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
        return self.exit_status


@dataclass
class ObserveSession(AttachSession):
    """Pinned `terminal session observe` JSON/ANSI output, never a writer.

    Herdr 0.9.1 emits base64 ANSI frames, not raw PTY output. Decode only
    that envelope; malformed/oversized data fails closed. Closing the old
    attach client lets Herdr restore its recorded local geometry controller.
    """

    _pending: bytearray = field(default_factory=bytearray, repr=False)
    _decoded: bytearray = field(default_factory=bytearray, repr=False)

    @property
    def buffered_output(self) -> bool:
        return bool(self._decoded)

    def read(self, max_bytes: int = 65536, timeout: float = 5.0) -> bytes:
        if self._decoded:
            return self.read_available(max_bytes)
        return super().read(max_bytes, timeout)

    def read_available(self, max_bytes: int = 65536) -> bytes:
        try:
            return self._read_frames(max_bytes)
        except (ValueError, TypeError, KeyError, AttributeError, AttachError) as exc:
            self.close()
            raise AttachError("invalid observer output") from exc

    def _read_frames(self, max_bytes: int) -> bytes:
        if self._decoded:
            result = bytes(self._decoded[:max_bytes])
            del self._decoded[:max_bytes]
            return result
        self._pending.extend(super().read_available(max_bytes))
        if len(self._pending) > 2 * 1024 * 1024:
            raise AttachError("observer frame too large")
        output = bytearray()
        while b"\n" in self._pending:
            line, _, rest = self._pending.partition(b"\n")
            self._pending = bytearray(rest)
            frame = json.loads(line)
            if frame.get("type") == "terminal.closed":
                continue
            if frame.get("type") != "terminal.frame" or frame.get("encoding") != "ansi":
                raise AttachError("invalid observer frame")
            data = base64.b64decode(frame["bytes"], validate=True)
            if len(data) > 1024 * 1024:
                raise AttachError("observer output too large")
            output.extend(data)
        self._decoded.extend(output)
        result = bytes(self._decoded[:max_bytes])
        del self._decoded[:max_bytes]
        return result

    def resize(self, cols: int, rows: int) -> None:
        raise AttachError("observer has no resize authority")

    def write(self, data: bytes) -> int:
        raise AttachError("observer has no input authority")

    async def write_async(self, data: bytes) -> int:
        raise AttachError("observer has no input authority")


def apply_controller_resize(session: AttachSession, cols: int, rows: int) -> tuple[int, int]:
    """Apply a controller-authorized PTY geometry change.

    The caller must hold the live control lease: viewers never reach
    here (the gateway rejects their resize frames). Upstream
    `pane.resize` is split-layout only (direction/amount) and carries no
    PTY geometry, so it is never used here; sizing flows through the
    attach client's local PTY winsize plus SIGWINCH, following the
    CLI-subprocess design. Returns the applied (cols, rows).
    """
    _check_geometry(cols, rows)
    session.resize(cols, rows)
    return (session.cols, session.rows)


class PtySession(Protocol):
    """Minimal retained-session surface: AttachSession satisfies this
    (its close() takes an optional timeout)."""

    def close(self) -> int | None: ...


@dataclass
class _Retained:
    session: PtySession
    last_active_ms: int
    idle_since_ms: int | None = None


class AttachmentPool:
    """Retained upstream attachments.

    At most RETENTION_SLOTS live attach clients per host. `release`
    detaches a view (Back navigation): the session stays live upstream
    and keeps buffering. Idle sessions past RETENTION_IDLE_MS are
    evicted; a full pool evicts the least-recently-active non-visible
    session instead of failing. Visible terminals are never evicted: if
    every slot is visible, admission fails with PoolFullError.

    Eviction and `close` stop only the local attach client; the remote
    pane survives both (close the pane explicitly via pane.close).
    Teardown is awaited before the slot is released.
    """

    def __init__(self, *, slots: int = RETENTION_SLOTS, idle_ms: int = RETENTION_IDLE_MS) -> None:
        if slots < 1:
            raise AttachError("retention slots must be >= 1")
        self._slots = slots
        self._idle_ms = idle_ms
        self._sessions: dict[str, _Retained] = {}
        self._visible: frozenset[str] = frozenset()

    def __len__(self) -> int:
        return len(self._sessions)

    def keys(self) -> tuple[str, ...]:
        return tuple(self._sessions)

    def set_visible(self, keys: Collection[str]) -> None:
        self._visible = frozenset(keys)

    def acquire(
        self,
        key: str,
        factory: Callable[[], PtySession],
        now_ms: int,
        *,
        allow_eviction: bool = True,
    ) -> PtySession:
        """Get the live session for `key`, creating it via `factory`
        (which must spawn and return a ready session) on a miss.

        With `allow_eviction=False`, a full pool refuses admission before
        invoking the factory; existing sessions remain untouched.
        """
        hit = self._sessions.get(key)
        if hit is not None:
            hit.idle_since_ms = None
            hit.last_active_ms = now_ms
            return hit.session
        if len(self._sessions) >= self._slots:
            if not allow_eviction:
                raise PoolFullError(len(self._sessions))
            victim = self._eviction_candidate()
            if victim is None:
                visible = sum(1 for k in self._sessions if k in self._visible)
                raise PoolFullError(visible)
            self._drop(victim)
        session = factory()
        self._sessions[key] = _Retained(session, now_ms)
        return session

    def release(self, key: str, now_ms: int) -> None:
        """Detach a view: the session stays live upstream (retained,
        still buffering) but becomes idle-evictable. Idempotent."""
        hit = self._sessions.get(key)
        if hit is None:
            return
        hit.idle_since_ms = now_ms
        hit.last_active_ms = now_ms

    def evict_idle(self, now_ms: int) -> tuple[str, ...]:
        """Close and drop idle sessions past the TTL. Visible sessions
        are protected even when idle."""
        dead = [
            key
            for key, retained in self._sessions.items()
            if key not in self._visible
            and retained.idle_since_ms is not None
            and now_ms - retained.idle_since_ms >= self._idle_ms
        ]
        for key in dead:
            self._drop(key)
        return tuple(dead)

    def close(self, key: str) -> None:
        """Explicit close of the local attach client now (the remote
        pane survives). Idempotent."""
        self._drop(key)

    def close_all(self) -> None:
        for key in list(self._sessions):
            self._drop(key)

    def _eviction_candidate(self) -> str | None:
        """Longest-idle non-visible session first, else the
        least-recently-active non-visible one. Visible: never."""
        idle_best: str | None = None
        idle_ms = 0
        active_best: str | None = None
        active_ms = 0
        for key, retained in self._sessions.items():
            if key in self._visible:
                continue
            if retained.idle_since_ms is not None:
                if idle_best is None or retained.idle_since_ms < idle_ms:
                    idle_best, idle_ms = key, retained.idle_since_ms
            elif active_best is None or retained.last_active_ms < active_ms:
                active_best, active_ms = key, retained.last_active_ms
        return idle_best if idle_best is not None else active_best

    def _drop(self, key: str) -> None:
        retained = self._sessions.get(key)
        if retained is None:
            return
        try:
            retained.session.close()
        finally:
            self._sessions.pop(key, None)
