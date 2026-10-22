"""The connector tunnel and terminal viewer WebSocket routes."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from collections.abc import Awaitable
from contextlib import suppress
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

from relaydeck.contracts import paths
from relaydeck.contracts.schemas import SchemaError, validate
from relaydeck.contracts.tunnel import SnapshotIntake, parse_terminal_input
from relaydeck.contracts.wire import terminal as terminal_wire
from relaydeck.gateway.backend import BackendError
from relaydeck.gateway.bounds import (
    MAX_LEASE_ID_CHARS,
    MAX_SNAPSHOT_BYTES,
    TERMINAL_PUSH_POLL_S,
    TUNNEL_MAX_OUTPUT,
    TUNNEL_MAX_TEXT,
    TUNNEL_POLL_S,
)
from relaydeck.gateway.command_io import command_io
from relaydeck.gateway.context import RouteContext
from relaydeck.gateway.ring import FIRST_REPLAY_MAX_BYTES, VIEWER_QUEUE_MAX_BYTES
from relaydeck.gateway.terminal_epochs import release_lease_and_close
from relaydeck.gateway.tunnel_cleanup import TunnelCleanup
from relaydeck.lease_diagnostics import LeaseTrace, opaque
from relaydeck.offload import settled
from relaydeck.tunnel_queue import (
    BoundedInbox,
    TunnelCapacityError,
    TunnelIntake,
    TunnelWriter,
    cancel_tasks,
)

LOG = logging.getLogger("relaydeck.gateway.app")


def build_connector_router(ctx: RouteContext) -> APIRouter:
    router = APIRouter()
    state = ctx.state
    emit_command_update = ctx.emit_command_update

    @router.websocket(paths.CONNECTORS)
    async def connector_socket(websocket: WebSocket) -> None:
        # Bearer header preferred (query strings land in access logs);
        # the query form stays for compatibility.
        token = websocket.query_params.get("token", "")
        if not token:
            scheme, _, value = websocket.headers.get("authorization", "").partition(" ")
            if scheme.lower() == "bearer" and value:
                token = value
        record = state.hosts.lookup(token)
        if record is None:
            await websocket.close(code=1008, reason="unauthenticated")
            return
        if state.sockets.host_count(record.host_id) >= 2:
            # One current tunnel plus its retiring predecessor; replacement storms
            # cannot accumulate connection-local queues or blocked cleanup lanes.
            await websocket.close(code=1013, reason="tunnel_busy")
            return
        # Single live tunnel per host (OWASP flood/DoS bound): a new
        # tunnel replaces a stale one, and arrival order on the survivor
        # is the terminal-output order.
        displaced = state.sockets.host_sockets(record.host_id)
        await websocket.accept()
        state.sockets.register_host(record.host_id, websocket, record)
        intake = SnapshotIntake()
        last_epochs = state.backend.epochs_for_host(record.host_id, record.account)
        last_receive = time.monotonic()

        def current() -> bool:
            return state.hosts.lookup(token) == record and state.sockets.host_current(
                record.host_id, websocket
            )

        writer = TunnelWriter(websocket.send_text, current)
        state.sockets.bind_writer(websocket, writer)
        incoming: BoundedInbox[dict[str, Any] | None] = BoundedInbox()
        commands: BoundedInbox[dict[str, Any]] = BoundedInbox()
        admission = TunnelIntake(incoming, intake)
        cleanup = TunnelCleanup(state.sockets)
        cleanup.add(displaced, "", "replaced")

        async def timed[T](branch: str, work: Awaitable[T]) -> T:
            started = time.monotonic()
            try:
                return await work
            finally:
                elapsed = time.monotonic() - started
                if elapsed > 0.5:
                    LOG.warning("tunnel_stall branch=%s ms=%.0f", branch, elapsed * 1000)

        async def send_hello() -> None:
            await state.sockets.send_text(
                websocket,
                json.dumps(
                    {
                        "kind": "hello",
                        "host_id": record.host_id,
                        "snapshot_chunks": True,
                        "epochs": last_epochs,
                    }
                ),
            )

        async def pump_outbox() -> bool:
            """Drain journaled dispatches down this tunnel. The
            write-ahead marker is journaled BEFORE each send; a marker
            miss skips the send (never re-dispatch, including
            TTL-refused markers, which settle expired), and a send
            failure ends the tunnel (bytes are never silently lost)."""
            nonlocal last_epochs
            if state.hosts.lookup(token) != record:
                await state.sockets.close_socket(websocket, "unauthenticated")
                return False
            epochs = state.backend.epochs_for_host(record.host_id, record.account)
            if epochs != last_epochs:
                try:
                    await state.sockets.send_text(
                        websocket,
                        json.dumps(
                            {
                                "kind": "terminal.epochs",
                                "host_id": record.host_id,
                                "epochs": epochs,
                            }
                        ),
                    )
                except Exception:
                    return False
                last_epochs = epochs
            pending = await command_io(state.backend, "drain_outbox", record.host_id)
            consumed = 0
            try:
                for index, message in enumerate(pending):
                    consumed = index
                    if message.get("principal") != record.account or not record.account:
                        LOG.error(
                            "refused cross-account connector dispatch for host %s", record.host_id
                        )
                        consumed = index + 1
                        continue
                    command_id = str(message.get("command_id", ""))

                    async def before(command_id: str = command_id) -> bool:
                        if not command_id or not current():
                            return False
                        ready = await command_io(
                            state.backend, "mark_command_dispatched", command_id, state.now_ms()
                        )
                        emit_command_update(
                            await command_io(state.backend, "command_status", command_id)
                        )
                        return bool(ready)

                    try:
                        await writer.send(json.dumps(message), before=before)
                    except Exception:
                        return False
                    consumed = index + 1
                consumed = len(pending)
            finally:
                binding = state.hosts.record_for_host(record.host_id)
                restore = (
                    pending[consumed:]
                    if binding is not None and binding.account == record.account
                    else []
                )
                state.backend.restore_outbox(record.host_id, restore, pending)
            return True

        async def ingest_snapshot(snapshot: dict[str, Any], now_ms: int) -> bool:
            try:
                if len(json.dumps(snapshot).encode()) > MAX_SNAPSHOT_BYTES:
                    return False
                validate("relaydeck/gateway/v1/snapshot", snapshot)
            except (ValueError, TypeError, SchemaError):
                return False
            host = snapshot.get("host", {})
            if host.get("id") != record.host_id:
                return False
            try:
                previous_epoch = state.backend.snapshot(record.host_id).get("host", {}).get("epoch")
            except BackendError:
                previous_epoch = None
            if "ok" not in state.backend.ingest_snapshot(
                record.host_id, snapshot, now_ms, state.cursor_secret
            ):
                return False
            state.notifications.snapshot(record.account, record.host_id, snapshot, now_ms)
            if previous_epoch is not None and previous_epoch != host.get("epoch"):
                reset_doc = json.dumps({"kind": "reset", "detail": "stale_epoch"})
                for lease_id in state.backend.rotate_streams(record.host_id):
                    cleanup.add(state.sockets.lease_sockets(lease_id), reset_doc, "stale_revision")
            return True

        async def handle_tunnel_doc(doc: dict[str, Any]) -> bool:
            """One connector->gateway message. True keeps the tunnel;
            False fails it closed (OWASP: per-message authorization, not
            just handshake auth). Only the command lane calls the awaited
            acknowledgement/result branches; output never awaits journal work."""
            kind = doc.get("kind")
            now_ms = state.now_ms()
            if kind == "terminal.reset_required":
                if (
                    doc.get("host_id") != record.host_id
                    or not isinstance(doc.get("terminal_id"), str)
                    or not 1 <= len(doc["terminal_id"]) <= 128
                    or not isinstance(doc.get("epoch"), str)
                    or not 1 <= len(doc["epoch"]) <= 128
                ):
                    return False
                dead = state.backend.reset_terminal(
                    record.host_id, doc["terminal_id"], doc["epoch"], record.account
                )
                for lease_id in dead:
                    cleanup.add(
                        state.sockets.lease_sockets(lease_id),
                        json.dumps({"kind": "reset", "detail": "stale_epoch"}),
                        "stale_revision",
                    )
                return True
            if kind == "terminal.input_failed":
                if (
                    doc.get("host_id") != record.host_id
                    or not isinstance(doc.get("lease_id"), str)
                    or type(doc.get("generation")) is not int
                    or type(doc.get("input_seq")) is not int
                    or not 1 <= doc["input_seq"] <= 0xFFFFFFFF
                ):
                    return False
                failed_lease = state.backend.open_lease(doc["lease_id"])
                if failed_lease is None:
                    return True
                if (
                    failed_lease.host_id != record.host_id
                    or failed_lease.owner != record.account
                    or failed_lease.generation != doc["generation"]
                ):
                    return False
                cleanup.add(
                    state.sockets.lease_sockets(failed_lease.lease_id),
                    json.dumps(
                        {
                            "kind": "gap",
                            "detail": "input_uncertain",
                            "generation": doc["generation"],
                            "input_seq": doc["input_seq"],
                        }
                    ),
                )
                return True
            if kind == "command.ack":
                if doc.get("host_id") != record.host_id or not isinstance(
                    doc.get("command_id"), str
                ):
                    return False
                command_id = str(doc["command_id"])
                if await command_io(state.backend, "command_owner", command_id) != (
                    record.account,
                    record.host_id,
                ):
                    return False
                if not current():
                    raise ConnectionError("tunnel_superseded")
                updated = await command_io(
                    state.backend, "note_command_ack", command_id, record.host_id, now_ms
                )
                emit_command_update(updated)
                return updated is not None
            if kind == "command.result":
                if (
                    doc.get("host_id") != record.host_id
                    or not isinstance(doc.get("command_id"), str)
                    or doc.get("status") not in ("succeeded", "failed", "rejected", "unknown")
                    or not isinstance(doc.get("result_digest", ""), str)
                    or not isinstance(doc.get("error_code", ""), str)
                    or ("result" in doc and not isinstance(doc["result"], dict))
                ):
                    return False
                if await command_io(state.backend, "command_owner", str(doc["command_id"])) != (
                    record.account,
                    record.host_id,
                ):
                    return False
                if not current():
                    raise ConnectionError("tunnel_superseded")
                updated = await command_io(
                    state.backend,
                    "note_command_result",
                    str(doc["command_id"]),
                    record.host_id,
                    str(doc["status"]),
                    str(doc.get("result_digest", "")),
                    str(doc.get("error_code", "")),
                    doc.get("result"),
                    now_ms,
                )
                emit_command_update(updated)
                return updated is not None
            if kind == "terminal.output":
                data_b64 = doc.get("data_b64", "")
                if (
                    doc.get("host_id") != record.host_id
                    or not isinstance(doc.get("terminal_id"), str)
                    or not doc.get("terminal_id")
                    or not isinstance(doc.get("epoch"), str)
                    or not isinstance(data_b64, str)
                    or not data_b64
                ):
                    return False
                try:
                    data = base64.b64decode(data_b64, validate=True)
                except ValueError:
                    return False
                if len(data) > TUNNEL_MAX_OUTPUT:
                    return False
                # Semantic misses (unknown terminal, rotated epoch) drop
                # the frame but keep the host tunnel: one stale stream
                # must not kill the whole host link.
                state.backend.ingest_terminal_output(
                    record.host_id, str(doc["terminal_id"]), str(doc["epoch"]), data, now_ms
                )
                return True
            if kind == "snapshot":
                # Connector metadata sync: schema-validated,
                # host-bound, replaces. A bad snapshot fails the tunnel
                # closed (never stored, never served).
                snapshot = doc.get("snapshot")
                if doc.get("host_id") != record.host_id or not isinstance(snapshot, dict):
                    return False
                return await ingest_snapshot(snapshot, now_ms)
            if kind == "event":
                # Connector metadata sync: partial event, the
                # server stamps sequence/cursor and dedupes by event_id.
                partial = doc.get("event")
                if doc.get("host_id") != record.host_id or not isinstance(partial, dict):
                    return False
                try:
                    current_epoch = state.backend.snapshot(record.host_id)["host"]["epoch"]
                except (BackendError, KeyError):
                    return False
                if partial.get("host_epoch") != current_epoch:
                    return False
                event_type = partial.get("type")
                if not isinstance(event_type, str):
                    return False
                status_key = {"pane.upserted": "agent_status", "agent.upserted": "status"}.get(
                    event_type
                )
                payload = partial.get("payload")
                if status_key and isinstance(payload, dict) and status_key in payload:
                    value = payload[status_key]
                    if not isinstance(value, str) or value not in {
                        "idle",
                        "working",
                        "blocked",
                        "done",
                        "unknown",
                    }:
                        return False
                result = state.backend.ingest_event(
                    record.host_id, partial, now_ms, state.cursor_secret
                )
                if "ok" not in result:
                    return False
                if not result.get("duplicate"):
                    state.notifications.event(record.account, record.host_id, result["ok"], now_ms)
                return True
            return False

        async def receive_documents() -> None:
            nonlocal last_receive
            while True:
                try:
                    message = await timed(
                        "receive", asyncio.wait_for(websocket.receive(), timeout=TUNNEL_POLL_S)
                    )
                except TimeoutError:
                    intake.check_deadline(time.monotonic())
                    continue
                last_receive = time.monotonic()
                if not current():
                    raise ConnectionError("tunnel_superseded")
                if message["type"] == "websocket.disconnect":
                    incoming.put(None, 0, control=True)
                    await asyncio.Future()
                text = message.get("text")
                if not isinstance(text, str) or len(text.encode()) > TUNNEL_MAX_TEXT:
                    raise ValueError("bad_tunnel_frame")
                doc = json.loads(text)
                if not isinstance(doc, dict):
                    raise ValueError("bad_tunnel_frame")
                if doc.get("kind") == "ping":
                    writer.post((json.dumps({"kind": "pong"}),), control=True)
                else:
                    size = len(text.encode())
                    # Both consumers (including active work and partial snapshots)
                    # share the same byte budget; journal waits cannot stall output.
                    if (
                        incoming.bytes + commands.bytes + admission.batch_bytes + size
                        > incoming.byte_limit
                    ):
                        raise TunnelCapacityError("tunnel_queue_full")
                    if doc.get("kind") in ("command.ack", "command.result"):
                        intake.check_deadline(time.monotonic())
                        if intake.active:
                            raise ValueError("snapshot_interleaved")
                        commands.put(doc, size)
                    else:
                        admission.put(doc, size, record.host_id, time.monotonic())
                    if doc.get("kind") == "bye":
                        await asyncio.Future()

        async def process_documents() -> None:
            while True:
                try:
                    async with asyncio.timeout(TUNNEL_POLL_S):
                        entry = await incoming.get()
                except TimeoutError:
                    continue
                try:
                    doc = entry[0]
                    if doc is None or doc.get("kind") == "bye":
                        # A graceful close still settles all preceding replies.
                        await commands.queue.join()
                        return
                    if not current():
                        return
                    kind = doc.get("kind")
                    branch = (
                        kind
                        if kind
                        in (
                            "snapshot",
                            "snapshot.begin",
                            "snapshot.chunk",
                            "snapshot.end",
                            "event",
                            "terminal.output",
                            "terminal.reset_required",
                            "terminal.input_failed",
                        )
                        else "unknown_doc"
                    )
                    if not await timed(str(branch), handle_tunnel_doc(doc)):
                        raise ValueError("bad_tunnel_document")
                finally:
                    incoming.finish(entry)

        async def process_commands() -> None:
            """FIFO replies; started persistence settles before this tunnel retires."""
            while True:
                entry = await commands.get()
                try:
                    if not current():
                        return
                    # Own the whole reply, not only its individual journal awaits.
                    # Supersession during owner lookup is checked before mutation.
                    accepted = await settled(
                        asyncio.create_task(
                            timed(str(entry[0]["kind"]), handle_tunnel_doc(entry[0]))
                        )
                    )
                    if not accepted:
                        raise ValueError("bad_tunnel_document")
                finally:
                    commands.finish(entry)

        async def outbox() -> None:
            while current():
                # Retain the drained list through cancellation, including the journal commit.
                if not await timed("pump_outbox", settled(asyncio.create_task(pump_outbox()))):
                    raise ConnectionError("tunnel_write_failed")
                await asyncio.sleep(TUNNEL_POLL_S)

        cleanup_task = asyncio.create_task(cleanup.run())
        tasks = [asyncio.create_task(writer.run())]
        try:
            await timed("hello", send_hello())
            tasks.extend(
                [
                    asyncio.create_task(outbox()),
                    asyncio.create_task(receive_documents()),
                    asyncio.create_task(process_documents()),
                    asyncio.create_task(process_commands()),
                ]
            )
            done, _ = await asyncio.wait(
                [*tasks, cleanup_task], return_when=asyncio.FIRST_COMPLETED
            )
            for task in done:
                task.result()
        except TunnelCapacityError:
            with suppress(Exception):
                await state.sockets.close_socket(websocket, "tunnel_queue_full")
        except (WebSocketDisconnect, TimeoutError, ConnectionError, ValueError):
            with suppress(Exception):
                await state.sockets.close_socket(websocket, "forbidden")
        except asyncio.CancelledError:
            return  # An ASGI disconnect still joins all owned work below.
        finally:
            LOG.warning(
                "tunnel_loop idle_recv_s=%.3f queued=%d bytes=%d oldest_s=%.3f writer=%s "
                "write_queued=%d write_bytes=%d write_oldest_s=%.3f "
                "command_queued=%d command_bytes=%d command_oldest_s=%.3f",
                time.monotonic() - last_receive,
                incoming.count,
                incoming.bytes,
                incoming.oldest_age,
                writer.phase,
                writer.inbox.count,
                writer.inbox.bytes,
                writer.inbox.oldest_age,
                commands.count,
                commands.bytes,
                commands.oldest_age,
            )

            async def finish_tunnel() -> None:
                state.sockets.unregister(websocket)
                await cancel_tasks(tasks)
                await cleanup.finish(cleanup_task)
                if not state.sockets.host_count(record.host_id):
                    state.notifications.drop_host(record.host_id)

            with suppress(asyncio.CancelledError):
                await settled(asyncio.create_task(finish_tunnel()))

    return router


def build_terminal_socket_router(ctx: RouteContext) -> APIRouter:
    router = APIRouter()
    state = ctx.state

    @router.websocket(paths.TERMINAL_SOCKET)
    async def terminal_socket(websocket: WebSocket, lease_id: str) -> None:
        ticket = websocket.query_params.get("ticket", "")
        grant = state.tickets.redeem(ticket, lease_id, state.now_ms()) if ticket else None
        if grant is None:
            await websocket.close(code=1008, reason="unauthenticated")
            return
        if "terminal.view" not in grant.capabilities:
            await websocket.close(code=1008, reason="unsupported_capability")
            return
        lease = state.backend.open_lease(lease_id)
        if lease is None or lease.host_id not in grant.hosts or grant.account != lease.owner:
            await websocket.close(code=1008, reason="lease_expired")
            return
        if grant.generation and grant.generation != lease.generation:
            await websocket.close(code=1008, reason="lease_expired")
            return
        reset_protocol = websocket.query_params.get("output_reset") == "v1"
        live_edge = websocket.query_params.get("live_edge") == "1"
        if live_edge and (not reset_protocol or lease.device_id != grant.device_id):
            await websocket.close(code=1008, reason="forbidden")
            return
        resume_params = "epoch" in websocket.query_params or "offset" in websocket.query_params
        resume_epoch = websocket.query_params.get("epoch", "")
        try:
            resume_offset = int(websocket.query_params.get("offset", "0"))
        except ValueError:
            await websocket.close(code=1008, reason="forbidden")
            return
        if resume_offset < 0 or len(resume_epoch) > MAX_LEASE_ID_CHARS:
            await websocket.close(code=1008, reason="forbidden")
            return
        if lease.resume_pending and not (
            resume_params
            and grant.resume
            and grant.generation == lease.generation
            and grant.device_id == lease.device_id
        ):
            await websocket.close(code=1008, reason="lease_expired")
            return
        await websocket.accept()
        close_logged = False

        class StreamTrace(LeaseTrace):
            def emit(self, event: str, **fields: Any) -> None:
                nonlocal close_logged
                if event == "socket_close":
                    if close_logged:
                        return
                    close_logged = True
                super().emit(event, **fields)

        trace = StreamTrace(
            opaque("principal", grant.account, grant.device_id),
            opaque("terminal", lease.host_id, lease.terminal_id),
            lease_id,
            lease.mode,
            lease.generation,
        )
        state.sockets.register(
            grant.account, lease_id, websocket, grant.device_id, diagnostics=trace
        )
        trace.emit("socket_open", reason="ok", source="server", socket=websocket)
        fanout = state.backend.fanout_for(lease.attachment_id)
        viewer_id = str(id(websocket))
        recovery_close = False

        def disconnected() -> bool:
            return (
                getattr(websocket, "application_state", None) == WebSocketState.DISCONNECTED
                or getattr(websocket, "client_state", None) == WebSocketState.DISCONNECTED
            )

        def preserve_reader() -> None:
            # Publish the handoff before sending reset: the client can mint its replacement
            # ticket immediately, before this handler's close/finally gets another turn.
            if (
                lease.mode == "control"
                and grant.device_id == lease.device_id
                and not state.sockets.has_device_lease(
                    grant.account, grant.device_id, lease_id, excluding=websocket
                )
            ):
                state.backend.begin_reader_handoff(
                    lease_id, grant.account, grant.device_id, lease.generation
                )

        async def close_stream(reason: str) -> None:
            # Reset/revoke can close this socket while receive is suspended.
            # Registry removal claims the close before its first await; do not
            # send a second close or overwrite the first reset/revoke reason.
            if disconnected():
                return
            if reason in ("stale_epoch", "stale_offset", "overflow", "reset"):
                preserve_reader()
            await state.sockets.close_socket(websocket, reason)

        async def report_input_limit() -> bool:
            """Drop this input only; a rate limit does not end the output stream."""
            if disconnected():
                return False
            try:
                await state.sockets.send_text(
                    websocket, json.dumps({"kind": "gap", "detail": "rate_limited"})
                )
            except Exception:
                # Revocation/close can win after the state check. Never send again.
                return False
            return True

        async def forward_input(chunk: terminal_wire.InputChunk) -> None:
            record = state.hosts.record_for_host(lease.host_id)
            # FakeBackend remains an explicit synthetic recorder. Production
            # never treats a local input log as delivery to the home host.
            if record is None and not state.backend.requires_connector:
                return
            if record is None:
                raise BackendError("host_offline", "connector unavailable", True)
            if record.account != lease.owner or not record.account:
                raise BackendError("not_found", "no such terminal")
            if not state.sockets.host_count(lease.host_id, record):
                raise BackendError("host_offline", "connector unavailable", True)
            doc: dict[str, Any] = {
                "kind": "terminal.resize" if chunk.kind == "resize" else "terminal.input",
                "host_id": lease.host_id,
                "terminal_id": lease.terminal_id,
                "epoch": lease.epoch,
                "lease_id": lease_id,
                "generation": chunk.generation,
                "input_seq": chunk.input_seq,
            }
            if chunk.kind == "resize":
                doc.update(cols=chunk.cols, rows=chunk.rows)
            else:
                doc.update(input_kind=chunk.kind, data_b64=base64.b64encode(chunk.data).decode())
            parse_terminal_input(doc, lease.host_id)

            refused = ""

            async def still_allowed() -> bool:
                nonlocal refused
                current_lease = state.backend.open_lease(lease_id)
                valid = (
                    current_lease is not None
                    and current_lease.mode == "control"
                    and current_lease.generation == chunk.generation
                    and current_lease.epoch == lease.epoch
                    and current_lease.owner == grant.account
                    and current_lease.device_id == grant.device_id
                    and state.hosts.record_for_host(lease.host_id) == record
                )
                refused = (
                    "stale_revision"
                    if not valid
                    else (
                        "input_failed"
                        if current_lease is not None and current_lease.resume_pending
                        else ""
                    )
                )
                return not refused

            if not await state.sockets.notify_host(
                lease.host_id, json.dumps(doc), binding=record, before=still_allowed
            ):
                raise BackendError(
                    refused or "host_offline", "input not delivered; not replayed", True
                )

        async def push_pending() -> str | None:
            """Send queued live bytes. Returns None while flowing, else
            the reset detail that must close the socket: an overflowed
            (slow) viewer or a rotated epoch gets an explicit reset doc
            plus close, never silent loss presented as valid bytes."""
            if state.backend.open_lease(lease_id) is None:
                return "lease_expired"
            queue = fanout.viewer(viewer_id) if fanout is not None else None
            if queue is None:
                return None
            if queue.reset_pending or queue.overflowed:
                detail = "stale_epoch" if queue.reset_pending else "overflow"
                preserve_reader()
                try:
                    await state.sockets.send_text(
                        websocket, json.dumps({"kind": "reset", "detail": detail})
                    )
                except Exception:
                    pass
                return detail
            try:
                for offset, data in queue.pop_all():
                    # A queued payload may exceed the 64 KiB wire chunk:
                    # re-chunk with continuous offsets (never fail the
                    # viewer on a large upstream write).
                    for at in range(0, len(data), terminal_wire.MAX_OUTPUT_CHUNK):
                        piece = data[at : at + terminal_wire.MAX_OUTPUT_CHUNK]
                        await state.sockets.send_bytes(
                            websocket, terminal_wire.encode_output(queue.epoch, offset + at, piece)
                        )
            except Exception:
                return "reset"
            return None

        try:
            if fanout is not None:
                # Least-privilege subscription (OWASP): this socket sees
                # only its own lease's attachment stream, and the id is
                # per-connection so co-viewers never share a queue.
                fanout.subscribe(viewer_id)
            baseline: dict[str, Any] | None = None
            if reset_protocol and (live_edge or (not resume_epoch and resume_offset == 0)):
                # Subscribe and capture the authorized boundary without yielding. New output
                # enters this viewer's queue while the baseline and capped replay are sent.
                epoch = fanout.ring.epoch if fanout is not None else lease.epoch
                if live_edge and resume_epoch != epoch:
                    recovery_close = True
                    preserve_reader()
                    await state.sockets.send_text(
                        websocket,
                        json.dumps(
                            {
                                "kind": "reset",
                                "detail": "stale_epoch",
                            }
                        ),
                    )
                    await close_stream("stale_epoch")
                    return
                end = fanout.ring.end_offset if fanout is not None else 0
                base = fanout.ring.base_offset if fanout is not None else 0
                start = end if live_edge else max(base, end - FIRST_REPLAY_MAX_BYTES)
                baseline = {
                    "kind": "history_reset",
                    "epoch": epoch,
                    "offset": start,
                    "incomplete": live_edge or start > base,
                }
                resume_epoch, resume_offset, resume_params = epoch, start, True
            if resume_params:
                # ring resume: exact continuation while retained,
                # else an explicit reset doc (never a forged tail).
                try:
                    stream = state.backend.read_stream(lease_id, resume_epoch, resume_offset)
                except BackendError as exc:
                    await close_stream(exc.code)
                    return
                if stream.status != "ok":
                    recovery_close = stream.status in ("stale_epoch", "stale_offset")
                    if recovery_close:
                        preserve_reader()
                    await state.sockets.send_text(
                        websocket, json.dumps({"kind": "reset", "detail": stream.status})
                    )
                    await close_stream(stream.status)
                    return
                if lease.resume_pending:
                    try:
                        state.backend.finish_reader_handoff(
                            lease_id, grant.account, grant.device_id, grant.generation
                        )
                    except BackendError as exc:
                        await close_stream(exc.code)
                        return
                if grant.resume:
                    state.tickets.discard_resume(lease_id, grant.account, grant.device_id)
                if baseline is not None:
                    await state.sockets.send_text(websocket, json.dumps(baseline))
                for offset, data in stream.chunks:
                    await state.sockets.send_bytes(
                        websocket, terminal_wire.encode_output(stream.epoch, offset, data)
                    )
            else:
                # Ticket-only first connect replays retained ring bytes like a first-connect resume,
                # so pre-connect publishes (the attach greeting) reach the viewer; the ring is the
                # single backlog authority. Stale reads surface as explicit reset docs, never
                # blanks.
                try:
                    stream = state.backend.read_stream(lease_id, "", 0)
                except BackendError as exc:
                    await close_stream(exc.code)
                    return
                if stream.status != "ok":
                    recovery_close = stream.status in ("stale_epoch", "stale_offset")
                    if recovery_close:
                        preserve_reader()
                    await state.sockets.send_text(
                        websocket, json.dumps({"kind": "reset", "detail": stream.status})
                    )
                    await close_stream(stream.status)
                    return
                if sum(len(data) for _, data in stream.chunks) > VIEWER_QUEUE_MAX_BYTES:
                    recovery_close = True
                    await state.sockets.send_text(
                        websocket, json.dumps({"kind": "reset", "detail": "history_incomplete"})
                    )
                    await close_stream("history_incomplete")
                    return
                for offset, data in stream.chunks:
                    await state.sockets.send_bytes(
                        websocket, terminal_wire.encode_output(stream.epoch, offset, data)
                    )
            while True:
                try:
                    async with asyncio.timeout(TERMINAL_PUSH_POLL_S):
                        message = await websocket.receive()
                except TimeoutError:
                    push_detail = await push_pending()
                    if push_detail is not None:
                        recovery_close = push_detail in ("stale_epoch", "overflow", "reset")
                        await close_stream(push_detail)
                        return
                    continue
                if message["type"] == "websocket.disconnect":
                    break
                if "bytes" in message and message["bytes"] is not None:
                    if "terminal.control" not in grant.capabilities:
                        await close_stream("unsupported_capability")
                        return
                    try:
                        chunk = terminal_wire.decode_input(message["bytes"])
                    except ValueError:
                        await close_stream("forbidden")
                        return
                    if chunk.lease_id != lease_id:
                        await close_stream("forbidden")
                        return
                    if chunk.generation > lease.generation:
                        await close_stream("forbidden")
                        return
                    if not state.input_bytes.check(lease_id, state.now_ms(), cost=len(chunk.data)):
                        if not await report_input_limit():
                            return
                        continue
                    try:
                        fresh = state.backend.write_input(
                            lease_id,
                            chunk.generation,
                            chunk.input_seq,
                            chunk.kind,
                            chunk.data,
                        )
                        if fresh:
                            await forward_input(chunk)
                    except BackendError as exc:
                        if exc.code == "rate_limited":
                            if not await report_input_limit():
                                return
                            continue
                        if exc.code == "input_failed":
                            await state.sockets.send_text(
                                websocket,
                                json.dumps(
                                    {
                                        "kind": "gap",
                                        "detail": "input_failed",
                                        "generation": chunk.generation,
                                        "input_seq": chunk.input_seq,
                                    }
                                ),
                            )
                            continue
                        recovery_close = exc.code in (
                            "stale_revision",
                            "host_offline",
                        )
                        trace.emit(
                            "socket_close", reason=exc.code, source="server", socket=websocket
                        )
                        if not disconnected():
                            with suppress(Exception):
                                await state.sockets.send_text(
                                    websocket,
                                    json.dumps(
                                        {
                                            "kind": "gap"
                                            if exc.code in ("stale_revision", "host_offline")
                                            else "bye",
                                            "detail": exc.code,
                                            **(
                                                {
                                                    "generation": chunk.generation,
                                                    "input_seq": chunk.input_seq,
                                                }
                                                if 1 <= chunk.generation <= 0xFFFFFFFF
                                                and 1 <= chunk.input_seq <= 0xFFFFFFFF
                                                else {}
                                            ),
                                        }
                                    ),
                                )
                        if exc.code not in ("stale_revision", "host_offline"):
                            await close_stream(exc.code)
                        return
                elif "text" in message and message["text"] is not None:
                    # Character count, not bytes: a parse-bomb bound, not a
                    # byte bound (multibyte input may exceed it in bytes).
                    if len(message["text"]) > TUNNEL_MAX_TEXT:
                        # Tunnel parity: never parse an unbounded control
                        # string (valid control docs are ~300 bytes).
                        await close_stream("forbidden")
                        return
                    try:
                        control = json.loads(message["text"])
                        validate("relaydeck/gateway/v1/terminal-control", control)
                    except (ValueError, SchemaError):
                        await close_stream("forbidden")
                        return
                    if control["kind"] == "bye":
                        break
                push_detail = await push_pending()
                if push_detail is not None:
                    recovery_close = push_detail in ("stale_epoch", "overflow", "reset")
                    await close_stream(push_detail)
                    return
        except (WebSocketDisconnect, TimeoutError):
            trace.emit("socket_close", reason="receive_failure", source="server", socket=websocket)
        finally:
            # Close hygiene (OWASP): unsubscribe the per-connection viewer
            # on EVERY exit so a dead socket never retains its 1 MiB queue.
            trace.emit("socket_close", reason="stream_closed", source="server", socket=websocket)
            if fanout is not None:
                fanout.unsubscribe(viewer_id)
            state.sockets.unregister(websocket)
            if (
                lease.mode == "control"
                and grant.device_id == lease.device_id
                and not state.sockets.has_device_lease(grant.account, grant.device_id, lease_id)
            ):
                replacement_ticket = state.tickets.has_resume(
                    lease_id, grant.account, grant.device_id, lease.generation, state.now_ms()
                )
                handoff = (
                    recovery_close or replacement_ticket
                ) and state.backend.begin_reader_handoff(
                    lease_id, grant.account, grant.device_id, lease.generation
                )
                if not handoff:
                    with suppress(BackendError):
                        await release_lease_and_close(
                            state, lease_id, grant.account, grant.device_id
                        )

    return router
