"""Local-runner seed data and the loopback dev login for `relaydeck.client.serve`.

Development only: the empty and demo snapshots, the demo backend that greets each attachment with
synthetic bytes, and the single-use loopback sign-in behind `--dev-login`. They live outside
`serve.py` so the file that owns the web sign-in boundary carries only production paths, and
outside `relaydeck.client` so the native packager never stages them. `serve.py` re-exports these
names; the two state dicts are shared by identity, so tests and `main()` mutate one object.
"""

from __future__ import annotations

import hmac
import time
from typing import TYPE_CHECKING, Any

from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse

from relaydeck.gateway.backend import FakeBackend

if TYPE_CHECKING:
    from relaydeck.client.serve import WebSessionStore
    from relaydeck.gateway.app import GatewayState


def dev_snapshot(host_id: str, cursor_secret: bytes) -> dict[str, Any]:
    """Empty development snapshot for the local-runner host.

    The local runner has no connector, so no host ever enrolls; without
    a seeded snapshot every sync attempt 404s and the app sits at
    DISCONNECTED. Seeding an empty-but-valid snapshot shows LIVE with
    empty inventory instead. Must validate against the snapshot schema
    (pinned by test).

    The cursor is a real HMAC cursor over sequence 0: the schema
    requires a non-empty cursor and the events route 409s anything it
    cannot authenticate, so there is no valid seed without a secret
    (encode_cursor raises loudly on b"").
    """
    from relaydeck.gateway.cursors import encode_cursor

    cursor = encode_cursor(cursor_secret, {"host_id": host_id, "sequence": 0})
    return {
        "host": {
            "id": host_id,
            "label": host_id,
            "epoch": "dev-epoch-0",
            "reachable": True,
            "protocol": 22,
            "version": "0.9.1",
            "capabilities": [
                "sessions.read",
                "terminal.view",
                "terminal.control",
                "agents.prompt",
                "agents.start",
                "shells.create",
                "terminal.attach",
            ],
        },
        "cursor": cursor,
        "workspaces": [],
        "tabs": [],
        "panes": [],
        "agents": [],
    }


_DEV_LOGIN_TOKEN = {"value": ""}
_DEV_HOSTS = {"value": frozenset({"home-mac"})}


class DemoBackend(FakeBackend):
    """Local-runner backend that greets every new attachment with fixed
    synthetic VT bytes (behind --dev-demo-sessions). There is no PTY
    and no connector: the greeting is the only byte source, and input
    frames are only logged. Never production."""

    def create_attachment(
        self,
        host_id: str,
        terminal_id: str,
        kind: str,
        account: str,
        device_id: str,
        *,
        takeover: bool,
        owner_label: str = "",
        expected_control_revision: str | None = None,
    ) -> tuple[Any, tuple[str, ...]]:
        attachment, displaced = super().create_attachment(
            host_id,
            terminal_id,
            kind,
            account,
            device_id,
            takeover=takeover,
            owner_label=owner_label,
            expected_control_revision=expected_control_revision,
        )
        try:
            self.publish_output(attachment.attachment_id, attachment.epoch, DEMO_GREETING)
        except Exception as exc:  # noqa: BLE001 - demo-only; attach already succeeded
            print(f"WARNING: demo greeting failed ({exc})")
        return attachment, displaced


DEMO_SHELL_TAB = "demo-shell"
DEMO_AGENT_TAB = "demo-agent-tab"
DEMO_AGENT_ID = "demo-agent-1"
DEMO_GREETING = b"\x1b[1;36mRelayDeck demo terminal\x1b[0m\r\n\x1b[32mdemo $\x1b[0m "


def dev_demo_snapshot(host_id: str, cursor_secret: bytes) -> dict[str, Any]:
    """Demo seed for renderer QA: `dev_snapshot` plus one synthetic
    workspace with a shell tab and an agent tab (all ids demo-*).

    Local runner only, behind --dev-demo-sessions. Never production:
    there is no connector, no PTY, and no real host behind these rows;
    the greeting below is the only byte source. Must validate against
    the snapshot schema (pinned by test)."""
    doc = dev_snapshot(host_id, cursor_secret)
    doc["workspaces"] = [
        {
            "id": "demo-ws",
            "host_id": host_id,
            "number": 1,
            "label": "demo",
            "focused": True,
            "pane_count": 2,
            "tab_count": 2,
            "active_tab_id": DEMO_SHELL_TAB,
            "agent_status": "idle",
        }
    ]
    doc["tabs"] = [
        {
            "id": DEMO_SHELL_TAB,
            "host_id": host_id,
            "workspace_id": "demo-ws",
            "number": 1,
            "label": DEMO_SHELL_TAB,
            "focused": True,
            "pane_count": 1,
            "agent_status": "unknown",
        },
        {
            "id": DEMO_AGENT_TAB,
            "host_id": host_id,
            "workspace_id": "demo-ws",
            "number": 2,
            "label": DEMO_AGENT_TAB,
            "focused": False,
            "pane_count": 1,
            "agent_status": "idle",
        },
    ]
    doc["panes"] = [
        {
            "id": "demo-shell-p1",
            "host_id": host_id,
            "workspace_id": "demo-ws",
            "tab_id": DEMO_SHELL_TAB,
            "focused": True,
            "agent_status": "unknown",
        },
        {
            "id": "demo-agent-p1",
            "host_id": host_id,
            "workspace_id": "demo-ws",
            "tab_id": DEMO_AGENT_TAB,
            "focused": False,
            "agent_status": "idle",
        },
    ]
    doc["agents"] = [
        {
            "terminal_id": DEMO_AGENT_ID,
            "host_id": host_id,
            "workspace_id": "demo-ws",
            "tab_id": DEMO_AGENT_TAB,
            "pane_id": "demo-agent-p1",
            "name": "demo-agent",
            "agent": "demo",
            "title": "demo agent",
            "status": "idle",
            "focused": False,
            "launch_pending": False,
            "interactive_ready": True,
            "state_change_seq": 0,
            "revision": 1,
        }
    ]
    return doc


async def _dev_login(request: Request) -> Any:
    """Loopback-only single-use dev login. Enabled only with an explicit
    local-run flag; never in production (no upstream IdP is configured)."""
    store: WebSessionStore = request.app.state.rd_store
    gateway_state: GatewayState = request.app.state.rd_gateway_state
    client = request.client.host if request.client else ""
    if client not in ("127.0.0.1", "::1", "localhost"):
        return JSONResponse(
            status_code=403, content={"code": "forbidden", "message": "loopback only"}
        )
    presented = request.query_params.get("token", "")
    expected = _DEV_LOGIN_TOKEN["value"]
    if not presented or not expected or not hmac.compare_digest(presented, expected):
        return JSONResponse(status_code=403, content={"code": "forbidden", "message": "bad token"})
    _DEV_LOGIN_TOKEN["value"] = ""
    now_ms = int(time.time() * 1000)
    access, refresh = gateway_state.sessions.create(
        "dev",
        frozenset(
            {
                "sessions.read",
                "terminal.view",
                "terminal.control",
                "agents.prompt",
                "agents.start",
                "shells.create",
                "terminal.attach",
            }
        ),
        _DEV_HOSTS["value"],
        now_ms,
    )
    principal = gateway_state.sessions.lookup(access, now_ms)
    assert principal is not None  # noqa: S101
    sid = store.create_session(
        principal.account, principal.device_id, access, refresh, now_ms + 900_000
    )
    return RedirectResponse(url="/", status_code=302, headers=store.cookie_headers(sid))
