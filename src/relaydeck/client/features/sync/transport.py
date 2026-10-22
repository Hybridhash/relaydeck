"""Gateway transport seam.

`GatewayTransport` speaks HTTP (snapshot, stream, commands) and
`TerminalSocket` speaks the terminal WebSocket. Production
implementations use httpx/websockets; tests use scripted fakes.
"""

from __future__ import annotations

import asyncio
import json
from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass
from typing import Any

import httpx
import websockets.asyncio.client as ws_client

from relaydeck.client.features.sync.sse import SseFrame, SseParser
from relaydeck.contracts import paths
from relaydeck.contracts.cursors import AFTER_SEQUENCE_HEADER, MAX_CURSOR_LEN
from relaydeck.contracts.schemas import SchemaError, validate


class TransportError(Exception):
    def __init__(self, code: str, message: str, retryable: bool = True) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.retryable = retryable


class GatewayTransport(ABC):
    async def usage(
        self,
        host_id: str,
        *,
        days: int | None = None,
        start: str | None = None,
        end: str | None = None,
        space_id: str | None = None,
        cursor: str | None = None,
        bucket: str | None = None,
    ) -> dict[str, Any]:
        """One page of the host's usage report. Transports that cannot read it report that."""
        raise TransportError("usage_unavailable", "usage is not available", False)

    @abstractmethod
    async def me(self) -> dict[str, Any]: ...

    @abstractmethod
    async def hosts(self) -> dict[str, Any]: ...

    @abstractmethod
    async def snapshot(self, host_id: str) -> dict[str, Any]: ...

    @abstractmethod
    async def agents(
        self,
        host_id: str,
        limit: int = 50,
        page_token: str = "",
        *,
        project: str = "",
        status: str = "",
        query: str = "",
    ) -> dict[str, Any]: ...

    @abstractmethod
    def stream(self, host_id: str, cursor: str) -> AsyncGenerator[SseFrame]: ...

    @abstractmethod
    async def submit_command(self, command: dict[str, Any]) -> dict[str, Any]: ...

    @abstractmethod
    async def command_status(self, host_id: str, command_id: str) -> dict[str, Any] | None: ...

    @abstractmethod
    async def terminal_ticket(self, lease_id: str) -> dict[str, Any]: ...

    async def create_attachment(
        self,
        host_id: str,
        terminal_id: str,
        *,
        kind: str,
        takeover: bool,
        cols: int = 80,
        rows: int = 24,
    ) -> dict[str, Any]:
        """POST an explicit upstream attach authorization.
        Concrete default raises: scripted fakes override only what
        they drive."""
        raise NotImplementedError

    async def create_lease(self, attachment_id: str, *, mode: str) -> dict[str, Any]:
        """Issue a view/control lease on an owned attachment."""
        raise NotImplementedError

    async def renew_lease(self, lease_id: str) -> dict[str, Any]:
        """Foreground renewal of a held lease."""
        raise NotImplementedError


