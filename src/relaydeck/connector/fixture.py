"""Synthetic Herdr fixture for the connector link.

`FixtureAdapter` satisfies the `journal.DispatchAdapter` protocol with a
scripted in-memory daemon: deterministic panes/agents, no sockets, no
host mutation. The live user daemon is never touched: `connector run`
refuses to start against real Herdr until that path is explicitly wired
and authorized, so every link run today is fixture-backed.

The fixture also renders the metadata the link pushes upstream on
(re)connect: a schema-valid snapshot plus seed events. Re-push is safe:
snapshots replace and the gateway dedupes events by `event_id`.
"""

from __future__ import annotations

from typing import Any

from relaydeck.connector.herdr import EXPECTED_PROTOCOL, HerdrError
from relaydeck.connector.journal import DispatchAdapter
from relaydeck.connector.source import LinkSource

__all__ = [
    "FIXTURE_EPOCH",
    "FIXTURE_WORKSPACE",
    "FixtureAdapter",
    "fixture_event",
    "fixture_snapshot",
]

FIXTURE_EPOCH = "epoch-fixture-1"
FIXTURE_WORKSPACE = "w1"
# synthetic kinds only; the fixture never names a real agent product.
FIXTURE_AGENT_KINDS = ("agent-a", "agent-b", "agent-c")


def fixture_snapshot(
    host_id: str,
    adapter: FixtureAdapter | None = None,
    *,
    epoch: str = FIXTURE_EPOCH,
) -> dict[str, Any]:
    """Schema-valid snapshot for `host_id` from fixture state (or empty)."""
    tabs: list[dict[str, Any]] = []
    panes: list[dict[str, Any]] = []
    agents: list[dict[str, Any]] = []
    state = adapter.state() if adapter is not None else {"tabs": {}, "panes": {}, "agents": {}}
    for tab_id, tab in sorted(state["tabs"].items()):
        tabs.append(
            {
                "id": tab_id,
                "host_id": host_id,
                "workspace_id": str(tab.get("workspace_id", FIXTURE_WORKSPACE)),
                "label": str(tab.get("label", "")),
                "focused": False,
                "pane_count": 1,
                "agent_status": "unknown",
            }
        )
    for pane_id, pane in sorted(state["panes"].items()):
        panes.append(
            {
                "id": pane_id,
                "host_id": host_id,
                "workspace_id": str(pane.get("workspace_id", FIXTURE_WORKSPACE)),
                "tab_id": str(pane.get("tab_id", "")),
                "focused": False,
                "agent_status": "unknown",
            }
        )
    for agent_id, agent in sorted(state["agents"].items()):
        agents.append(
            {
                "terminal_id": str(agent.get("terminal_id", agent_id)),
                "host_id": host_id,
                "workspace_id": str(agent.get("workspace_id", FIXTURE_WORKSPACE)),
                "tab_id": str(agent.get("tab_id", "")),
                "pane_id": str(agent.get("pane_id", "")),
                "name": str(agent.get("name", "")),
                "agent": str(agent.get("kind", "")),
                "title": str(agent.get("name", "")),
                "status": "working",
                "focused": False,
                "interactive_ready": True,
                "state_change_seq": 0,
                "revision": 0,
            }
        )
    return {
        "host": {
            "id": host_id,
            "label": f"{host_id} (fixture)",
            "epoch": epoch,
            "reachable": True,
            "protocol": EXPECTED_PROTOCOL,
            "version": "0.9.1-fixture",
            "agent_catalog": {
                "status": "ready",
                "source": "synthetic",
                "observed_ms": 0,
                "entries": [
                    {"kind": kind, "available": True, "reason": "installed"}
                    for kind in FIXTURE_AGENT_KINDS
                ],
            },
        },
        "cursor": "fixture-cursor-1",
        "workspaces": [
            {
                "id": workspace_id,
                "host_id": host_id,
                "number": 1,
                "label": workspace.get("label", "fixture"),
                "focused": True,
                "pane_count": sum(p["workspace_id"] == workspace_id for p in panes),
                "tab_count": sum(t["workspace_id"] == workspace_id for t in tabs),
                "agent_status": "unknown",
            }
            for workspace_id, workspace in (
                adapter._workspaces.items() if adapter is not None else [(FIXTURE_WORKSPACE, {})]
            )
        ],
        "tabs": tabs,
        "panes": panes,
        "agents": agents,
    }


