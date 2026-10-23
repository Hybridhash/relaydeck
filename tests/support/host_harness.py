"""Host-mode Flet render harness (no Flutter required).

Simulates the client round-trip the same way FletSocketServer does:
UPDATE_CONTROL_PROPS via ``session.apply_patch`` followed by CONTROL_EVENT
via ``session.dispatch_event``. Renders run on a real Session with a
capturing fake connection.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from flet.messaging.connection import Connection
from flet.messaging.protocol import Message
from flet.messaging.session import Session
from flet.pubsub.pubsub_hub import PubSubHub


class FakeConnection(Connection):
    def __init__(self) -> None:
        super().__init__()  # type: ignore[no-untyped-call]
        self.sent: list[Message] = []
        self.data_frames: list[tuple[int, bytes]] = []

    def send_message(self, message: Message) -> None:
        self.sent.append(message)

    def send_data_channel_frame(self, channel_id: int, payload: bytes) -> None:
        self.data_frames.append((channel_id, payload))


class HostApp:
    """One rendered session with helpers to find controls and send input."""

    def __init__(self, session: Session, conn: FakeConnection) -> None:
        self.session = session
        self.conn = conn

    @property
    def page(self) -> Any:
        return self.session.page

    def find(self, key: str) -> Any:
        """Depth-first search of the rendered tree for a control with key."""
        stack: list[Any] = [self.page.views[0].controls]
        overlay = getattr(self.page, "_dialogs", None)
        if overlay is not None:
            stack.extend(overlay.controls or [])
        seen: set[int] = set()
        while stack:
            control = stack.pop()
            if control is None or id(control) in seen:
                continue
            seen.add(id(control))
            if getattr(control, "key", None) == key:
                return control
            # Component bodies render into `_b` (single control or list).
            body = getattr(control, "_b", None)
            if body is not None:
                stack.extend(body if isinstance(body, list) else [body])
            for attr in (
                "controls",
                "content",
                "destinations",
                "actions",
                "items",
                "leading",
                "title",
                "subtitle",
                "trailing",
            ):
                try:
                    value = getattr(control, attr, None)
                except Exception:  # noqa: S112 - tolerant tree walk
                    continue
                if value is None:
                    continue
                if isinstance(value, list | tuple):
                    stack.extend(value)
                else:
                    stack.append(value)
        raise KeyError(f"control with key {key!r} not found")

    async def settle(self, rounds: int = 10) -> None:
        for _ in range(rounds):
            await asyncio.sleep(0.01)

    async def wait_for(self, key: str, timeout: float = 10.0) -> Any:
        """The control once it renders; a loaded runner can need more than one settle."""
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            try:
                return self.find(key)
            except KeyError:
                if asyncio.get_running_loop().time() >= deadline:
                    raise
            await asyncio.sleep(0.01)

    async def set_props_and_fire(
        self, key: str, props: dict[str, Any], event: str, data: Any = ""
    ) -> None:
        control = self.find(key)
        if props:
            self.session.apply_patch(control._i, props)
        await self.session.dispatch_event(control._i, event, data)
        await self.settle()


def button_label(control: Any) -> str:
    """Visible label of a button: a plain `content` string, or the first
    Text inside a design-system button's content tree."""
    queue: list[Any] = [control]
    while queue:
        node = queue.pop(0)
        if node is None:
            continue
        if type(node).__name__ == "Text" and isinstance(getattr(node, "value", None), str):
            return str(node.value)
        content = getattr(node, "content", None)
        if isinstance(content, str):
            return content
        if content is not None:
            queue.append(content)
        queue.extend(getattr(node, "controls", None) or [])
    raise AssertionError(f"no label in {control!r}")


# Flet's per-session scheduler task is referenced only through a cycle with its own session,
# so a test that drops its `HostApp` (e.g. by reassigning `app` in a loop) lets the cyclic GC
# destroy the still-pending task, which asyncio then logs at an arbitrary later moment
# ("Task was destroyed but it is pending!"). Keep every session alive until its loop closes
# (`asyncio.run` cancels its tasks when it does); entries of closed loops are pruned on the
# next render, so memory stays bounded to the current test.
_LIVE: list[tuple[asyncio.AbstractEventLoop, HostApp]] = []


def _retain(loop: asyncio.AbstractEventLoop, app: HostApp) -> None:
    _LIVE[:] = [(kept, held) for kept, held in _LIVE if not kept.is_closed()]
    _LIVE.append((loop, app))


async def render_component(component: Callable[..., Any], *args: Any, **kwargs: Any) -> HostApp:
    """Render a declarative component into a fresh host session."""
    loop = asyncio.get_running_loop()
    executor = ThreadPoolExecutor(max_workers=2)
    hub = PubSubHub(loop=loop, executor=executor)
    conn = FakeConnection()
    conn.loop = loop
    conn.executor = executor
    conn.pubsubhub = hub
    session = Session(conn)
    session.attach_connection(conn)
    session.page.render(component, *args, **kwargs)
    # Same as a real client registering: serialize the full tree, which
    # mounts components and renders their bodies.
    session.get_page_patch()  # type: ignore[no-untyped-call]
    app = HostApp(session, conn)
    _retain(loop, app)
    await app.settle()
    return app


def run[T](coro: Coroutine[Any, Any, T]) -> T:
    """Run a coroutine to completion (keeps tests sync, no plugin needed)."""
    return asyncio.run(coro)
