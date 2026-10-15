"""Connector tunnel link: gateway socket + journaled dispatch.

`ConnectorLink` owns one host link: it opens the authenticated
`/v1/connectors` tunnel, pushes the source snapshot/seed events, then
receives `command.dispatch` intents, verifies the digest, journals via
`ConnectorDispatch`, and reports `command.ack` / `command.result`. A
digest mismatch or malformed intent is never executed and never acked:
the tunnel drops (fail closed) and reconnects with backoff, and the
journal's UUID dedupe makes redelivery idempotent.

The link runs against the `LinkSource` port (source.py): fixture and
production Herdr sources are interchangeable here. A source data
failure (unbootstrapped/invalid snapshot) ends the run as
`source-failed` instead of reconnecting forever. Live sources
(`LiveSource`) additionally stream post-bootstrap upstream events and
PTY bytes on descriptor readiness, resync content on reconnect, and rebootstrap
(new epoch) after upstream death.

Reconnect policy: transport failures and protocol drops reconnect with
seeded exponential backoff (attempts reset on every `hello`); a
rejected handshake or an `unauthenticated` close means the credential
is dead and the link exits instead of retrying. Repeated initial-push
transport failures trip a circuit breaker (`push-failed` exit); an
over-bound snapshot with no gateway chunked intake exits immediately
as `snapshot-too-large` (fail loudly, never churn). Shutdown is
graceful: the link sends `bye`, closes the socket, and closes the
journal.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import random
import uuid
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from typing import Any, Protocol

from websockets.asyncio.client import ClientConnection

from relaydeck.connector.herdr import HerdrError
from relaydeck.connector.journal import ACK_KIND, ConnectorDispatch
from relaydeck.connector.source import LinkSource, LiveSource
from relaydeck.contracts.schemas import SchemaError, validate
from relaydeck.contracts.tunnel import SEND_TIMEOUT_S, parse_terminal_input
from relaydeck.journal import CommandJournal, digest_for

__all__ = [
    "CHUNK_BYTES",
    "DEFAULT_JOURNAL_PATH",
    "MAX_SNAPSHOT_BYTES",
    "PUSH_FAIL_LIMIT",
    "TUNNEL_MAX_TEXT",
    "LinkConfig",
    "LinkError",
    "LinkOutcome",
    "ConnectorLink",
    "TunnelTransport",
]

DEFAULT_JOURNAL_PATH = "~/.config/relaydeck/connector-journal.db"

LOG = logging.getLogger("relaydeck.connector.link")

# Reconnect backoff bounds (mirrors the client sync constants; the
# connector cannot import client code, see test_import_boundaries).
BACKOFF_BASE_MS = 500
BACKOFF_FACTOR = 2.0
BACKOFF_MAX_MS = 30_000
BACKOFF_JITTER = 0.25

HELLO_TIMEOUT_S = 10.0
RECV_POLL_S = 1.0

# Gateway tunnel bounds, mirrored (gateway/app.py): the link pre-checks
# these BEFORE sending so an over-bound snapshot fails loudly here
# instead of wedging the session in a hello->push->1008 loop.
TUNNEL_MAX_TEXT = 64 * 1024
MAX_SNAPSHOT_BYTES = 1024 * 1024

# Circuit breaker: consecutive sessions dying inside the initial push
# (transport errors only; upstream Herdr outages retry with backoff).
PUSH_FAIL_LIMIT = 5

# Live metadata tick: gateway-recv wait for live sources, plus upstream
# event drain budget. Readable-source PTY collection runs independently.
LIVE_TICK_S = 0.1
LIVE_DRAIN_S = 0.05
# Bound output publication batching, not PTY reads. Ready descriptors are
# drained immediately while these few milliseconds coalesce a repaint.
OUTPUT_COALESCE_S = 0.005

# Raw snapshot bytes per chunked-transfer frame (b64 inflates ~4/3, so
# frames stay well under TUNNEL_MAX_TEXT with envelope).
CHUNK_BYTES = 32 * 1024
MAX_INPUT_TRACKERS = 1024


class LinkError(RuntimeError):
    pass


class _ProtocolDrop(LinkError):
    """Tunnel bytes failed closed (tamper/malformed/unknown kind)."""


class _SnapshotTooLarge(LinkError):
    """Snapshot exceeds the tunnel bound with no chunked intake."""

    def __init__(self, raw_bytes: int, text_chars: int) -> None:
        super().__init__(
            f"snapshot too large: {raw_bytes} bytes / {text_chars} chars "
            f"(tunnel bound {TUNNEL_MAX_TEXT} chars, no chunked intake)"
        )
        self.raw_bytes = raw_bytes
        self.text_chars = text_chars


@dataclass(frozen=True)
class LinkOutcome:
    # shutdown | once-done | auth-failed | retries-exhausted |
    # source-failed | push-failed | snapshot-too-large
    reason: str
    sessions: int = 0
    dispatches: int = 0
    results: int = 0
    pushed_snapshot: bool = False
    pushed_events: int = 0
    pushed_outputs: int = 0
    pushed_chunks: int = 0


@dataclass(frozen=True)
class LinkConfig:
    gateway_url: str
    host_id: str
    host_token: str
    journal_path: str = DEFAULT_JOURNAL_PATH
    # Seed events for NON-live sources; live sources drain their own
    # seeds (bootstrap/rebootstrap batches) instead.
    events: tuple[dict[str, Any], ...] = ()
    once: bool = False
    once_timeout_s: float = 10.0
    max_retries: int | None = None
    ping_interval_s: float = 30.0
    backoff_seed: int | None = None
    backoff_base_ms: float = BACKOFF_BASE_MS
    backoff_max_ms: float = BACKOFF_MAX_MS
    push_fail_limit: int = PUSH_FAIL_LIMIT
    clock: Callable[[], int] | None = None


class TunnelTransport(Protocol):
    async def send_text(self, text: str) -> None: ...
    async def recv_text(self) -> str: ...
    async def close(self) -> None: ...


def _ws_url(gateway_url: str) -> str:
    base = gateway_url.rstrip("/")
    if base.startswith("https://"):
        return "wss://" + base[len("https://") :] + "/v1/connectors"
    if base.startswith("http://"):
        return "ws://" + base[len("http://") :] + "/v1/connectors"
    raise LinkError(f"gateway URL must be http(s): {gateway_url!r}")


class _PongTrackedConnection(ClientConnection):
    """Pinned websockets 16 connection factory; observe solicited wire pongs."""

    last_pong_at: float | None = None

    def acknowledge_pings(self, data: bytes) -> None:
        if data in self.pending_pings:
            self.last_pong_at = self.loop.time()
        super().acknowledge_pings(data)


class _WebsocketTransport:
    """Real tunnel over `websockets` (header auth, never query tokens)."""

    def __init__(self, url: str, token: str) -> None:
        self._url = url
        self._token = token
        self._ws: Any = None

    async def open(self) -> None:
        import websockets.asyncio.client as ws_client

        try:
            self._ws = await ws_client.connect(
                self._url,
                additional_headers={"Authorization": f"Bearer {self._token}"},
                compression=None,
                max_size=TUNNEL_MAX_TEXT,
                max_queue=16,
                open_timeout=HELLO_TIMEOUT_S,
                close_timeout=SEND_TIMEOUT_S,
                create_connection=_PongTrackedConnection,
            )
        except Exception as exc:
            if type(exc).__name__ in ("InvalidStatus", "InvalidHandshake", "InvalidURI"):
                raise LinkError(f"handshake rejected: {exc}") from exc
            raise

    async def send_text(self, text: str) -> None:
        assert self._ws is not None
        await self._ws.send(text)

    async def recv_text(self) -> str:
        assert self._ws is not None
        message = await self._ws.recv()
        if isinstance(message, bytes):
            raise _ProtocolDrop("binary frame on command tunnel")
        if not isinstance(message, str):
            raise _ProtocolDrop("tunnel message is not text")
        return message

    async def close(self) -> None:
        ws, self._ws = self._ws, None
        if ws is not None:
            try:
                await ws.close()
            except Exception:  # noqa: BLE001 - close is best-effort
                pass


def _close_reason(exc: BaseException) -> tuple[int | None, str]:
    rcvd = getattr(exc, "rcvd", None)
    code = getattr(rcvd, "code", None)
    reason = str(getattr(rcvd, "reason", "") or "")
    return (int(code) if isinstance(code, int) else None, reason)


def _safe_exception_type(exc: BaseException | None) -> str:
    name = type(exc).__name__
    return (
        name
        if name
        in {
            "ConnectionClosed",
            "ConnectionClosedOK",
            "ConnectionClosedError",
            "TimeoutError",
            "OSError",
            "ConnectionResetError",
            "BrokenPipeError",
            "EOFError",
            "ProtocolError",
            "CancelledError",
        }
        else "other"
        if exc is not None
        else "none"
    )


class ConnectorLink:
    """One host link: tunnel sessions + journaled dispatch + backoff."""

    def __init__(
        self,
        config: LinkConfig,
        source: LinkSource,
        *,
        journal: CommandJournal | None = None,
        opener: Callable[[], Coroutine[Any, Any, TunnelTransport]] | None = None,
        sleep: Callable[[float], Coroutine[Any, Any, None]] | None = None,
    ) -> None:
        if not config.gateway_url or not config.host_id or not config.host_token:
            raise LinkError("gateway URL, host id, and host token are required")
        self._config = config
        self._source = source
        self._journal = journal or CommandJournal(
            os.path.expanduser(config.journal_path), clock=config.clock
        )
        self._own_journal = journal is None
        self._dispatch = ConnectorDispatch(
            self._journal,
            source.dispatch_adapter,
            host_id=config.host_id,
            clock=config.clock,
            epoch_probe=lambda: str(source.snapshot_document(config.host_id)["host"]["epoch"]),
        )
        self._opener = opener or self._open_transport
        self._sleep = sleep or asyncio.sleep
        self._random = random.Random(config.backoff_seed)
        self._sessions = 0
        self._dispatches = 0
        self._results = 0
        self._pushed_snapshot = False
        self._pushed_events = 0
        self._pushed_outputs = 0
        self._pushed_chunks = 0
        self._push_failures = 0
        self._source_stale = False
        self._chunked_ok = False
        self._last_epochs: dict[str, str] = {}
        self._input_highwater: dict[tuple[str, str, str, int], int] = {}
        self._output_send_lock = asyncio.Lock()

    async def _open_transport(self) -> TunnelTransport:
        transport = _WebsocketTransport(_ws_url(self._config.gateway_url), self._config.host_token)
        await transport.open()
        return transport

    @property
    def source(self) -> LinkSource:
        return self._source

    def _backoff_ms(self, attempts: int) -> float:
        delay = min(
            self._config.backoff_max_ms, self._config.backoff_base_ms * (BACKOFF_FACTOR**attempts)
        )
        spread = delay * BACKOFF_JITTER
        return max(0.0, delay + self._random.uniform(-spread, spread))

    async def run(self, stop: asyncio.Event) -> LinkOutcome:
        """Run sessions until shutdown, auth failure, or retry exhaustion."""
        attempts = 0
        try:
            while True:
                if stop.is_set():
                    return self._outcome("shutdown")
                try:
                    session_done = await self._session(stop)
                except LinkError as exc:
                    if "handshake rejected" in str(exc):
                        LOG.error("tunnel rejected (credential dead, not retrying): %s", exc)
                        return self._outcome("auth-failed")
                    raise
                except (TimeoutError, OSError) as exc:
                    LOG.warning("tunnel transport failed: %s", _safe_exception_type(exc))
                    session_done = "drop"
                except (ValueError, HerdrError) as exc:
                    if getattr(exc, "code", "") == "output_overflow":
                        # One terminal's output burst is never a source failure: reconnect
                        # (the source resyncs that terminal), keep every other stream.
                        LOG.warning("terminal output overflow, reconnecting")
                        session_done = "drop"
                    else:
                        # Source data failure (unbootstrapped/invalid
                        # snapshot): deterministic, retrying is pointless.
                        LOG.error("source failed: %s", exc)
                        return self._outcome("source-failed")
                if session_done == "auth-failed":
                    return self._outcome("auth-failed")
                if session_done == "push-failed":
                    return self._outcome("push-failed")
                if session_done == "snapshot-too-large":
                    return self._outcome("snapshot-too-large")
                if session_done == "once-done":
                    return self._outcome("once-done")
                if session_done == "stop":
                    return self._outcome("shutdown")
                # "drop": reconnect with backoff (attempts reset on hello).
                if self._config.max_retries is not None and attempts >= self._config.max_retries:
                    return self._outcome("retries-exhausted")
                delay_ms = self._backoff_ms(attempts)
                attempts += 1
                LOG.info("tunnel down, retry %d in %.0f ms", attempts, delay_ms)
                try:
                    await asyncio.wait_for(self._sleep(delay_ms / 1000), timeout=None)
                except asyncio.CancelledError:
                    return self._outcome("shutdown")
                if stop.is_set():
                    return self._outcome("shutdown")
        finally:
            if self._own_journal:
                self._journal.close()

    def _outcome(self, reason: str) -> LinkOutcome:
        return LinkOutcome(
            reason=reason,
            sessions=self._sessions,
            dispatches=self._dispatches,
            results=self._results,
            pushed_snapshot=self._pushed_snapshot,
            pushed_events=self._pushed_events,
            pushed_outputs=self._pushed_outputs,
            pushed_chunks=self._pushed_chunks,
        )

    async def _session(self, stop: asyncio.Event) -> str:
        """One tunnel session. Returns stop | drop | once-done |
        auth-failed | push-failed | snapshot-too-large."""
        transport = await self._opener()
        self._last_application_pong_at: float | None = None
        try:
            try:
                raw_hello = await asyncio.wait_for(transport.recv_text(), HELLO_TIMEOUT_S)
            except TimeoutError as exc:
                raise _ProtocolDrop("no hello from gateway") from exc
            if len(raw_hello.encode()) > TUNNEL_MAX_TEXT:
                raise _ProtocolDrop("hello too large")
            try:
                hello = json.loads(raw_hello)
            except ValueError as exc:
                raise _ProtocolDrop("hello is not JSON") from exc
            if not isinstance(hello, dict) or hello.get("kind") != "hello":
                raise _ProtocolDrop("first message is not hello")
            if hello.get("host_id") != self._config.host_id:
                raise _ProtocolDrop("hello host mismatch")
            self._sessions += 1
            self._apply_hello_extras(hello)
            prepared = await self._prepare_source()
            if prepared == "drop":
                return "drop"
            try:
                await self._initial_push(transport)
            except _SnapshotTooLarge as exc:
                LOG.error("snapshot rejected, exiting (no churn): %s", exc)
                return "snapshot-too-large"
            except (TimeoutError, OSError) as exc:
                self._log_tunnel_failure(transport, exc, "initial_push")
                return self._note_push_failure(exc)
            except BaseException as exc:
                if type(exc).__name__ in (
                    "ConnectionClosedOK",
                    "ConnectionClosedError",
                    "ConnectionClosed",
                ):
                    code, reason = _close_reason(exc)
                    self._log_tunnel_failure(transport, exc, "initial_push")
                    if code == 1008 and reason == "unauthenticated":
                        return "auth-failed"
                    return self._note_push_failure(exc)
                raise
            self._push_failures = 0
            if self._config.once:
                return await self._drain_once(transport, stop)
            return await self._serve(transport, stop)
        except _ProtocolDrop as exc:
            LOG.warning("tunnel failed closed: %s", exc)
            return "drop"
        except BaseException as exc:
            if type(exc).__name__ in (
                "ConnectionClosedOK",
                "ConnectionClosedError",
                "ConnectionClosed",
            ):
                code, reason = _close_reason(exc)
                self._log_tunnel_failure(transport, exc, "stream")
                if code == 1008 and reason == "unauthenticated":
                    return "auth-failed"
                return "drop"
            if isinstance(exc, TimeoutError | OSError):
                self._log_tunnel_failure(transport, exc, "session")
            raise
        finally:
            try:
                await asyncio.wait_for(transport.close(), SEND_TIMEOUT_S)
            except (OSError, TimeoutError):
                pass

    async def _send(self, transport: TunnelTransport, doc: dict[str, Any]) -> None:
        text = json.dumps(doc, separators=(",", ":"))
        if len(text.encode()) > TUNNEL_MAX_TEXT:
            raise _ProtocolDrop("outbound tunnel frame too large")
        await self._send_text(transport, text)

    async def _send_text(self, transport: TunnelTransport, text: str) -> None:
        try:
            await asyncio.wait_for(transport.send_text(text), SEND_TIMEOUT_S)
        except TimeoutError as exc:
            self._log_tunnel_failure(transport, exc, "send_timeout")
            raise

    def _log_tunnel_failure(
        self, transport: TunnelTransport, exc: BaseException, stage: str
    ) -> None:
        """No exception/reason text, URLs, payloads or credentials in diagnostics."""
        code, _ = _close_reason(exc)
        sent = getattr(exc, "sent", None)
        sent_code = getattr(sent, "code", None)
        sent_code = int(sent_code) if isinstance(sent_code, int) else None
        cause = (
            "keepalive_timeout"
            if getattr(sent, "reason", None) == "keepalive ping timeout"
            else _safe_exception_type(exc.__cause__)
        )
        now = asyncio.get_running_loop().time()
        ws = transport._ws if isinstance(transport, _WebsocketTransport) else None
        last_pong = getattr(ws, "last_pong_at", None)
        last_app_pong = getattr(self, "_last_application_pong_at", None)
        LOG.warning(
            "tunnel closed stage=%s exception=%s code=%s sent_code=%s cause=%s "
            "last_pong_age_s=%s last_app_pong_age_s=%s",
            stage,
            _safe_exception_type(exc),
            code,
            sent_code,
            cause,
            round(now - last_pong, 3) if last_pong is not None else None,
            round(now - last_app_pong, 3) if last_app_pong is not None else None,
        )

    async def _push_outputs(self, transport: TunnelTransport) -> None:
        async with self._output_send_lock:
            await self._drain_output_batch(transport)

    async def _drain_output_batch(self, transport: TunnelTransport) -> None:
        terminal = ""
        current_epoch = ""
        pending = bytearray()

        async def flush() -> None:
            if not pending:
                return
            # Queued/in-flight work must not cross a newly removed/replaced
            # route. The gateway also fences the frame's explicit epoch.
            if not self._output_epoch_current(terminal, current_epoch):
                pending.clear()
                return
            await self._send(
                transport,
                {
                    "kind": "terminal.output",
                    "host_id": self._config.host_id,
                    "terminal_id": terminal,
                    "epoch": current_epoch,
                    "data_b64": base64.b64encode(pending).decode(),
                },
            )
            self._pushed_outputs += 1
            pending.clear()

        take = getattr(self._source, "take_output_batch", None)
        release = getattr(self._source, "release_output_batch", None)
        reserved = callable(take) and callable(release)
        if reserved:
            assert callable(take)
            frames = take()
        else:
            frames = self._source.drain_outputs()
        size = sum(len(data) for _, _, data in frames)
        try:
            for terminal_id, epoch, data in frames:
                if (terminal_id, epoch) != (terminal, current_epoch):
                    await flush()
                    terminal, current_epoch = terminal_id, epoch
                at = 0
                while at < len(data):
                    count = min(CHUNK_BYTES - len(pending), len(data) - at)
                    pending.extend(data[at : at + count])
                    at += count
                    if len(pending) == CHUNK_BYTES:
                        await flush()
            await flush()
        finally:
            if reserved:
                assert callable(release)
                release(size)

    def _output_epoch_current(self, terminal_id: str, epoch: str) -> bool:
        return (
            not callable(getattr(self._source, "wait_output_ready", None))
            or self._last_epochs.get(terminal_id) == epoch
        )

    def _note_routes(self, routes: dict[str, dict[str, str]]) -> None:
        self._last_epochs = {terminal: route["epoch"] for terminal, route in routes.items()}
        self._input_highwater = {
            key: seq
            for key, seq in self._input_highwater.items()
            if self._last_epochs.get(key[0]) == key[1]
        }
        if isinstance(self._source, LiveSource):
            failures = self._source.note_epochs(routes)
            for terminal_id, reason in failures.items():
                LOG.error("epoch attach failed for %s: %s", terminal_id, reason)

    def _apply_hello_extras(self, hello: dict[str, Any]) -> None:
        """Apply additive hello fields (forward-compatible by rule:
        unknown extras are ignored, malformed extras are skipped loudly
        — a hello must never wedge old links against newer gateways)."""
        self._chunked_ok = hello.get("snapshot_chunks") is True
        extras = hello.get("epochs")
        if extras is None:
            return
        if not isinstance(extras, list) or len(extras) > MAX_INPUT_TRACKERS:
            LOG.error("hello epochs malformed (not a list), ignoring")
            return
        routes: dict[str, dict[str, str]] = {}
        for entry in extras:
            if (
                not isinstance(entry, dict)
                or not isinstance(entry.get("terminal_id"), str)
                or not 1 <= len(entry["terminal_id"]) <= 128
                or not isinstance(entry.get("epoch"), str)
                or not 1 <= len(entry["epoch"]) <= 128
                or entry.get("kind") not in ("shell", "agent")
                or entry.get("mode", "control") not in ("control", "observe")
            ):
                LOG.error("hello epoch entry malformed, skipping: %r", entry)
                continue
            routes[str(entry["terminal_id"])] = {
                "epoch": str(entry["epoch"]),
                "kind": str(entry["kind"]),
                "mode": str(entry.get("mode", "control")),
            }
        self._note_routes(routes)

    async def _prepare_source(self) -> str | None:
        """Ready a live source for this session. Returns "drop" when the
        upstream needs a retry (rebootstrap after death, resync on
        reconnect); None when ready. Upstream outages retry with
        backoff and never trip the push breaker (a different failure
        domain than the gateway transport)."""
        if not isinstance(self._source, LiveSource):
            return None
        if self._source_stale:
            try:
                epoch = await self._source.rebootstrap()
            except (HerdrError, OSError) as exc:
                LOG.warning("upstream rebootstrap failed, retrying: %s", exc)
                return "drop"
            LOG.info("upstream rebootstrap complete (epoch=%s)", epoch)
            self._source_stale = False
            return None
        if self._sessions > 1:
            try:
                await self._source.resync()
            except (HerdrError, OSError) as exc:
                LOG.warning("upstream resync failed, retrying: %s", exc)
                return "drop"
        return None

    def _note_push_failure(self, exc: BaseException) -> str:
        """Count a transport failure inside the initial push; trip the
        breaker (loud exit) instead of churning forever."""
        self._push_failures += 1
        LOG.warning(
            "initial push failed (%d/%d): %s",
            self._push_failures,
            self._config.push_fail_limit,
            _safe_exception_type(exc),
        )
        if self._push_failures >= self._config.push_fail_limit:
            LOG.error(
                "initial push failed %d times in a row, exiting (push-failed)",
                self._push_failures,
            )
            return "push-failed"
        return "drop"

    async def _initial_push(self, transport: TunnelTransport) -> None:
        """Snapshot + seed events on every connect (replace + event_id
        dedupe make re-push safe); drained terminal bytes per connect
        (sources drain exactly-once: the ring is append-only, so
        re-push would duplicate bytes).

        The snapshot frame is size-guarded BEFORE sending: over-bound
        snapshots go chunked when the gateway advertised intake, else
        the run exits `snapshot-too-large` (fail loudly, never churn).
        """
        await self._push_snapshot(transport)
        if isinstance(self._source, LiveSource):
            seeds = self._source.drain_seeds()
        else:
            seeds = list(self._config.events)
        for event in seeds:
            await self._send(
                transport, {"kind": "event", "host_id": self._config.host_id, "event": event}
            )
            self._pushed_events += 1
        await self._push_outputs(transport)

    async def _push_snapshot(self, transport: TunnelTransport) -> None:
        """Publish current typed inventory without replaying seed metadata or bytes."""
        snapshot = self._source.snapshot_document(self._config.host_id)
        text = json.dumps(
            {"kind": "snapshot", "host_id": self._config.host_id, "snapshot": snapshot},
            separators=(",", ":"),
        )
        if len(text.encode()) > TUNNEL_MAX_TEXT:
            raw = json.dumps(snapshot, separators=(",", ":")).encode()
            if not self._chunked_ok or len(raw) > MAX_SNAPSHOT_BYTES:
                raise _SnapshotTooLarge(len(raw), len(text))
            await self._push_chunked(transport, raw)
        else:
            await self._send_text(transport, text)
        self._pushed_snapshot = True

    async def _refresh_after_creation(
        self, transport: TunnelTransport, message: dict[str, Any], result: dict[str, Any]
    ) -> None:
        if message.get("gateway_kind") not in ("agent.start", "shell.create", "session.close"):
            return
        if result.get("status") == "rejected":
            return
        # The result is sent first: read failure cannot erase a known outcome.
        refresh = getattr(self._source, "refresh", None)
        async with asyncio.timeout(10):
            if callable(refresh):
                await refresh()
            # Snapshot and outputs only: seed events belong to the session's first push and
            # re-sending them here would deliver each one twice.
            await self._push_snapshot(transport)
            await self._push_outputs(transport)

    async def _push_chunked(self, transport: TunnelTransport, raw: bytes) -> None:
        """Chunked snapshot transfer: begin/chunk/end frames under TUNNEL_MAX_TEXT,
        sha256-verified by the receiver. Transfer ids are unique per
        push so a stale retry never splices into a live transfer."""
        transfer_id = uuid.uuid4().hex
        digest = hashlib.sha256(raw).hexdigest()
        chunks = [raw[at : at + CHUNK_BYTES] for at in range(0, len(raw), CHUNK_BYTES)]
        await self._send(
            transport,
            {
                "kind": "snapshot.begin",
                "host_id": self._config.host_id,
                "transfer_id": transfer_id,
                "total_bytes": len(raw),
                "total_chunks": len(chunks),
                "sha256": digest,
            },
        )
        for seq, piece in enumerate(chunks):
            await self._send(
                transport,
                {
                    "kind": "snapshot.chunk",
                    "host_id": self._config.host_id,
                    "transfer_id": transfer_id,
                    "seq": seq,
                    "data_b64": base64.b64encode(piece).decode(),
                },
            )
            self._pushed_chunks += 1
        await self._send(
            transport,
            {"kind": "snapshot.end", "host_id": self._config.host_id, "transfer_id": transfer_id},
        )

    def _precheck(self, message: dict[str, Any]) -> str:
        """Fail-closed gate before ack: host binding + digest. Returns the
        command id; raises _ProtocolDrop (never ack, never execute)."""
        try:
            validate("relaydeck/gateway/v1/command-dispatch", message)
        except SchemaError as exc:
            raise _ProtocolDrop("invalid typed command dispatch") from exc
        command_id = message.get("command_id")
        kind = message.get("gateway_kind")
        target = message.get("target")
        payload = message.get("payload")
        if (
            not isinstance(command_id, str)
            or not command_id
            or not isinstance(kind, str)
            or not isinstance(target, dict)
            or not isinstance(payload, dict)
            or message.get("host_id") != self._config.host_id
        ):
            raise _ProtocolDrop("malformed dispatch")
        if message.get("digest") != digest_for(self._config.host_id, kind, target, payload):
            raise _ProtocolDrop("dispatch digest mismatch")
        return command_id

    async def _push_live(self, transport: TunnelTransport, *, outputs: bool = True) -> bool:
        """Push one live batch (upstream events + PTY bytes). Returns
        False when the upstream died: the caller drops the session so
        the next session rebootstraps (new epoch) and re-pushes."""
        assert isinstance(self._source, LiveSource)
        try:
            events = await self._source.collect_live(LIVE_DRAIN_S)
        except HerdrError as exc:
            if exc.code != "upstream_closed":
                raise
            LOG.warning("upstream closed, dropping session for rebootstrap")
            self._source_stale = True
            return False
        take_dirty = getattr(self._source, "take_snapshot_dirty", None)
        removed = callable(take_dirty) and bool(take_dirty())
        if events or removed:
            # Upstream metadata partials omit names/project/resource structure. A removal
            # re-reads the snapshot too: a closed tab must not survive in the gateway copy.
            # Reuse session.snapshot + agent.list once per metadata batch, never
            # per PTY byte/heartbeat. Publish before the event notification so
            # a client refresh observes the corresponding complete inventory.
            refresh = getattr(self._source, "refresh", None)
            if callable(refresh):
                try:
                    async with asyncio.timeout(10):
                        await refresh()
                        await self._push_snapshot(transport)
                except Exception:
                    self._source_stale = True
                    raise
        for event in events:
            await self._send(
                transport, {"kind": "event", "host_id": self._config.host_id, "event": event}
            )
            self._pushed_events += 1
        if outputs:
            self._source.pump_live()
            await self._push_terminal_updates(transport)
        return True

    async def _push_terminal_updates(self, transport: TunnelTransport) -> None:
        """One ordered output consumer; metadata refresh never blocks its producer."""
        drain_resets = getattr(self._source, "drain_terminal_resets", None)
        if drain_resets is not None:
            for terminal_id, epoch in drain_resets().items():
                if not self._output_epoch_current(terminal_id, epoch):
                    continue
                await self._send(
                    transport,
                    {
                        "kind": "terminal.reset_required",
                        "host_id": self._config.host_id,
                        "terminal_id": terminal_id,
                        "epoch": epoch,
                    },
                )
        await self._push_outputs(transport)

    def _handle_epochs(self, message: dict[str, Any]) -> None:
        """Consume a gateway epoch-distribution message: validate
        strictly (fail closed like dispatches), then route PTY bytes
        through the new table. Raises _ProtocolDrop."""
        if message.get("host_id") != self._config.host_id:
            raise _ProtocolDrop("epochs host mismatch")
        entries = message.get("epochs")
        if not isinstance(entries, list) or len(entries) > MAX_INPUT_TRACKERS:
            raise _ProtocolDrop("epochs is not a list")
        routes: dict[str, dict[str, str]] = {}
        for entry in entries:
            if (
                not isinstance(entry, dict)
                or not isinstance(entry.get("terminal_id"), str)
                or not 1 <= len(entry["terminal_id"]) <= 128
                or not isinstance(entry.get("epoch"), str)
                or not 1 <= len(entry["epoch"]) <= 128
                or entry.get("kind") not in ("shell", "agent")
                or entry.get("mode", "control") not in ("control", "observe")
            ):
                raise _ProtocolDrop("epochs entry malformed")
            routes[entry["terminal_id"]] = {
                "epoch": entry["epoch"],
                "kind": entry["kind"],
                "mode": entry.get("mode", "control"),
            }
        self._note_routes(routes)

    async def _handle_terminal(self, transport: TunnelTransport, doc: dict[str, Any]) -> None:
        try:
            frame = parse_terminal_input(doc, self._config.host_id)
        except (ValueError, UnicodeError) as exc:
            raise _ProtocolDrop("malformed terminal input") from exc
        if self._last_epochs.get(frame.terminal_id) != frame.epoch:
            raise _ProtocolDrop("stale input epoch")
        key = (frame.terminal_id, frame.epoch, frame.lease_id, frame.generation)
        if frame.input_seq <= self._input_highwater.get(key, 0):
            return
        if key not in self._input_highwater and len(self._input_highwater) >= MAX_INPUT_TRACKERS:
            raise _ProtocolDrop("input tracker capacity exceeded")
        # Mark BEFORE the attempt: a timeout/partial write is uncertain and
        # must never become an automatic retry across reconnect.
        self._input_highwater[key] = frame.input_seq
        if frame.kind == "resize":
            resize = getattr(self._source, "resize_terminal", None)
            reason = (
                resize(frame.terminal_id, frame.epoch, frame.cols, frame.rows)
                if resize
                else ("terminal source unavailable")
            )
        else:
            submit = getattr(self._source, "submit_terminal_input", None)
            try:
                reason = (
                    await asyncio.wait_for(
                        submit(frame.terminal_id, frame.epoch, frame.kind, frame.data),
                        SEND_TIMEOUT_S,
                    )
                    if submit
                    else "terminal source unavailable"
                )
            except (OSError, TimeoutError):
                reason = "input outcome uncertain"
        if reason is not None:
            LOG.warning("terminal input failed for %s: %s", frame.terminal_id, reason)
            await self._send(
                transport,
                {
                    "kind": "terminal.input_failed",
                    "host_id": self._config.host_id,
                    "lease_id": frame.lease_id,
                    "generation": frame.generation,
                    "input_seq": frame.input_seq,
                },
            )

    async def _serve(self, transport: TunnelTransport, stop: asyncio.Event) -> str:
        wait_ready = getattr(self._source, "wait_output_ready", None)
        if not isinstance(self._source, LiveSource) or not callable(wait_ready):
            return await self._serve_messages(transport, stop)
        available = asyncio.Event()
        tasks = [
            asyncio.create_task(self._serve_messages(transport, stop, outputs=False)),
            asyncio.create_task(self._collect_pty(wait_ready, available)),
            asyncio.create_task(self._forward_pty(transport, available)),
        ]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            # Surface producer overflow/send failure even when shutdown races
            # it; no background failure may leave a seemingly live link.
            for task in tasks[1:]:
                if task in done:
                    task.result()
                    return "drop"
            return str(tasks[0].result())
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _collect_pty(
        self, wait_ready: Callable[[], Awaitable[None]], available: asyncio.Event
    ) -> None:
        assert isinstance(self._source, LiveSource)
        while True:
            self._source.pump_live()
            available.set()
            # Scheduling fairness, no timed idle sleep. Network sends run in
            # the consumer; a blocked tunnel cannot suspend this PTY pump.
            await asyncio.sleep(0)
            await wait_ready()

    async def _forward_pty(self, transport: TunnelTransport, available: asyncio.Event) -> None:
        while True:
            await available.wait()
            # A PTY can refill in multiple readable wakeups as a blocked
            # producer is released. Keep those ordered pieces in one batch;
            # the independent reader remains immediate and never awaits sends.
            await asyncio.sleep(OUTPUT_COALESCE_S)
            available.clear()
            await self._push_terminal_updates(transport)

    async def _serve_messages(
        self, transport: TunnelTransport, stop: asyncio.Event, *, outputs: bool = True
    ) -> str:
        live = isinstance(self._source, LiveSource)
        tick = LIVE_TICK_S if live else RECV_POLL_S
        last_ping = asyncio.get_running_loop().time()
        while True:
            if stop.is_set():
                try:
                    await self._send(transport, {"kind": "bye"})
                except Exception:  # noqa: BLE001 - shutdown best-effort
                    pass
                return "stop"
            interval = self._config.ping_interval_s
            if interval > 0 and asyncio.get_running_loop().time() - last_ping >= interval:
                await self._send(transport, {"kind": "ping"})
                last_ping = asyncio.get_running_loop().time()
            try:
                raw = await asyncio.wait_for(transport.recv_text(), tick)
            except TimeoutError:
                raw = ""
            if live and not await self._push_live(transport, outputs=outputs):
                return "drop"
            if not raw:
                continue
            if len(raw.encode()) > TUNNEL_MAX_TEXT:
                raise _ProtocolDrop("inbound tunnel frame too large")
            try:
                message = json.loads(raw)
            except ValueError as exc:
                raise _ProtocolDrop("tunnel message is not JSON") from exc
            if not isinstance(message, dict):
                raise _ProtocolDrop("tunnel message is not an object")
            kind = message.get("kind")
            if kind == "pong":
                self._last_application_pong_at = asyncio.get_running_loop().time()
                continue
            if kind == "terminal.epochs":
                self._handle_epochs(message)
                continue
            if kind in ("terminal.input", "terminal.resize"):
                await self._handle_terminal(transport, message)
                continue
            if kind != "command.dispatch":
                raise _ProtocolDrop(f"unexpected tunnel kind {kind!r}")
            command_id = self._precheck(message)
            await self._send(
                transport,
                {"kind": ACK_KIND, "command_id": command_id, "host_id": self._config.host_id},
            )
            self._dispatches += 1
            result = await self._dispatch.dispatch(message)
            await self._send(transport, result)
            self._results += 1
            await self._refresh_after_creation(transport, message, result)

    async def _drain_once(self, transport: TunnelTransport, stop: asyncio.Event) -> str:
        """Single session for `--once`: process until the first result (or
        the timeout), then say bye. Used by smoke runs and loopback tests."""
        try:
            async with asyncio.timeout(self._config.once_timeout_s):
                while not stop.is_set():
                    try:
                        raw = await asyncio.wait_for(transport.recv_text(), RECV_POLL_S)
                    except TimeoutError:
                        continue
                    if len(raw.encode()) > TUNNEL_MAX_TEXT:
                        raise _ProtocolDrop("inbound tunnel frame too large")
                    try:
                        message = json.loads(raw)
                    except ValueError as exc:
                        raise _ProtocolDrop("tunnel message is not JSON") from exc
                    if not isinstance(message, dict):
                        raise _ProtocolDrop("tunnel message is not an object")
                    if message.get("kind") == "pong":
                        self._last_application_pong_at = asyncio.get_running_loop().time()
                        continue
                    if message.get("kind") == "terminal.epochs":
                        self._handle_epochs(message)
                        continue
                    if message.get("kind") in ("terminal.input", "terminal.resize"):
                        await self._handle_terminal(transport, message)
                        continue
                    if message.get("kind") != "command.dispatch":
                        raise _ProtocolDrop(f"unexpected tunnel kind {message.get('kind')!r}")
                    command_id = self._precheck(message)
                    await self._send(
                        transport,
                        {
                            "kind": ACK_KIND,
                            "command_id": command_id,
                            "host_id": self._config.host_id,
                        },
                    )
                    self._dispatches += 1
                    result = await self._dispatch.dispatch(message)
                    await self._send(transport, result)
                    self._results += 1
                    await self._refresh_after_creation(transport, message, result)
                    break
        except TimeoutError:
            pass
        try:
            await self._send(transport, {"kind": "bye"})
        except Exception:  # noqa: BLE001 - shutdown best-effort
            pass
        return "once-done" if not stop.is_set() else "stop"

    async def close(self) -> None:
        if self._own_journal:
            self._journal.close()
