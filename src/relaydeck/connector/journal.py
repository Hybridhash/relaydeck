"""Connector-side intent journal and Herdr dispatch.

The connector journals every gateway intent BEFORE touching Herdr and
records the outcome when known, so a connector crash between dispatch
and acknowledgement reconciles to unknown instead of blind-retrying.

Durability core is shared with the gateway through the neutral
`relaydeck.journal.CommandJournal` (sqlite3, 0600, WAL + per-commit
fsync, same retention/tombstone policy). Sharing one crash-critical
implementation beats duplicating it; the core is transport-agnostic
and imports no gateway or connector code, so the layering stays one-way.

Tunnel wire (project protocol over the authenticated /v1/connectors
socket; JSON text, each message <= 64 KiB):
  Gateway -> connector:
    command.dispatch {command_id, principal, host_id, host_epoch, gateway_kind,
      target, payload, digest, generation, expires_ms}
      `digest` is the gateway's canonical-body sha256; a mismatch fails
      closed (tamper evidence), never executes.
  Connector -> gateway:
    command.ack    {command_id, host_id}  intent journaled downstream.
    command.result {command_id, host_id, status, result_digest,
      error_code?, result?}               terminal outcome; `status` is
      succeeded | failed | rejected. `result` <= 4 KiB (ids + type only).
    terminal.output {host_id, terminal_id, epoch, data_b64}
      upstream PTY bytes, base64, decoded <= 64 KiB per message.
      Ordering = tunnel arrival order (single live tunnel per host).

Gateway-kind -> Herdr chains (every method below is allowlisted in
herdr.ALLOWED_METHODS and present in herdr-0.9.1-schema.json; params
match the frozen $defs):
  agent.prompt -> agent.prompt(target=agent_id, text)
      This is the `prompt_text` seam, connector side.
  agent.start  -> tab.create(workspace_id, label=name) then
                  agent.start(name, kind, pane_id=<root_pane>, args=argv).
      agent.start REQUIRES pane_id upstream, which only exists after a
      tab (Schema: AgentStartParams.required=[name,kind,pane_id];
      tab_created returns the root_pane). Partial identities are
      journaled via note_progress between the steps so reconcile reuses
      them instead of duplicating creation.
  shell.create -> tab.create(workspace_id, label?).
  agent.rename -> tab.rename(tab_id, label=display name) then, when the label has a usable
      slug, agent.rename(target=agent_id, name=slug).
  session.close -> pane.close(pane_id=terminal_id). Upstream has no
      agent.close (verified against live_methods.txt); closing the pane
      ends whatever it hosts, matching attach.py's explicit-close rule.
Unknown gateway kinds are rejected, never mapped by guess.

Error handling: typed HerdrError (agent_blocked, agent_not_ready, ...)
proves non-delivery-or-refusal and records failed + upstream error_code.
Transport/envelope/timeout failures after the write-ahead dispatched
marker record unknown (timeout after dispatch is unknown, not
failed) — including connect failures, which the adapter cannot prove
pre-delivery.

Follow-on protocol (see connector/link.py): the tunnel link also
pushes `snapshot` (schema-validated, host-bound, replaces) and `event`
(server-stamped sequence/cursor, `event_id` dedupe) upstream, ingested
by the gateway backends. Typed input.forward/resize.forward is generation-bound,
never replayed after an uncertain write; large snapshots use bounded, hashed
chunks. Before executing a new prompt, dispatch checks the source epoch and
fetches the agent revision through the existing agent.list API. This is a
preflight check, not an invented atomic conditional upstream RPC.
"""

from __future__ import annotations

import contextlib
import hashlib
from asyncio import sleep
from collections.abc import Callable
from time import monotonic
from typing import Any, Protocol

from relaydeck.connector.herdr import HerdrError, HerdrProtocolError
from relaydeck.contracts.canonical import canonical_json
from relaydeck.contracts.launch import display_name_clean
from relaydeck.journal import CommandJournal, JournalConflict, digest_for

DISPATCH_KIND = "command.dispatch"
ACK_KIND = "command.ack"
RESULT_KIND = "command.result"
OUTPUT_KIND = "terminal.output"