def fixture_event(
    event_type: str,
    event_id: str,
    *,
    host_epoch: str = FIXTURE_EPOCH,
    resource_id: str = "",
    resource_revision: int = 0,
    observed_at: str = "2026-10-04T00:00:00Z",
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One connector-side event partial. Sequence, cursor, protocol, and
    host binding are server-stamped at ingest; the connector must not
    mint them."""
    doc: dict[str, Any] = {
        "event_id": event_id,
        "host_epoch": host_epoch,
        "type": event_type,
        "observed_at": observed_at,
    }
    if resource_id:
        doc["resource_id"] = resource_id
    if resource_revision:
        doc["resource_revision"] = resource_revision
    if payload is not None:
        doc["payload"] = dict(payload)
    return doc


class FixtureAdapter(LinkSource):
    """Scripted link source: deterministic ids (tab-1, pane-1, ...),
    call log for assertions, typed refusals for unknown targets,
    scripted terminal bytes (drained once: the ring is append-only, so
    re-push would duplicate bytes)."""

    def __init__(self) -> None:
        self._workspaces: dict[str, dict[str, Any]] = {FIXTURE_WORKSPACE: {"label": "fixture"}}
        self._tabs: dict[str, dict[str, Any]] = {}
        self._panes: dict[str, dict[str, Any]] = {}
        self._agents: dict[str, dict[str, Any]] = {}
        self._prompts: list[tuple[str, str]] = []
        self._calls: list[tuple[str, dict[str, Any]]] = []
        self._outputs: list[tuple[str, str, bytes]] = []
        self._ids = 0

    @property
    def dispatch_adapter(self) -> DispatchAdapter:
        return self

    def snapshot_document(self, host_id: str) -> dict[str, Any]:
        return fixture_snapshot(host_id, self)

    def script_output(self, terminal_id: str, epoch: str, data: bytes) -> None:
        self._outputs.append((terminal_id, epoch, data))

    def drain_outputs(self) -> list[tuple[str, str, bytes]]:
        drained, self._outputs = self._outputs, []
        return drained

    @property
    def calls(self) -> tuple[tuple[str, dict[str, Any]], ...]:
        return tuple(self._calls)

    @property
    def prompts(self) -> tuple[tuple[str, str], ...]:
        return tuple(self._prompts)

    def state(self) -> dict[str, dict[str, dict[str, Any]]]:
        return {"tabs": dict(self._tabs), "panes": dict(self._panes), "agents": dict(self._agents)}

    def _next(self, prefix: str) -> str:
        self._ids += 1
        return f"{prefix}-{self._ids}"

    async def agent_prompt(self, target: str, text: str) -> dict[str, Any]:
        self._calls.append(("agent.prompt", {"target": target}))
        agent = self._agents.get(target)
        if agent is None:
            raise HerdrError("no_such_agent", f"unknown agent {target}")
        if not text:
            raise HerdrError("agent_not_ready", "empty prompt refused")
        self._prompts.append((target, text))
        return {"type": "agent_prompted"}

    async def tab_create(self, workspace_id: str, *, label: str | None = None) -> dict[str, Any]:
        self._calls.append(("tab.create", {"workspace_id": workspace_id}))
        if workspace_id not in self._workspaces:
            raise HerdrError("no_such_workspace", f"unknown workspace {workspace_id}")
        tab_id = self._next("tab")
        pane_id = self._next("pane")
        self._tabs[tab_id] = {"workspace_id": workspace_id, "label": label or ""}
        self._panes[pane_id] = {"workspace_id": workspace_id, "tab_id": tab_id}
        return {"tab": {"tab_id": tab_id}, "root_pane": {"pane_id": pane_id}}

    def verify_agent_kind(self, kind: str) -> None:
        if kind not in FIXTURE_AGENT_KINDS:
            raise HerdrError("agent_kind_unavailable", "unknown synthetic agent kind")

    async def workspace_create(
        self, *, cwd: str | None = None, label: str | None = None
    ) -> dict[str, Any]:
        self._calls.append(("workspace.create", {"cwd": cwd, "label": label}))
        workspace_id = self._next("workspace")
        self._workspaces[workspace_id] = {"label": label or "fixture", "cwd": cwd}
        tab_id, pane_id = self._next("tab"), self._next("pane")
        self._tabs[tab_id] = {"workspace_id": workspace_id, "label": ""}
        self._panes[pane_id] = {"workspace_id": workspace_id, "tab_id": tab_id}
        return {
            "type": "workspace_created",
            "workspace": {"workspace_id": workspace_id},
            "tab": {"tab_id": tab_id},
            "root_pane": {"pane_id": pane_id},
        }

    async def tab_rename(self, tab_id: str, label: str) -> dict[str, Any]:
        self._calls.append(("tab.rename", {"tab_id": tab_id, "label": label}))
        if tab_id not in self._tabs:
            raise HerdrError("tab_not_found", "unknown synthetic tab")
        self._tabs[tab_id]["label"] = label
        return {"type": "ok"}

    async def agent_start(
        self,
        name: str,
        kind: str,
        pane_id: str,
        args: list[str] | None = None,
        timeout_ms: int | None = None,
    ) -> dict[str, Any]:
        self._calls.append(("agent.start", {"name": name, "kind": kind, "pane_id": pane_id}))
        if pane_id not in self._panes:
            raise HerdrError("no_such_pane", f"unknown pane {pane_id}")
        if not name or not kind:
            raise HerdrError("agent_not_ready", "name and kind are required")
        agent_id = self._next("agent")
        pane = self._panes[pane_id]
        self._agents[agent_id] = {
            "terminal_id": agent_id,
            "workspace_id": pane["workspace_id"],
            "tab_id": pane["tab_id"],
            "pane_id": pane_id,
            "name": name,
            "kind": kind,
        }
        # Upstream AgentInfo keys identity as terminal_id (schema +
        # agent_prompted golden); there is no "id" key.
        return {"agent": {"terminal_id": agent_id}}

    async def pane_close(self, pane_id: str) -> dict[str, Any]:
        self._calls.append(("pane.close", {"pane_id": pane_id}))
        if pane_id not in self._panes:
            # Upstream code (tests/fixtures/upstream/error_pane_not_found.json).
            raise HerdrError("pane_not_found", f"unknown pane {pane_id}")
        del self._panes[pane_id]
        for agent_id in [k for k, v in self._agents.items() if v["pane_id"] == pane_id]:
            del self._agents[agent_id]
        return {"type": "ok"}

    async def tab_close(self, tab_id: str) -> dict[str, Any]:
        """Close a tab and its panes (and the agents in them). Never a workspace."""
        self._calls.append(("tab.close", {"tab_id": tab_id}))
        if tab_id not in self._tabs:
            raise HerdrError("no_such_tab", f"unknown tab {tab_id}")
        del self._tabs[tab_id]
        pane_ids = {k for k, v in self._panes.items() if v["tab_id"] == tab_id}
        for pane_id in pane_ids:
            del self._panes[pane_id]
        for agent_id in [k for k, v in self._agents.items() if v["pane_id"] in pane_ids]:
            del self._agents[agent_id]
        return {"type": "ok"}
