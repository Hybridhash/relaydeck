"""Herdr NDJSON adapter.

One request per Unix-socket connection:
connect, send one JSON line, read one reply line, close. Subscriptions
hold a dedicated connection open and yield event lines until cancelled.

Bootstrap subscribes first so no event is missed,
buffers events that arrive while the snapshot reads run, then hands
the live subscription back with the buffered prefix.

No method names are invented: every method string here exists in the
frozen 0.9.1 export (tests/contracts/test_upstream_schema.py). Protocol
must be 22 or the adapter refuses to proceed (fail closed on drift).
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any

EXPECTED_PROTOCOL = 22
DEFAULT_SOCKET = "~/.config/herdr/herdr.sock"
READ_LIMIT = 8 * 1024 * 1024

READ_SOURCES = ("visible", "recent", "recent_unwrapped", "detection")
READ_FORMATS = ("text", "ansi")

ALLOWED_METHODS = frozenset(
    {
        "ping",
        "session.snapshot",
        "agent.list",
        "agent.get",
        "agent.read",
        "agent.prompt",
        "agent.start",
        "agent.send_keys",
        "pane.read",
        "pane.send_text",
        "pane.send_keys",
        "pane.send_input",
        "pane.close",
        "tab.create",
        "tab.close",
        "tab.rename",
        "workspace.create",
        "workspace.close",
        "events.subscribe",
    }
)


class HerdrError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class HerdrProtocolError(HerdrError):
    pass


def resolve_socket(path: str = DEFAULT_SOCKET) -> str:
    if path.startswith("~"):
        path = os.path.expanduser(path)
    if "~" in path:
        raise HerdrProtocolError("bad_socket", "unresolved home in socket path")
    return path


@dataclass(frozen=True)
class HerdrResult:
    id: str
    result: dict[str, Any]


def parse_reply(line: bytes, expect_id: str | None = None) -> HerdrResult:
    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        doc: dict[str, Any] = {}
        for key, value in pairs:
            if key in doc:
                raise ValueError("duplicate reply property")
            doc[key] = value
        return doc

    try:
        doc = json.loads(line, object_pairs_hook=unique_object)
    except ValueError as exc:
        raise HerdrProtocolError("bad_envelope", "reply is not JSON") from exc
    if not isinstance(doc, dict):
        raise HerdrProtocolError("bad_envelope", "reply is not an object")
    # An upstream refusal is evidence for safe retry only after correlation
    # and an unambiguous envelope have been established, just like success.
    if "id" not in doc or not isinstance(doc["id"], str):
        raise HerdrProtocolError("bad_envelope", "reply id is not a string")
    if expect_id is not None and doc["id"] != expect_id:
        raise HerdrProtocolError("bad_envelope", "reply id mismatch")
    if ("result" in doc) == ("error" in doc):
        raise HerdrProtocolError("bad_envelope", "reply must have exactly one result or error")
    if "error" in doc:
        error = doc["error"]
        if (
            not isinstance(error, dict)
            or not isinstance(error.get("code"), str)
            or not error["code"]
            or not isinstance(error.get("message"), str)
        ):
            raise HerdrProtocolError("bad_envelope", "reply error is not well formed")
        raise HerdrError(error["code"], error["message"])
    if not isinstance(doc["result"], dict):
        raise HerdrProtocolError("bad_envelope", "result is not an object")
    return HerdrResult(doc["id"], doc["result"])


def parse_event(line: bytes) -> tuple[str, dict[str, Any]]:
    try:
        doc = json.loads(line)
    except ValueError as exc:
        raise HerdrProtocolError("bad_event", "event is not JSON") from exc
    if not isinstance(doc, dict) or "event" not in doc or "id" in doc:
        raise HerdrProtocolError("bad_event", "not an event line")
    data = doc.get("data", {})
    if not isinstance(data, dict):
        raise HerdrProtocolError("bad_event", "event data is not an object")
    return str(doc["event"]), data


class Subscription:
    """One held-open subscription connection after acknowledgement."""

    def __init__(self, reader: Any, writer: Any) -> None:
        self._reader = reader
        self._writer = writer
        self._closed = False

    def __aiter__(self) -> Subscription:
        return self

    async def __anext__(self) -> tuple[str, dict[str, Any]]:
        if self._closed:
            raise StopAsyncIteration
        raw = await self._reader.readline()
        if not raw:
            raise StopAsyncIteration
        return parse_event(raw)

    async def aclose(self) -> None:
        self._closed = True
        self._writer.close()
        try:
            await self._writer.wait_closed()
        except Exception:  # noqa: BLE001 - close is best-effort
            pass


@dataclass(frozen=True)
class BootstrapResult:
    snapshot: dict[str, Any]
    agents: list[dict[str, Any]]
    buffered: list[tuple[str, dict[str, Any]]]
    subscription: Subscription


class HerdrAdapter:
    """Async request/response client. Connection factory is injectable so
    tests run against a scripted fake socket."""

    def __init__(
        self,
        socket_path: str = DEFAULT_SOCKET,
        timeout: float = 10.0,
        opener: Callable[[], Any] | None = None,
    ) -> None:
        self._socket_path = resolve_socket(socket_path)
        self._timeout = timeout
        self._opener = opener or self._open
        self._ids = 0

    async def _open(self) -> tuple[Any, Any]:
        return await asyncio.wait_for(
            asyncio.open_unix_connection(self._socket_path), self._timeout
        )

    def _next_id(self, prefix: str = "rd") -> str:
        self._ids += 1
        return f"{prefix}-{self._ids}"

    async def call(
        self, method: str, params: dict[str, Any], *, request_id: str | None = None
    ) -> HerdrResult:
        if method not in ALLOWED_METHODS:
            raise HerdrProtocolError("forbidden_method", f"method {method} not allowlisted")
        rid = request_id or self._next_id()
        line = (
            json.dumps({"id": rid, "method": method, "params": params}, separators=(",", ":"))
            + "\n"
        ).encode()
        reader, writer = await self._opener()
        try:
            writer.write(line)
            await asyncio.wait_for(writer.drain(), self._timeout)
            raw = await asyncio.wait_for(reader.readline(), self._timeout)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:  # noqa: BLE001 - close is best-effort
                pass
        if not raw:
            raise HerdrProtocolError("bad_envelope", "empty reply")
        if len(raw) > READ_LIMIT:
            raise HerdrProtocolError("bad_envelope", "reply too large")
        return parse_reply(raw, expect_id=rid)

    async def ping(self) -> dict[str, Any]:
        result = await self.call("ping", {})
        if result.result.get("protocol") != EXPECTED_PROTOCOL:
            raise HerdrProtocolError(
                "protocol_mismatch",
                f"expected protocol {EXPECTED_PROTOCOL}",
            )
        return result.result

    async def session_snapshot(self) -> dict[str, Any]:
        return (await self.call("session.snapshot", {})).result

    async def agent_list(self) -> list[dict[str, Any]]:
        result = await self.call("agent.list", {})
        agents = result.result.get("agents", [])
        if not isinstance(agents, list):
            raise HerdrProtocolError("bad_envelope", "agent.list shape")
        return agents

    async def agent_get(self, target: str) -> dict[str, Any]:
        return (await self.call("agent.get", {"target": target})).result

    async def agent_read(
        self,
        target: str,
        source: str,
        *,
        lines: int = 200,
        format: str = "text",
        strip_ansi: bool = True,
    ) -> dict[str, Any]:
        if source not in READ_SOURCES:
            raise HerdrProtocolError("bad_params", f"bad read source {source}")
        if format not in READ_FORMATS:
            raise HerdrProtocolError("bad_params", f"bad read format {format}")
        return (
            await self.call(
                "agent.read",
                {
                    "target": target,
                    "source": source,
                    "lines": lines,
                    "format": format,
                    "strip_ansi": strip_ansi,
                },
            )
        ).result

    async def agent_prompt(
        self, target: str, text: str, *, wait_ms: int | None = None
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"target": target, "text": text}
        if wait_ms is not None:
            params["wait"] = {"timeout_ms": wait_ms}
        return (await self.call("agent.prompt", params)).result

    async def agent_start(
        self,
        name: str,
        kind: str,
        pane_id: str,
        args: list[str] | None = None,
        timeout_ms: int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"name": name, "kind": kind, "pane_id": pane_id}
        if args:
            params["args"] = args
        if timeout_ms is not None:
            if not 3000 < timeout_ms <= 300000:
                raise HerdrProtocolError("bad_params", "timeout_ms out of range")
            params["timeout_ms"] = timeout_ms
        return (await self.call("agent.start", params)).result

    async def agent_send_keys(self, target: str, keys: list[str]) -> dict[str, Any]:
        return (await self.call("agent.send_keys", {"target": target, "keys": list(keys)})).result

    async def pane_read(
        self,
        pane_id: str,
        source: str,
        *,
        lines: int = 200,
        format: str = "text",
        strip_ansi: bool = True,
    ) -> dict[str, Any]:
        if source not in READ_SOURCES:
            raise HerdrProtocolError("bad_params", f"bad read source {source}")
        if format not in READ_FORMATS:
            raise HerdrProtocolError("bad_params", f"bad read format {format}")
        return (
            await self.call(
                "pane.read",
                {
                    "pane_id": pane_id,
                    "source": source,
                    "lines": lines,
                    "format": format,
                    "strip_ansi": strip_ansi,
                },
            )
        ).result

    async def pane_send_text(self, pane_id: str, text: str) -> dict[str, Any]:
        return (await self.call("pane.send_text", {"pane_id": pane_id, "text": text})).result

    async def pane_send_keys(self, pane_id: str, keys: list[str]) -> dict[str, Any]:
        return (await self.call("pane.send_keys", {"pane_id": pane_id, "keys": list(keys)})).result

    async def pane_send_input(
        self,
        pane_id: str,
        *,
        text: str = "",
        keys: list[str] | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"pane_id": pane_id}
        if text:
            params["text"] = text
        if keys:
            params["keys"] = list(keys)
        if len(params) == 1:
            raise HerdrProtocolError("bad_params", "text or keys required")
        return (await self.call("pane.send_input", params)).result

    async def pane_close(self, pane_id: str) -> dict[str, Any]:
        return (await self.call("pane.close", {"pane_id": pane_id})).result

    async def tab_create(self, workspace_id: str, *, label: str | None = None) -> dict[str, Any]:
        """tab.create (Schema: TabCreateParams carries optional label/cwd/
        env/focus; passes the gateway shell label through)."""
        params: dict[str, Any] = {"workspace_id": workspace_id}
        if label:
            params["label"] = label
        return (await self.call("tab.create", params)).result

    async def tab_close(self, tab_id: str) -> dict[str, Any]:
        return (await self.call("tab.close", {"tab_id": tab_id})).result

    async def tab_rename(self, tab_id: str, label: str) -> dict[str, Any]:
        return (await self.call("tab.rename", {"tab_id": tab_id, "label": label})).result

    async def workspace_create(
        self, *, cwd: str | None = None, label: str | None = None
    ) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if cwd is not None:
            params["cwd"] = cwd
        if label is not None:
            params["label"] = label
        return (await self.call("workspace.create", params)).result

    async def workspace_close(self, workspace_id: str) -> dict[str, Any]:
        return (await self.call("workspace.close", {"workspace_id": workspace_id})).result

    async def _open_subscription(self, subscriptions: list[dict[str, Any]]) -> Subscription:
        """Open a subscription connection and await the acknowledgement.

        Raises HerdrError for typed upstream rejections (e.g. an invalid
        pane subscription) and HerdrProtocolError for ack mismatch.
        """
        rid = self._next_id("sub")
        line = (
            json.dumps(
                {
                    "id": rid,
                    "method": "events.subscribe",
                    "params": {"subscriptions": subscriptions},
                },
                separators=(",", ":"),
            )
            + "\n"
        ).encode()
        reader, writer = await self._opener()
        try:
            writer.write(line)
            await asyncio.wait_for(writer.drain(), self._timeout)
            raw = await asyncio.wait_for(reader.readline(), self._timeout)
            if not raw:
                raise HerdrProtocolError("upstream_closed", "subscribe ack missing")
            ack = parse_reply(raw, expect_id=rid)
            if ack.result.get("type") != "subscription_started":
                raise HerdrProtocolError("bad_envelope", "subscribe not acked")
        except Exception:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:  # noqa: BLE001 - close is best-effort
                pass
            raise
        return Subscription(reader, writer)

    async def open_subscription(self, subscriptions: list[dict[str, Any]]) -> Subscription:
        """Open one held subscription connection (ack awaited).

        The caller owns the returned subscription (multiplexed drains,
        per-pane re-subscription) and must `aclose` it. Raises
        HerdrError for typed upstream rejections and HerdrProtocolError
        for ack mismatch."""
        return await self._open_subscription(subscriptions)

    async def subscribe(
        self, subscriptions: list[dict[str, Any]]
    ) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        """Yield (event, data) lines until the caller closes the iterator.
        Implemented as an async generator over one held-open connection."""
        sub = await self._open_subscription(subscriptions)
        try:
            async for event in sub:
                yield event
        finally:
            await sub.aclose()

    async def bootstrap(self, subscriptions: list[dict[str, Any]]) -> BootstrapResult:
        """Subscribe, snapshot, and buffer concurrently.

        The subscription is acknowledged before any snapshot read runs;
        events arriving during the reads are buffered and returned ahead
        of the live stream. EOF on the subscription mid-bootstrap means
        the daemon went away: fail closed with upstream_closed so the
        caller retries the whole bootstrap.
        """
        await self.ping()
        sub = await self._open_subscription(subscriptions)
        buffered: list[tuple[str, dict[str, Any]]] = []
        closed = False

        async def collect() -> None:
            nonlocal closed
            while True:
                try:
                    buffered.append(await sub.__anext__())
                except StopAsyncIteration:
                    closed = True
                    return

        collector = asyncio.create_task(collect())
        try:
            snapshot = await self.session_snapshot()
            agents = await self.agent_list()
        except Exception:
            collector.cancel()
            await sub.aclose()
            raise
        # Yield so events already in flight are collected before we stop.
        await asyncio.sleep(0)
        collector.cancel()
        try:
            await collector
        except asyncio.CancelledError:
            pass
        except Exception:
            await sub.aclose()
            raise
        if closed:
            await sub.aclose()
            raise HerdrProtocolError("upstream_closed", "daemon closed subscription")
        return BootstrapResult(
            snapshot=snapshot, agents=agents, buffered=buffered, subscription=sub
        )