GATEWAY_KINDS = ("agent.prompt", "agent.start", "agent.rename", "shell.create", "session.close")
RESULT_STATUSES = ("succeeded", "failed", "rejected")
AGENT_PROMPT_BUDGET_S = 15.0


class DispatchAdapter(Protocol):
    """HerdrAdapter surface used by dispatch (scripted fakes satisfy it)."""

    async def agent_prompt(self, target: str, text: str) -> dict[str, Any]: ...
    async def agent_list(self) -> list[dict[str, Any]]: ...
    async def agent_rename(self, target: str, name: str | None) -> dict[str, Any]: ...
    async def tab_create(self, workspace_id: str, *, label: str | None = ...) -> dict[str, Any]: ...
    async def workspace_create(
        self, *, cwd: str | None = ..., label: str | None = ...
    ) -> dict[str, Any]: ...
    async def tab_rename(self, tab_id: str, label: str) -> dict[str, Any]: ...
    async def agent_start(
        self,
        name: str,
        kind: str,
        pane_id: str,
        args: list[str] | None = ...,
        timeout_ms: int | None = ...,
    ) -> dict[str, Any]: ...
    async def pane_close(self, pane_id: str) -> dict[str, Any]: ...
    async def tab_close(self, tab_id: str) -> dict[str, Any]: ...


def result_digest_for(result: dict[str, Any]) -> str:
    raw = canonical_json(result)
    return hashlib.sha256(raw.encode()).hexdigest()


def _narrow(value: Any, limit: int = 128) -> str:
    text = value if isinstance(value, str) else ""
    return text[:limit]


