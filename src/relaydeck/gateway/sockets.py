"""Live terminal-socket registry.

Revocation closes existing sockets and invalidates leases immediately
instead of waiting for token expiry. The registry tracks open terminal
sockets by account and by lease so session revoke, device revoke, and
lease close can all sever the right connections. The
lease-close route calls `close_lease_sockets`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from relaydeck.contracts.tunnel import SEND_TIMEOUT_S
from relaydeck.lease_diagnostics import LeaseTrace
from relaydeck.offload import settled
from relaydeck.tunnel_queue import TunnelWriter


@dataclass
class SocketRegistry:
    _by_account: dict[str, set[int]] = field(default_factory=dict)
    _by_device: dict[tuple[str, str], set[int]] = field(default_factory=dict)
    _by_lease: dict[str, set[int]] = field(default_factory=dict)
    _by_host: dict[str, set[int]] = field(default_factory=dict)
    _sockets: dict[int, Any] = field(default_factory=dict)
    _keys: dict[int, tuple[str, str]] = field(default_factory=dict)
    _devices: dict[int, tuple[str, str]] = field(default_factory=dict)
    _hosts: dict[int, str] = field(default_factory=dict)
    _host_bindings: dict[int, Any] = field(default_factory=dict)
    _send_locks: dict[int, asyncio.Lock] = field(default_factory=dict)
    _diagnostics: dict[int, LeaseTrace] = field(default_factory=dict)
    _writers: dict[int, TunnelWriter] = field(default_factory=dict)
    _host_current: dict[str, int] = field(default_factory=dict)

    def __len__(self) -> int:
        return len(self._sockets)

    def leases(self) -> tuple[str, ...]:
        return tuple(self._by_lease)

    def has_device_lease(
        self, account: str, device_id: str, lease_id: str, *, excluding: Any = None
    ) -> bool:
        return bool(
            (self._by_device.get((account, device_id), set()) & self._by_lease.get(lease_id, set()))
            - {id(excluding)}
        )

    def register(
        self,
        account: str,
        lease_id: str,
        socket: Any,
        device_id: str = "",
        *,
        diagnostics: LeaseTrace | None = None,
    ) -> None:
        key = id(socket)
        self._sockets[key] = socket
        self._keys[key] = (account, lease_id)
        if diagnostics is not None:
            self._diagnostics[key] = diagnostics
        self._by_account.setdefault(account, set()).add(key)
        self._by_lease.setdefault(lease_id, set()).add(key)
        if device_id:
            self._devices[key] = (account, device_id)
            self._by_device.setdefault((account, device_id), set()).add(key)

    def register_host(self, host_id: str, socket: Any, binding: Any = None) -> None:
        """Track a home-connector tunnel socket (host-keyed)."""
        key = id(socket)
        self._sockets[key] = socket
        self._hosts[key] = host_id
        self._host_bindings[key] = binding
        self._by_host.setdefault(host_id, set()).add(key)
        self._host_current[host_id] = key

    def host_current(self, host_id: str, socket: Any) -> bool:
        return self._host_current.get(host_id) == id(socket) and id(socket) in self._sockets

    def bind_writer(self, socket: Any, writer: TunnelWriter) -> None:
        self._writers[id(socket)] = writer

    def lease_sockets(self, lease_id: str) -> tuple[Any, ...]:
        """Capture concrete sockets before yielding; never resolve a replacement later."""
        return tuple(self._sockets[k] for k in self._by_lease.get(lease_id, ()))

    def host_sockets(self, host_id: str) -> tuple[Any, ...]:
        return tuple(self._sockets[k] for k in self._by_host.get(host_id, ()))

    def unregister(self, socket: Any) -> None:
        key = id(socket)
        keys = self._keys.pop(key, None)
        device = self._devices.pop(key, None)
        host = self._hosts.pop(key, None)
        self._host_bindings.pop(key, None)
        writer = self._writers.pop(key, None)
        if writer is not None:
            writer.close()
        if host is not None and self._host_current.get(host) == key:
            self._host_current.pop(host, None)
        self._sockets.pop(key, None)
        self._send_locks.pop(key, None)
        self._diagnostics.pop(key, None)
        if keys is not None:
            account, lease_id = keys
            for index, name in ((self._by_account, account), (self._by_lease, lease_id)):
                self._discard(index, name, key)
        if device is not None:
            self._discard(self._by_device, device, key)
        if host is not None:
            self._discard(self._by_host, host, key)

    @staticmethod
    def _discard(index: dict[Any, set[int]], name: Any, key: int) -> None:
        bucket = index.get(name)
        if bucket is not None:
            bucket.discard(key)
            if not bucket:
                del index[name]

    async def close_account(self, account: str, reason: str) -> int:
        return await self._close(list(self._by_account.get(account, ())), reason)

    async def close_device(self, account: str, device_id: str, reason: str) -> int:
        return await self._close(list(self._by_device.get((account, device_id), ())), reason)

    async def close_lease(self, lease_id: str, reason: str) -> int:
        return await self._close(list(self._by_lease.get(lease_id, ())), reason)

    async def close_host(self, host_id: str, reason: str) -> int:
        return await self._close(list(self._by_host.get(host_id, ())), reason)

    async def close_socket(self, socket: Any, reason: str) -> int:
        """Close this registered socket at most once, including concurrent revocation."""
        return await self._close([id(socket)], reason)

    def host_count(self, host_id: str, binding: Any = None) -> int:
        """Live connector-tunnel sockets for one host (normally <= 1)."""
        return sum(
            1
            for key in self._by_host.get(host_id, ())
            if binding is None or self._host_bindings.get(key) == binding
        )

    async def send_text(self, socket: Any, text: str) -> None:
        """Serialize all writers on a socket, including the lock wait bound."""
        writer = self._writers.get(id(socket))
        if writer is not None:
            await writer.send(text)
            return
        lock = self._send_locks.setdefault(id(socket), asyncio.Lock())
        async with asyncio.timeout(SEND_TIMEOUT_S):
            async with lock:
                await socket.send_text(text)

    async def send_bytes(self, socket: Any, data: bytes) -> None:
        lock = self._send_locks.setdefault(id(socket), asyncio.Lock())
        async with asyncio.timeout(SEND_TIMEOUT_S):
            async with lock:
                await socket.send_bytes(data)

    async def notify_host(
        self,
        host_id: str,
        text: str,
        binding: Any = None,
        *,
        before: Callable[[], Awaitable[bool]] | None = None,
    ) -> int:
        """Best-effort text push to every live tunnel of one host (the
        command.dispatch pump usually sends on its own socket instead;
        this covers out-of-pump sends). Failures are tolerated like
        notify_lease."""
        notified = 0
        for key in list(self._by_host.get(host_id, ())):
            if self._host_current.get(host_id) != key:
                continue
            if binding is not None and self._host_bindings.get(key) != binding:
                continue
            socket = self._sockets.get(key)
            if socket is None:
                continue
            try:
                writer = self._writers.get(key)
                if before is not None and writer is not None:
                    if not await writer.send(text, before=before):
                        continue
                else:
                    if before is not None and not await before():
                        continue
                    await self.send_text(socket, text)
            except Exception:  # noqa: BLE001 - notify is best-effort
                continue
            notified += 1
        return notified

    async def notify_lease(self, lease_id: str, text: str) -> int:
        """Best-effort text push to every socket on one lease. Takeover
        displacement sends the reset doc through here before
        close_lease so the client drives reset+notice instead of mapping
        a bare close to host_offline. Send failures are tolerated like
        _close; sockets stay registered for the close that follows."""
        notified = 0
        for key in list(self._by_lease.get(lease_id, ())):
            socket = self._sockets.get(key)
            if socket is None:
                continue
            try:
                await self.send_text(socket, text)
            except Exception:  # noqa: BLE001 - notify is best-effort
                continue
            notified += 1
        return notified

    async def _close(self, keys: list[int], reason: str) -> int:
        closed = 0
        for key in keys:
            socket = self._sockets.get(key)
            if socket is None:
                continue
            trace = self._diagnostics.get(key)
            self.unregister(socket)
            if trace is not None:
                trace.emit("socket_close", reason=reason, source="server", socket=socket)
            try:
                await settled(
                    asyncio.create_task(
                        asyncio.wait_for(socket.close(code=1008, reason=reason), SEND_TIMEOUT_S)
                    )
                )
            except Exception:  # noqa: BLE001 - close is best-effort
                pass
            closed += 1
        return closed
