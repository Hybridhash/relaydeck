"""Terminal session construction with injectable runtime services.

The factory supplies ownership operations, transport, clipboard and identity.
Root owns navigation and supervision; construction itself acquires nothing.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from relaydeck.client.adapters.ports import ClipboardPort, CredentialStore
from relaydeck.client.features.shell import ShellSession
from relaydeck.client.features.shell.ops import ShellGatewayOps
from relaydeck.client.features.shell.session import SHELL_LIVE
from relaydeck.client.features.sync.transport import TerminalSocket, WebsocketTerminalSocket
from relaydeck.client.features.terminal.agent_session import AGENT_LIVE, AgentSession


def ws_base_for(base_url: str) -> str:
    """Derive the WebSocket base for terminal streams from the HTTP base."""
    if base_url.startswith("https://"):
        return "wss://" + base_url[len("https://") :]
    if base_url.startswith("http://"):
        return "ws://" + base_url[len("http://") :]
    return base_url


def production_stream_factory(ws_base: str) -> Callable[..., TerminalSocket]:
    """`(lease_id, ticket, resume="") -> socket` over the shipped gateway route
    `/v1/terminal/{lease}?ticket=` (the ticket is single-use and minted per open/recovery).
    `resume` is the controller's `epoch=...&offset=...` when reopening the same lease."""

    def open_stream(lease_id: str, ticket: str, resume: str = "") -> TerminalSocket:
        url = f"{ws_base}/v1/terminal/{quote(lease_id, safe='')}?ticket={quote(ticket, safe='')}"
        return WebsocketTerminalSocket(f"{url}&{resume}" if resume else url)

    return open_stream


def session_live(session: Any) -> bool:
    """True when a session holds a stream worth supervising: live phase
    plus a controller. Single coupling point for the worker's phase
    constants (a rename fails loudly here at integration)."""
    return session.phase in (AGENT_LIVE, SHELL_LIVE) and session.controller is not None


@dataclass
class SessionFactory:
    """Constructor bundle for independently retained terminal sessions.

    `open_stream` takes `(lease_id, ticket)`: the factory reads the live
    lease id from the session at socket-open time (the session assigns
    its lease before the controller starts in every open path) and
    closes it over the ticket-only socket factory the sessions accept.
    """

    ops: ShellGatewayOps
    open_stream: Callable[..., TerminalSocket]
    submit_command: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]
    prompt_text: Callable[[str, str], Awaitable[dict[str, Any]]] | None
    renew_lease: Callable[[str], Awaitable[dict[str, Any]]] | None
    clipboard: ClipboardPort | None
    device_id: str
    observe_command: Callable[[str, str], Awaitable[dict[str, Any]]] | None = None
    draft_store: CredentialStore | None = None
    note_input: Callable[[], None] = lambda: None

    def _socket_factory_for(self, box: dict[str, Any]) -> Callable[[str], TerminalSocket]:
        def factory(ticket: str) -> TerminalSocket:
            session = box.get("session")
            lease = session.lease if session is not None else None
            lease_id = lease.lease_id if lease is not None else ""
            controller = session.controller if session is not None else None
            # A recovery reopening the same lease names its position: when the previous reader
            # closed, the server kept the lease for this device's resume ticket, and admits that
            # reader only with a position. A first open stays ticket-only (retained replay).
            if (
                controller is not None
                and controller.reopening
                and controller.lease_id == lease_id
                and (resume := controller.resume_query())
            ):
                return self.open_stream(lease_id, ticket, resume)
            return self.open_stream(lease_id, ticket)

        return factory

    def make_shell(self, *, host_id: str, host_epoch: str = "") -> ShellSession:
        box: dict[str, Any] = {}
        session = ShellSession(
            ops=self.ops,
            host_id=host_id,
            host_epoch=host_epoch,
            device_id=self.device_id,
            socket_factory=self._socket_factory_for(box),
            clipboard=self.clipboard,
            renew_lease=self.renew_lease,
        )
        box["session"] = session
        session.note_input = self.note_input
        return session

    def make_agent(
        self,
        *,
        host_id: str,
        host_epoch: str,
        agent_id: str,
        target: str,
        initial_status: str = "idle",
        expected_revision: int = 0,
    ) -> AgentSession:
        box: dict[str, Any] = {}
        session = AgentSession(
            ops=self.ops,
            host_id=host_id,
            device_id=self.device_id,
            socket_factory=self._socket_factory_for(box),
            submit_command=self.submit_command,
            prompt_text=self.prompt_text,
            host_epoch=host_epoch,
            agent_id=agent_id,
            target=target,
            initial_status=initial_status,
            expected_revision=expected_revision,
            observe_command=self.observe_command,
            clipboard=self.clipboard,
            draft_store=self.draft_store,
            renew_lease=self.renew_lease,
        )
        box["session"] = session
        session.note_input = self.note_input
        return session
