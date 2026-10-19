"""One foreground owner. Stop is a nonce-bound request, never a PID signal."""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import secrets
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from relaydeck.companion.private import check_private, private_dir, read_private, write_json
from relaydeck.offload import settled

HEARTBEAT_S = 0.25
STALE_S = 3.0


class AlreadyRunning(RuntimeError):
    pass


class Resources(Protocol):
    async def start(self, stop: asyncio.Event) -> None: ...
    def report(self) -> dict[str, Any]: ...
    async def close(self) -> None: ...


class StateLease:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.instance = secrets.token_hex(16)
        self.fd: int | None = None

    def __enter__(self) -> StateLease:
        private_dir(self.directory)
        path = self.directory / "companion.lock"
        if path.exists() or path.is_symlink():
            check_private(path)
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(fd)
            raise AlreadyRunning("a companion already owns this state directory") from exc
        self.fd = fd
        return self

    def __exit__(self, *args: Any) -> None:
        if self.fd is not None:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
            os.close(self.fd)
            self.fd = None

    def publish(self, state: str, **details: Any) -> None:
        write_json(
            self.directory / "companion-status.json",
            {
                "version": 1,
                "instance": self.instance,
                "pid": os.getpid(),
                "heartbeat": time.time(),
                "state": state,
                **details,
            },
        )

    def stop_requested(self) -> bool:
        path = self.directory / "companion-stop.json"
        if not path.exists():
            return False
        try:
            body = json.loads(read_private(path))
            return isinstance(body, dict) and body.get("instance") == self.instance
        except (ValueError, OSError):
            return False


def status(directory: Path) -> dict[str, Any]:
    """Lock ownership is authoritative; PID is diagnostic only."""
    if not directory.exists():
        return {"state": "stopped"}
    check_private(directory, directory=True)
    path = directory / "companion.lock"
    if not path.exists():
        return {"state": "stopped"}
    check_private(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            try:
                body = json.loads(read_private(directory / "companion-status.json"))
            except (ValueError, OSError):
                return {"state": "starting"}
            if not isinstance(body, dict) or body.get("version") != 1:
                return {"state": "unresponsive"}
            heartbeat = body.get("heartbeat")
            if not isinstance(heartbeat, int | float) or abs(time.time() - heartbeat) > STALE_S:
                return {**body, "state": "unresponsive"}
            return body
        else:
            fcntl.flock(fd, fcntl.LOCK_UN)
            return {"state": "stopped"}
    finally:
        os.close(fd)


async def request_stop(directory: Path, *, timeout: float = 12.0) -> bool:
    current = status(directory)
    if current["state"] == "stopped":
        return True
    instance = current.get("instance")
    if not isinstance(instance, str) or not instance:
        return False
    write_json(directory / "companion-stop.json", {"version": 1, "instance": instance})
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        observed = status(directory)
        if observed["state"] in ("stopping", "stopped") or observed.get("instance") not in (
            None,
            instance,
        ):
            return True
        await asyncio.sleep(0.1)
    return False  # Never fall back to killing a process or listener.


async def _close_owned(resources: Resources, lease: StateLease) -> None:
    """Keep cleanup and its heartbeat owned until every started close has settled."""
    closing = asyncio.create_task(resources.close())
    try:
        while not closing.done():
            lease.publish("stopping")
            await asyncio.wait({closing}, timeout=HEARTBEAT_S)
    finally:
        await settled(closing)


async def supervise(
    directory: Path,
    factory: Callable[[], Resources],
    stop: asyncio.Event,
) -> int:
    with StateLease(directory) as lease:
        lease.publish("starting")
        resources: Resources | None = None
        result = 0
        try:
            resources = factory()
            startup = asyncio.create_task(resources.start(stop))
            try:
                while not startup.done():
                    lease.publish("starting")
                    if stop.is_set() or lease.stop_requested():
                        stop.set()
                        startup.cancel()
                        break
                    await asyncio.sleep(HEARTBEAT_S)
                if stop.is_set():
                    startup.cancel()
                    try:
                        await startup
                    except asyncio.CancelledError:
                        pass
                else:
                    await startup
            finally:
                if not startup.done():
                    startup.cancel()
                    try:
                        await startup
                    except asyncio.CancelledError:
                        pass
            while not stop.is_set():
                report = resources.report()
                lease.publish(str(report.pop("state")), **report)
                if report.get("failed"):
                    result = 1
                    break
                if lease.stop_requested():
                    stop.set()
                    break
                await asyncio.sleep(HEARTBEAT_S)
        except BaseException:
            result = 1
            raise
        finally:
            stop.set()
            lease.publish("stopping")
            try:
                if resources is not None:
                    # Cancellation must not abandon cleanup or release the state lock.
                    # The owned task also keeps the nonce/heartbeat live while settling.
                    await settled(asyncio.create_task(_close_owned(resources, lease)))
            except BaseException:
                result = 1
                raise
            finally:
                lease.publish("stopped", exit_code=result)
        return result