class ConnectorDispatch:
    """Executes journaled gateway intents against Herdr exactly once per
    UUID: same ID + same body returns the recorded outcome without
    touching Herdr again; same ID + changed body is rejected as a
    conflict (never executed under a reused UUID). Within that one intent,
    only the definite pre-launch agent_pane_busy refusal may retry."""

    def __init__(
        self,
        journal: CommandJournal,
        adapter: DispatchAdapter,
        *,
        host_id: str,
        clock: Callable[[], int] | None = None,
        epoch_probe: Callable[[], str] | None = None,
    ) -> None:
        self._journal = journal
        self._adapter = adapter
        self._host_id = host_id
        self._clock = clock
        self._epoch_probe = epoch_probe
        self.herdr_calls = 0

    def _now(self) -> int:
        if self._clock is not None:
            return self._clock()
        return self._journal.now()

    async def dispatch(self, message: dict[str, Any]) -> dict[str, Any]:
        """Execute one command.dispatch message; returns the connector's
        outbound command.result document (ack is implied: callers send
        command.ack once the intent is journaled)."""
        now = self._now()
        command_id = _narrow(message.get("command_id"))
        kind = _narrow(message.get("gateway_kind"))
        target = message.get("target")
        payload = message.get("payload")
        if (
            not command_id
            or not isinstance(target, dict)
            or not isinstance(payload, dict)
            or message.get("host_id") != self._host_id
        ):
            raise ValueError("malformed dispatch")
        # Fail closed on digest mismatch (tamper evidence).
        if message.get("digest") != digest_for(self._host_id, kind, target, payload):
            raise ValueError("dispatch digest mismatch")
        generation = message.get("generation", 0)
        if type(generation) is not int or generation < 0:
            raise ValueError("invalid revision assertion")
        try:
            await self._journal.run(
                self._journal.submit,
                command_id=command_id,
                principal=_narrow(message.get("principal")),
                host_id=self._host_id,
                host_epoch=str(message.get("host_epoch", "")),
                kind=kind,
                target={k: _narrow(v) for k, v in target.items()},
                payload=payload,
                generation=generation,
                expires_ms=int(message.get("expires_ms") or 0),
                now=now,
            )
        except JournalConflict:
            return {
                "kind": RESULT_KIND,
                "command_id": command_id,
                "host_id": self._host_id,
                "status": "rejected",
                "result_digest": "",
                "error_code": "command_conflict",
            }
        current = await self._journal.run(self._journal.record, command_id)
        assert current is not None
        if current["status"] != "accepted":
            # Redelivered dispatch for a settled intent: recorded outcome,
            # zero new Herdr calls (never dispatches twice).
            return self._result_doc(current)
        if kind not in GATEWAY_KINDS:
            await self._journal.run(
                self._journal.mark_result,
                command_id,
                "rejected",
                error_code="unsupported_kind",
                now=now,
            )
            current = await self._journal.run(self._journal.record, command_id)
            assert current is not None
            return self._result_doc(current)
        # Write-ahead: dispatched BEFORE the first Herdr byte, so a crash
        # here reconciles to unknown, never to a blind retry. A refused
        # marker (redelivery race already dispatched it, or TTL lapsed
        # mid-dispatch) returns the recorded outcome: never execute twice.
        if not await self._journal.run(
            lambda: self._journal.mark_dispatched(command_id, now=self._now())
        ):
            current = await self._journal.run(self._journal.record, command_id)
            assert current is not None
            return self._result_doc(current)
        try:
            if self._epoch_probe is not None and current["host_epoch"] != self._epoch_probe():
                raise HerdrError("stale_epoch", "host changed before command dispatch")
            outcome = await self._execute(command_id, kind, target, payload)
        except HerdrProtocolError as exc:
            await self._journal.run(self._journal.mark_unknown, command_id, now=self._now())
            return self._unknown_doc(command_id, exc.code)
        except HerdrError as exc:
            pending = await self._journal.run(self._journal.record, command_id)
            partial = pending.get("progress", {}) if pending is not None else {}
            result = {"type": "partial_creation", **partial} if partial else None
            await self._journal.run(
                self._journal.mark_result,
                command_id,
                "failed",
                error_code=exc.code,
                now=self._now(),
                result=result,
                result_digest=result_digest_for(result) if result else "",
            )
            current = await self._journal.run(self._journal.record, command_id)
            assert current is not None
            return self._result_doc(current)
        except (TimeoutError, OSError) as exc:
            await self._journal.run(self._journal.mark_unknown, command_id, now=self._now())
            return self._unknown_doc(command_id, type(exc).__name__)
        digest = result_digest_for(outcome)
        await self._journal.run(
            self._journal.mark_result,
            command_id,
            "succeeded",
            result_digest=digest,
            result=outcome,
            now=self._now(),
        )
        current = await self._journal.run(self._journal.record, command_id)
        assert current is not None
        return self._result_doc(current)

    async def _execute(
        self,
        command_id: str,
        kind: str,
        target: dict[str, Any],
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        if kind == "agent.prompt":
            current = await self._journal.run(self._journal.record, command_id)
            expected = current["generation"] if current else 0
            if expected:
                probe = getattr(self._adapter, "agent_revision", None)
                revision = await probe(_narrow(target.get("agent_id"))) if callable(probe) else None
                if type(revision) is not int or revision != expected:
                    raise HerdrError("stale_revision", "agent changed before prompt dispatch")
            self.herdr_calls += 1
            out = await self._adapter.agent_prompt(
                _narrow(target.get("agent_id")), str(payload.get("text", ""))
            )
            return {"type": str(out.get("type", "agent_prompted"))}
        if kind == "agent.rename":
            agent_id = _narrow(target.get("agent_id"))
            self.herdr_calls += 1
            agents = await self._adapter.agent_list()
            if not all(isinstance(row, dict) for row in agents):
                raise HerdrProtocolError("bad_agent_result", "invalid agent list")
            matches = [row for row in agents if row.get("terminal_id") == agent_id]
            if not matches:
                raise HerdrError("agent_not_found", "agent is no longer listed")
            if len(matches) != 1:
                raise HerdrProtocolError("bad_agent_result", "ambiguous agent identity")
            agent = matches[0]
            rename_tab = agent.get("tab_id")
            if not isinstance(rename_tab, str) or not 1 <= len(rename_tab) <= 128:
                raise HerdrProtocolError("bad_agent_result", "agent tab unavailable")
            tab_label = display_name_clean(str(payload["name"]))
            self.herdr_calls += 1
            await self._adapter.tab_rename(rename_tab, tab_label)
            await self._journal.run(
                self._journal.note_progress, command_id, {"tab_id": rename_tab}, now=self._now()
            )
            if agent.get("name"):
                await self._clear_agent_name(agent_id)
            return {"type": "ok"}
        created_workspace = ""
        new_workspace = target == {"workspace_mode": "new"}
        if new_workspace and kind not in ("shell.create", "agent.start"):
            raise HerdrError("unsupported_kind", "unsupported workspace operation")
        if kind == "agent.start":
            check = getattr(self._adapter, "verify_agent_kind", None)
            if callable(check):
                check(str(payload.get("kind", "")))
        if new_workspace:
            from pathlib import Path

            from relaydeck.contracts.launch import valid_new_workspace

            spec = payload.get("new_workspace")
            if not valid_new_workspace(spec):
                raise HerdrError("invalid_workspace", "invalid workspace destination")
            self.herdr_calls += 1
            created = await self._adapter.workspace_create(
                cwd=spec.get("directory", "").strip() or str(Path.home()),
                label=spec.get("label", "").strip() or None,
            )
            workspace = created.get("workspace", {})
            created_workspace = _narrow(workspace.get("workspace_id"))
            tab_id = _narrow(created.get("tab", {}).get("tab_id"))
            pane_id = _narrow(created.get("root_pane", {}).get("pane_id"))
            if not created_workspace or not tab_id or not pane_id:
                raise HerdrProtocolError(
                    "bad_workspace_result", "workspace creation outcome unavailable"
                )
            await self._journal.run(
                self._journal.note_progress,
                command_id,
                {"workspace_id": created_workspace, "tab_id": tab_id, "pane_id": pane_id},
                now=self._now(),
            )
        if kind == "shell.create":
            label = payload.get("label") or None
            if new_workspace:
                out = created
                if label:
                    self.herdr_calls += 1
                    await self._adapter.tab_rename(tab_id, str(label))
            else:
                self.herdr_calls += 1
                out = await self._adapter.tab_create(
                    _narrow(target.get("workspace_id")),
                    label=str(label) if label else None,
                )
            tab = out.get("tab", {})
            pane = out.get("root_pane", {})
            return {
                "type": "tab_created",
                "tab_id": _narrow(tab.get("tab_id") if isinstance(tab, dict) else ""),
                "pane_id": _narrow(pane.get("pane_id") if isinstance(pane, dict) else ""),
                "workspace_id": created_workspace or _narrow(target.get("workspace_id")),
            }
        if kind == "agent.start":
            if not new_workspace:
                self.herdr_calls += 1
                created = await self._adapter.tab_create(
                    _narrow(target.get("workspace_id")), label=str(payload.get("name", ""))
                )
            else:
                self.herdr_calls += 1
                await self._adapter.tab_rename(tab_id, str(payload.get("name", "")))
            tab = created.get("tab", {})
            pane = created.get("root_pane", {})
            tab_id = _narrow(tab.get("tab_id") if isinstance(tab, dict) else "")
            pane_id = _narrow(pane.get("pane_id") if isinstance(pane, dict) else "")
            # Preserve partial identities for explicit reconciliation;
            # a crash settles unknown and never repeats creation automatically.
            await self._journal.run(
                self._journal.note_progress,
                command_id,
                {
                    "workspace_id": created_workspace or _narrow(target.get("workspace_id")),
                    "tab_id": tab_id,
                    "pane_id": pane_id,
                },
                now=self._now(),
            )
            argv = payload.get("argv", [])
            started = await self._start_when_prompt_ready(
                command_id, payload, pane_id, list(argv) if isinstance(argv, list) else None
            )
            agent = started.get("agent", {})
            # Upstream AgentInfo keys identity as terminal_id ("id" is
            # kept as a fallback for older scripted adapters).
            identity = ""
            if isinstance(agent, dict):
                identity = str(agent.get("terminal_id") or agent.get("id") or "")
            await self._clear_agent_name(identity or pane_id)
            return {
                "type": "agent_started",
                "tab_id": tab_id,
                "pane_id": pane_id,
                "agent_id": _narrow(identity),
                "workspace_id": created_workspace or _narrow(target.get("workspace_id")),
            }
        # session.close: a RelayDeck shell's terminal id is a Herdr TAB id,
        # so pane.close is tried first and, only on upstream pane_not_found
        # (tests/fixtures/upstream/error_pane_not_found.json), tab.close runs once.
        # Workspaces are never closed here.
        terminal_id = _narrow(target.get("terminal_id"))
        self.herdr_calls += 1
        try:
            out = await self._adapter.pane_close(terminal_id)
        except HerdrError as exc:
            if exc.code != "pane_not_found":
                raise
            self.herdr_calls += 1
            out = await self._adapter.tab_close(terminal_id)
        return {"type": str(out.get("type", "ok"))}

    async def _clear_agent_name(self, target: str) -> None:
        """Herdr shows a set agent name in place of the CLI kind, so RelayDeck names only tabs."""
        self.herdr_calls += 1
        with contextlib.suppress(HerdrError, OSError, TimeoutError):
            await self._adapter.agent_rename(target, None)

    async def _start_when_prompt_ready(
        self, command_id: str, payload: dict[str, Any], pane_id: str, args: list[str] | None
    ) -> dict[str, Any]:
        """Let the newly journaled pane finish shell initialization.

        Upstream agent_pane_busy rejects before launch, so this is not a
        replay of uncertain input. No new tab/workspace is created here.
        The readiness backoff has a 15s budget; an actual launch retains
        the adapter's existing startup/readiness timeout. All other
        refusals, transport failures and cancellation keep today's path.
        """
        deadline, delay = monotonic() + AGENT_PROMPT_BUDGET_S, 0.1
        while True:
            self.herdr_calls += 1
            try:
                return await self._adapter.agent_start(
                    str(payload.get("name", "")), str(payload.get("kind", "")), pane_id, args=args
                )
            except HerdrProtocolError:
                raise  # A malformed reply is never evidence of safe non-delivery.
            except HerdrError as exc:
                if exc.code != "agent_pane_busy":
                    raise
                remaining = deadline - monotonic()
                current = await self._journal.run(self._journal.record, command_id)
                expires = current["expires_ms"] if current is not None else 0
                if expires:
                    remaining = min(remaining, (expires - self._now()) / 1000)
                if remaining <= 0:
                    raise
                await sleep(min(delay, remaining))
                # Fence every retry after the await, including TTL and host
                # replacement. A cancelled intent is never resumed on redelivery.
                if monotonic() >= deadline or (expires and self._now() >= expires):
                    raise
                current = await self._journal.run(self._journal.record, command_id)
                if current is None or current["status"] != "dispatched":
                    raise HerdrProtocolError(
                        "command_conflict", "creation intent changed"
                    ) from None
                if self._epoch_probe is not None and current["host_epoch"] != self._epoch_probe():
                    raise HerdrError(
                        "stale_epoch", "host changed while awaiting shell prompt"
                    ) from None
                delay = min(delay * 2, 1.0)

    def _result_doc(self, current: dict[str, Any]) -> dict[str, Any]:
        status = current["status"]
        if status not in RESULT_STATUSES:
            return self._unknown_doc(str(current["command_id"]), status)
        doc: dict[str, Any] = {
            "kind": RESULT_KIND,
            "command_id": current["command_id"],
            "host_id": self._host_id,
            "status": status,
            "result_digest": current["result_digest"],
        }
        if current["error_code"]:
            doc["error_code"] = current["error_code"]
        if current["result"]:
            doc["result"] = current["result"]
        return doc

    def _unknown_doc(self, command_id: str, detail: str) -> dict[str, Any]:
        # A command.result envelope carrying non-terminal unknown: the
        # connector proved nothing about execution, so the gateway must
        # record unknown (reconcile only), never a terminal outcome.
        return {
            "kind": RESULT_KIND,
            "command_id": command_id,
            "host_id": self._host_id,
            "status": "unknown",
            "result_digest": "",
            "error_code": _narrow(detail, 64),
        }

    def reconcile(self, command_id: str) -> dict[str, Any] | None:
        """Explicit reconcile query: full record incl. partial progress.
        Unknown outcomes are resolved here, never by resubmission."""
        return self._journal.record(command_id)