def _auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@dataclass
class HttpxGatewayTransport(GatewayTransport):
    base_url: str
    token: Callable[[], Any]  # async callable returning the access token
    client: httpx.AsyncClient
    timeout: float = 30.0
    commands: Any = None

    async def _token(self) -> str:
        value = await self.token()
        if not value:
            raise TransportError("unauthenticated", "no access token", False)
        return str(value)

    async def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        try:
            expected_version = kwargs.pop("_command_version", None)
            token = await self._token()
            if self.commands is not None and (url.endswith("/commands") or "/commands/" in url):
                self.commands.check()
                if self.commands.version() != expected_version:
                    raise TransportError(
                        "command_recovery",
                        "Command lifecycle changed; check status before another mutation.",
                        False,
                    )
            return await self.client.request(
                method,
                url,
                headers=_auth_headers(token),
                timeout=self.timeout,
                **kwargs,
            )
        except httpx.HTTPError as exc:
            raise TransportError("host_offline", str(exc), True) from exc

    async def me(self) -> dict[str, Any]:
        response = await self._request("GET", self.base_url + paths.ME)
        return self._management_doc(response, "me")

    async def hosts(self) -> dict[str, Any]:
        response = await self._request("GET", self.base_url + paths.HOSTS)
        return self._management_doc(response, "hosts")

    async def usage(
        self,
        host_id: str,
        *,
        days: int | None = None,
        start: str | None = None,
        end: str | None = None,
        space_id: str | None = None,
        cursor: str | None = None,
        bucket: str | None = None,
    ) -> dict[str, Any]:
        """GET /v1/hosts/{host}/usage. `days` (1..90) and start/end are mutually exclusive; the
        gateway's `error` envelope (usage_disabled, usage_unavailable) is kept as the code.
        `bucket="hour"` asks for hourly buckets (one day only; the gateway refuses other ranges)."""
        params: dict[str, str] = {}
        if days is not None:
            params["days"] = str(days)
        if start is not None:
            params["from"] = start
        if end is not None:
            params["to"] = end
        if space_id is not None:
            params["space_id"] = space_id
        if cursor is not None:
            params["cursor"] = cursor
        if bucket is not None:
            params["bucket"] = bucket
        response = await self._request(
            "GET",
            self.base_url + paths.usage(host_id),
            params=params,
        )
        if 200 <= response.status_code < 300:
            return self._checked(response)
        try:
            body = response.json()
        except ValueError:
            body = {}
        code = str(body.get("error", "usage_unavailable")) if isinstance(body, dict) else ""
        raise TransportError(code or "usage_unavailable", "usage request refused", False)

    async def register_device(self, device_id: str, token: str) -> dict[str, Any]:
        response = await self._request(
            "POST",
            self.base_url + paths.DEVICES,
            json={
                "device_id": device_id,
                "token": token,
                "platform": "android",
            },
        )
        doc = self._management_doc(response, "device-registration")
        if doc["device_id"] != device_id:
            raise TransportError("upstream_incompatible", "device scope mismatch", False)
        return doc

    async def resolve_notification(self, value: str, *, link: bool = False) -> dict[str, Any]:
        response = await self._request(
            "POST",
            self.base_url + paths.DEEP_LINK_RESOLVE,
            json={
                "token" if link else "notification_id": value,
            },
        )
        doc = self._management_doc(response, "deep-link-resolution")
        if doc["route"] != {"host_id": doc["host_id"], "session_id": doc["session_id"]}:
            raise TransportError("upstream_incompatible", "notification route mismatch", False)
        return doc

    async def web_push_state(self) -> dict[str, bool]:
        """Whether web push is on for this companion and whether THIS device is subscribed."""
        response = await self._request("GET", self.base_url + paths.PUSH_KEY)
        doc = self._checked(response)
        if (
            set(doc) != {"publicKey", "enabled", "subscribed", "csrf", "choice"}
            or type(doc["enabled"]) is not bool
            or type(doc["subscribed"]) is not bool
        ):
            raise TransportError("upstream_incompatible", "invalid web push state", False)
        return {"enabled": doc["enabled"], "subscribed": doc["subscribed"]}

    async def web_push_preferences(self) -> list[str]:
        """This device's choice: the categories it receives (blocked and done)."""
        response = await self._request("GET", self.base_url + paths.PUSH_PREFS)
        doc = self._checked(response)
        values = doc.get("categories")
        if set(doc) != {"categories"} or not isinstance(values, list):
            raise TransportError("upstream_incompatible", "invalid notification choice", False)
        if not all(isinstance(value, str) for value in values) or not set(values) <= {
            "blocked",
            "done",
        }:
            raise TransportError("upstream_incompatible", "invalid notification choice", False)
        return list(values)

    async def set_web_push_preferences(self, categories: list[str]) -> list[str]:
        response = await self._request(
            "PUT", self.base_url + paths.PUSH_PREFS, json={"categories": list(categories)}
        )
        doc = self._checked(response)
        if set(doc) != {"categories"} or not isinstance(doc["categories"], list):
            raise TransportError("upstream_incompatible", "notification choice not saved", True)
        return list(doc["categories"])

    async def web_push_disable(self) -> None:
        """Settings > Notifications > Turn off: the same DELETE the browser sends, bearer-authed."""
        response = await self._request("DELETE", self.base_url + paths.PUSH_SUBSCRIPTION)
        if response.status_code != 204:
            raise TransportError("upstream_incompatible", "web push not turned off", True)

    async def resolve_web_route(self, token: str) -> dict[str, str]:
        """Resolve a notification tap to a device-owned category, host and session.
        A refused, expired or foreign token raises TransportError; the caller stays on Console."""
        response = await self._request(
            "POST", self.base_url + paths.PUSH_RESOLVE, json={"route": token}
        )
        doc = self._checked(response)
        fields = ("category", "host_id", "session_id")
        if (
            not isinstance(doc, dict)
            or set(doc) != set(fields)
            or not all(isinstance(doc[name], str) and doc[name] for name in fields)
        ):
            raise TransportError("upstream_incompatible", "invalid web route response", False)
        return {name: doc[name] for name in fields}

    def _management_doc(self, response: httpx.Response, kind: str) -> dict[str, Any]:
        doc = self._checked(response)
        try:
            validate(f"relaydeck/gateway/v1/{kind}", doc)
        except SchemaError:
            raise TransportError(
                "upstream_incompatible", "invalid management response", False
            ) from None
        return doc

    async def create_enrollment(self, label: str, pubkey: str) -> dict[str, Any]:
        body = {"host_label": label, "pubkey": pubkey}
        try:
            validate("relaydeck/gateway/v1/enrollment-create", body)
        except SchemaError:
            raise TransportError("forbidden", "invalid enrollment request", False) from None
        response = await self._request("POST", self.base_url + paths.ENROLLMENTS, json=body)
        return self._management_doc(response, "enrollment")

    async def revoke_host(self, host_id: str) -> dict[str, Any]:
        response = await self._request("DELETE", self.base_url + paths.host(host_id))
        return self._management_doc(response, "host-revoke")

    async def devices(self) -> dict[str, Any]:
        response = await self._request("GET", self.base_url + paths.DEVICES)
        return self._management_doc(response, "devices")

    async def metrics(self) -> dict[str, Any]:
        response = await self._request("GET", self.base_url + paths.METRICS)
        return self._management_doc(response, "server-metrics")

    async def enrollment_status(self, enrollment_id: str) -> dict[str, Any]:
        response = await self._request("GET", self.base_url + paths.enrollment(enrollment_id))
        return self._management_doc(response, "enrollment-status")

    async def revoke_device(self, device_id: str) -> dict[str, Any]:
        response = await self._request("DELETE", self.base_url + paths.device(device_id))
        doc = self._checked(response)
        try:
            validate("relaydeck/gateway/v1/device-revoke", doc)
        except SchemaError:
            raise TransportError(
                "upstream_incompatible", "invalid device revoke response", False
            ) from None
        return doc

    async def snapshot(self, host_id: str) -> dict[str, Any]:
        response = await self._request("GET", self.base_url + paths.host_snapshot(host_id))
        return self._checked(response)

    async def agents(
        self,
        host_id: str,
        limit: int = 50,
        page_token: str = "",
        *,
        project: str = "",
        status: str = "",
        query: str = "",
    ) -> dict[str, Any]:
        params: dict[str, str] = {"limit": str(limit)}
        if page_token:
            params["page_token"] = page_token
        for key, value in (("project", project), ("status", status), ("q", query)):
            if value:
                params[key] = value
        response = await self._request(
            "GET", self.base_url + paths.host_agents(host_id), params=params
        )
        doc = self._checked(response)
        try:
            validate("relaydeck/gateway/v1/agents-page", doc)
        except SchemaError:
            raise TransportError("upstream_incompatible", "invalid inventory page", False) from None
        if doc["host_id"] != host_id:
            raise TransportError("upstream_incompatible", "inventory host mismatch", False)
        return doc

    async def mark_agent_read(
        self,
        host_id: str,
        agent_id: str,
        host_epoch: str,
        activity_id: str,
    ) -> dict[str, Any]:
        response = await self._request(
            "POST",
            self.base_url + paths.agent_read(host_id, agent_id),
            json={"host_epoch": host_epoch, "activity_id": activity_id},
        )
        doc = self._checked(response)
        try:
            validate("relaydeck/gateway/v1/agent-read", doc)
        except SchemaError:
            raise TransportError("upstream_incompatible", "invalid read marker", False) from None
        if (
            doc["host_id"],
            doc["agent_id"],
            doc["host_epoch"],
            doc["activity_id"],
            doc["unread"],
        ) != (
            host_id,
            agent_id,
            host_epoch,
            activity_id,
            False,
        ):
            raise TransportError("upstream_incompatible", "read marker context mismatch", False)
        return doc

    async def stream(self, host_id: str, cursor: str) -> AsyncGenerator[SseFrame]:
        params = {"host_id": host_id}
        if cursor:
            params["cursor"] = cursor
        try:
            async with self.client.stream(
                "GET",
                self.base_url + paths.EVENTS,
                params=params,
                headers=_auth_headers(await self._token()),
                timeout=self.timeout,
            ) as response:
                if response.status_code != 200:
                    await response.aread()
                    raise self._as_error(response)
                # Client-only readiness marker after accepted HTTP headers.
                # Idle streams may contain only SSE comments; snapshots do
                # not prove that this authenticated request was accepted.
                boundary = response.headers.get(AFTER_SEQUENCE_HEADER)
                opened = ""
                if boundary is not None:
                    if (
                        not boundary
                        or len(boundary) > MAX_CURSOR_LEN
                        or not boundary.isascii()
                        or not boundary.isdecimal()
                    ):
                        raise TransportError("upstream_incompatible", "bad stream boundary", False)
                    opened = json.dumps(
                        {"host_id": host_id, "cursor": cursor, "sequence": int(boundary)}
                    )
                yield SseFrame("stream_open", opened)
                parser = SseParser()
                async for chunk in response.aiter_bytes():
                    for frame in parser.feed(chunk):
                        yield frame
        except httpx.HTTPError as exc:
            raise TransportError("host_offline", str(exc), True) from exc

    async def submit_command(self, command: dict[str, Any]) -> dict[str, Any]:
        version = self.commands.version() if self.commands is not None else None
        if self.commands is not None:
            await self.commands.begin(command)
        response = await self._request(
            "POST",
            self.base_url + paths.host_commands(command["host_id"]),
            json=command,
            _command_version=version,
        )
        doc = self._checked(response)
        if self.commands is not None:
            await self.commands.observe(command["host_id"], command["command_id"], doc)
        return doc

    async def command_status(self, host_id: str, command_id: str) -> dict[str, Any] | None:
        response = await self._request(
            "GET",
            self.base_url + paths.host_command(host_id, command_id),
            _command_version=self.commands.version() if self.commands is not None else None,
        )
        doc = None if response.status_code == 404 else self._checked(response)
        if self.commands is not None:
            await self.commands.observe(host_id, command_id, doc)
        return doc

    async def terminal_ticket(self, lease_id: str) -> dict[str, Any]:
        response = await self._request(
            "POST", self.base_url + paths.TERMINAL_TICKETS, json={"lease_id": lease_id}
        )
        return self._checked(response)

    async def create_attachment(
        self,
        host_id: str,
        terminal_id: str,
        *,
        kind: str,
        takeover: bool,
        cols: int = 80,
        rows: int = 24,
    ) -> dict[str, Any]:
        response = await self._request(
            "POST",
            self.base_url + paths.terminal_attachments(host_id, terminal_id),
            json={"kind": kind, "takeover": takeover, "cols": cols, "rows": rows},
        )
        return self._checked(response)

    async def create_lease(self, attachment_id: str, *, mode: str) -> dict[str, Any]:
        response = await self._request(
            "POST",
            self.base_url + paths.attachment_leases(attachment_id),
            json={"mode": mode},
        )
        return self._checked(response)

    async def renew_lease(self, lease_id: str) -> dict[str, Any]:
        response = await self._request("POST", self.base_url + paths.lease_renew(lease_id), json={})
        return self._checked(response)

    def _checked(self, response: httpx.Response) -> dict[str, Any]:
        if 200 <= response.status_code < 300:
            try:
                data = response.json()
            except ValueError as exc:
                raise TransportError("upstream_incompatible", "bad json", False) from exc
            if not isinstance(data, dict):
                raise TransportError("upstream_incompatible", "bad shape", False)
            return data
        raise self._as_error(response)

    def _as_error(self, response: httpx.Response) -> TransportError:
        try:
            data = response.json()
        except ValueError:
            data = {}
        code = (
            str(data.get("code", "upstream_incompatible"))
            if isinstance(data, dict)
            else "upstream_incompatible"
        )
        message = (
            str(data.get("message", "gateway error")) if isinstance(data, dict) else "gateway error"
        )
        # Explicit retry set: only transport-level and rate/stale codes
        # reconnect. Every other envelope is terminal for this sync run.
        retryable = response.status_code in (503, 429) or code in (
            "host_offline",
            "stale_epoch",
            "stale_revision",
        )
        if code in ("unauthenticated", "forbidden"):
            retryable = False
        return TransportError(code, message, retryable)


class TerminalSocket(ABC):
    @abstractmethod
    async def connect(self) -> None: ...

    @abstractmethod
    async def send_bytes(self, data: bytes) -> None: ...

    @abstractmethod
    async def send_text(self, data: str) -> None: ...

    @abstractmethod
    async def receive(self) -> tuple[str, Any]:
        """Returns ("bytes", payload) or ("text", payload)."""

    @abstractmethod
    async def close(self) -> None: ...


# The default close handshake waits 10 s, and after an unread backlog the peer's close frame is
# never read, so every close() stalls that long. Closing is best-effort, so bound it.
WS_CLOSE_TIMEOUT_S = 1.0


@dataclass
class WebsocketTerminalSocket(TerminalSocket):
    url: str
    timeout: float = 30.0
    _ws: Any = None

    async def connect(self) -> None:
        try:
            self._ws = await ws_client.connect(
                self.url, open_timeout=self.timeout, close_timeout=WS_CLOSE_TIMEOUT_S
            )
        except Exception as exc:
            raise TransportError("host_offline", f"ws connect: {exc}") from exc

    async def send_bytes(self, data: bytes) -> None:
        try:
            await self._ws.send(data)
        except Exception as exc:
            raise TransportError("host_offline", f"ws send: {exc}") from exc

    async def send_text(self, data: str) -> None:
        try:
            await self._ws.send(data)
        except Exception as exc:
            raise TransportError("host_offline", f"ws send: {exc}") from exc

    async def receive(self) -> tuple[str, Any]:
        try:
            message = await self._ws.recv()
        except Exception as exc:
            raise TransportError("host_offline", f"ws recv: {exc}") from exc
        if isinstance(message, bytes):
            return ("bytes", message)
        return ("text", message)

    async def close(self) -> None:
        if self._ws is not None:
            ws, self._ws = self._ws, None
            try:
                await asyncio.wait_for(ws.close(), WS_CLOSE_TIMEOUT_S * 2)
            except Exception:  # noqa: BLE001 - close is best-effort (incl. timeout)
                pass
