"""Agent composer domain. Flet-free.

Owns the local multiline draft plus staged placeholder markers, validates
Send, and delivers through exactly one `agent.prompt` command — except on
the blocked path, where the draft UTF-8 goes straight into the live PTY.

Behaviour contract:
- `send` / `deliver`: one prompt per send; blocked status skips the RPC and inserts into the
  Attach PTY.
- `deliverThroughAttach`: safe-scalars gate, weak-writer gate
  (dead writer fails, never retained), `tracksAgentProgress = false`,
  `.delivered(.acknowledged)`; `SendResult.deliveredViaAttach` cues the
  tools keyboard.
- `insertComposerDraft`: raw
  `Data(text.utf8)`, no Enter, no bracketed framing.
- `promptWaitingOutLaunch` + defaults: `agent_not_ready`
  alone retries at a fixed 500 ms pace inside a 10 s budget, matching the
  transport shell-readiness wait; the pane id is
  correct and only time is missing, so the retry is proven pre-delivery.
  `agent_not_found` still fails immediately.
- `agentStatusDidChange` / `progressAfterAcknowledgment`:
  status-revision tracking, observed-working flag,
  acknowledged / agentBusy / working / done progress.
- `message(for:)`: `herdr rejected the message: <detail>`.
- `canSend`: non-whitespace draft, no pending staged content.
- `containsDropPlaceholder` (L230+): unresolved staged tokens block Send.

Deliberate choices:
- Multiline blocked insertion requires explicit review-or-confirm:
  embedded LF/CR can execute though no Enter is added, so it is never
  inserted immediately. Counts/preview reuse `paste_policy` helpers; the paste
  framing session is never used here.
- Timeout after dispatch is an explicit `unknown` outcome: no
  blind retry, draft and error preserved. Retryable transport failures and
  unexpected errors without a typed rejection code are `unknown` too: only
  a typed refusal (gateway `forbidden`, upstream `agent_not_found`, ...)
  proves non-delivery. A timeout is "unknown" rather than a failure
  because a retry could double delivery.
- The draft is cleared only by successful delivery (and only when unchanged
  since Send); recoverable failures keep the draft byte-identical in place.
  There is no withdraw because the draft never leaves.
- There is no automatic or manual `retry`: a failed/unknown send recovers
  by editing the preserved draft and pressing Send again (a new command
  id). Same-ID resubmission dedup belongs to the gateway journal.
- Placeholder shape: the source blocks Send on any drop-placeholder-shaped
  token in the draft; this port blocks Send only on tokens staged via
  `stage_marker` (unknown tokens are plain text).

Production observes the command ID returned by the gateway; observation never
submits a second prompt. The direct prompt callback/retry loop is retained only
for explicitly injected source tests. A matching late terminal input failure
marks a blocked insertion unknown and restores its draft without replay.

Submission `sending` covers send in progress including awaiting
blocked-review confirm; stacked sends are already gated by the pending
check, so no separate review state exists.

STAGED-MARKER BOUNDARY: markers are local
placeholder tokens with resolved/unresolved state only. No attachment,
file, snippet, or skill backend exists here; `resolve_marker` records a
local text replacement performed by the staging owner.

Writer binding: the blocked path probes the sole current writer through
`generation_probe` (None = no live writer) and sends UTF-8 chunks through
`sender`. Production binds `TerminalController.paste_generation` /
`send_paste_chunk` via `bind_blocked_writer`; no other controller method
is touched. A missing or dead writer is an explicit failure with no
hidden fallback. Handoff: blocked delivery sets `input_mode` to `"tools-keys"`
(state only; the keys UI must honor this value on delivery).
Normal delivery leaves the mode untouched; this store never switches back.
The screen resets input_mode to composer when the user leaves tools-keys
mode; the store never resets it. Mount contract: `ComposerBox` (components.py) is fully controlled
— immutable props plus callbacks, no sockets, no timers. The screen owns
one `ComposerStore`, passes `store.view()`-shaped props, and routes events
to the store methods.

Approval-looking strings: terminal output and notification text are never
send authority. `agent_status_did_change` accepts an inert `label` (which
may contain approval-looking text) that cannot trigger any response; the
blocked path inserts only the user's reviewed draft bytes.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from relaydeck.client.features.composer.commands import agent_prompt_command
from relaydeck.client.features.composer.pictures import Picture
from relaydeck.client.features.terminal.paste_policy import (
    PasteReview,
    character_count,
    chunk_payload,
    contains_only_safe_scalars,
    is_multiline,
    line_count,
    preview,
)
from relaydeck.domain.models import AgentStatus

if TYPE_CHECKING:
    from relaydeck.client.features.terminal.controller import TerminalController

# agent_not_ready retry window, matching the shell-readiness wait. Tests
# inject smaller values.
NOT_READY_DELAY_S = 0.5
NOT_READY_BUDGET_S = 10.0

INPUT_MODE_COMPOSER = "composer"
INPUT_MODE_TOOLS_KEYS = "tools-keys"

SEND_IGNORED = "ignored"
SEND_DELIVERED_VIA_PROMPT = "delivered-via-prompt"
SEND_DELIVERED_VIA_ATTACH = "delivered-via-attach"
SEND_FAILED = "failed"
SEND_UNKNOWN = "unknown"
SEND_BLOCKED_REVIEW = "blocked-review"

SUBMISSION_IDLE = "idle"
SUBMISSION_SENDING = "sending"
SUBMISSION_ACKED = "acked"
SUBMISSION_PROGRESS = "progress"
SUBMISSION_BLOCKED_INSERTED = "blocked-inserted"
SUBMISSION_UNKNOWN = "unknown"
SUBMISSION_FAILED = "failed"

DELIVERY_SENDING = "sending"
DELIVERY_DELIVERED = "delivered"
DELIVERY_FAILED = "failed"
DELIVERY_UNKNOWN = "unknown"

PROGRESS_ACKNOWLEDGED = "acknowledged"
PROGRESS_AGENT_BUSY = "agent_busy"
PROGRESS_WORKING = "working"
PROGRESS_DONE = "done"

BLOCKER_EMPTY = "empty"

MISSING_WRITER_MESSAGE = "The message could not be sent. Check the connection and retry."
UNSAFE_TEXT_MESSAGE = "The message contains unsafe terminal control characters."
STALE_BLOCKED_MESSAGE = (
    "Terminal session changed; review the blocked draft again. Nothing was sent."
)
CANCELLED_BLOCKED_MESSAGE = "Blocked insertion cancelled. Nothing was sent."
NO_BLOCKED_MESSAGE = "No blocked draft awaiting review."
UNKNOWN_DELIVERY_MESSAGE = (
    "Delivery is uncertain: the prompt may or may not have been sent. It was not retried."
)
LOST_DELIVERY_MESSAGE = (
    "The connection changed during send, so delivery is unknown. The draft was not resent."
)
REVIEW_LOST_MESSAGE = "The connection changed during review. Nothing was sent; send again."
GATEWAY_REFUSED_PREFIX = "The gateway refused the prompt"
HERDR_REJECTED_PREFIX = "herdr rejected the message"
INSERT_FAILED_PREFIX = "Blocked insertion failed"


@dataclass(frozen=True)
class StagedMarker:
    """A local placeholder token (MARKERS ONLY — see module docstring).

    `resolved` flips when the future staging owner replaces the token;
    unresolved tokens present in the draft block Send.
    """

    token: str
    resolved: bool = False


@dataclass
class DeliveryState:
    kind: str = DELIVERY_SENDING
    progress: str = ""
    message: str = ""


@dataclass
class ComposerMessage:
    id: str
    text: str
    agent_was_working_at_send: bool
    status_revision_at_send: int
    observed_working_after_send: bool = False
    tracks_agent_progress: bool = True
    state: DeliveryState = field(default_factory=DeliveryState)


@dataclass(frozen=True)
class PendingBlocked:
    """Multiline blocked draft awaiting explicit review .

    `generation` binds the live writer at review open; confirm revalidates
    it and inserts the exact reviewed bytes with no Enter and no framing.
    """

    message_id: str
    text: str
    generation: int
    review: PasteReview


def blocked_insert_bytes(text: str) -> bytes:
    """Golden blocked bytes: draft UTF-8 verbatim — no Enter appended, no
    bracketed framing."""
    return text.encode("utf-8")


def _is_ambiguous(exc: Exception) -> bool:
    """Timeouts and retryable transport failures leave delivery ambiguous:
    the command may have been recorded or dispatched already."""
    if isinstance(exc, TimeoutError | asyncio.TimeoutError):
        return True
    return bool(getattr(exc, "retryable", False))


def _is_typed_rejection(exc: Exception) -> bool:
    """Typed upstream/gateway refusals carry a string `code` and prove
    non-delivery (the refusal happened instead of the effect). Timeouts,
    retryable transport failures, and unexpected errors prove nothing
    about delivery and therefore settle as `unknown`."""
    return isinstance(getattr(exc, "code", None), str)


def _exc_code(exc: Exception) -> str:
    return str(getattr(exc, "code", type(exc).__name__))


def _exc_message(exc: Exception) -> str:
    message = getattr(exc, "message", None)
    if isinstance(message, str) and message:
        return message
    return str(exc) or type(exc).__name__


class ComposerStore:
    """Local draft + one-shot delivery state machine (Flet-free).

    `submit_command` records one envelope. Production observes that
    command ID through account/host-bound status queries; it never invokes
    the source prompt callback again. Scripted source tests may inject a
    direct callback. Typed refusal keeps the draft; an uncertain outcome
    remains unknown and cannot trigger a retry.
    """

    def __init__(
        self,
        *,
        target: str,
        submit_command: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]],
        prompt_text: Callable[[str, str], Awaitable[dict[str, Any]]] | None,
        host_id: str,
        host_epoch: str,
        agent_id: str,
        command_id_factory: Callable[[], str] | None = None,
        initial_status: str = AgentStatus.IDLE.value,
        not_ready_delay_s: float = NOT_READY_DELAY_S,
        not_ready_budget_s: float = NOT_READY_BUDGET_S,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        monotonic: Callable[[], float] | None = None,
        generation_probe: Callable[[], int | None] | None = None,
        sender: Callable[[bytes], Awaitable[None]] | None = None,
        observe_command: Callable[[str, str], Awaitable[dict[str, Any]]] | None = None,
        expected_revision: int = 0,
    ) -> None:
        self._target = target
        self._submit_command = submit_command
        self._prompt_text = prompt_text
        self._observe_command = observe_command
        self._expected_revision = expected_revision
        self._host_id = host_id
        self._host_epoch = host_epoch
        self._agent_id = agent_id
        self._command_id_factory = command_id_factory or (lambda: uuid.uuid4().hex)
        self._agent_status = AgentStatus.coerce(initial_status)
        self._status_revision = 0
        self._not_ready_delay = not_ready_delay_s
        self._not_ready_budget = not_ready_budget_s
        self._sleep = sleep or asyncio.sleep
        self._monotonic = monotonic or time.monotonic
        self._probe = generation_probe
        self._sender = sender
        self._draft = ""
        self._pictures: tuple[Picture, ...] = ()
        self._markers: tuple[StagedMarker, ...] = ()
        self._messages: list[ComposerMessage] = []
        self._submission = SUBMISSION_IDLE
        self._error: str | None = None
        self._input_mode = INPUT_MODE_COMPOSER
        self._pending_blocked: PendingBlocked | None = None
        self._last_command_id: str | None = None
        self._message_seq = 0
        self._send_generation = 0
        self._blocked_frames: dict[tuple[int, int], tuple[ComposerMessage, int]] = {}
        self._blocked_binding: object | None = None

    # -- draft -----------------------------------------------------------
    @property
    def draft(self) -> str:
        return self._draft

    @property
    def markers(self) -> tuple[StagedMarker, ...]:
        return self._markers

    @property
    def messages(self) -> tuple[ComposerMessage, ...]:
        return tuple(self._messages)

    @property
    def submission(self) -> str:
        return self._submission

    @property
    def error(self) -> str | None:
        return self._error

    @property
    def input_mode(self) -> str:
        return self._input_mode

    @property
    def pending_blocked(self) -> PendingBlocked | None:
        return self._pending_blocked

    @property
    def last_command_id(self) -> str | None:
        return self._last_command_id

    @property
    def agent_status(self) -> str:
        return self._agent_status.value

    def replace_draft(self, text: str) -> None:
        self._draft = text
        self._error = None

    @property
    def pictures(self) -> tuple[Picture, ...]:
        return self._pictures

    def add_picture(self, picture: Picture) -> None:
        if len(self._pictures) < 8 and picture.picture_id not in {
            p.picture_id for p in self._pictures
        }:
            self._pictures += (picture,)

    def remove_picture(self, identifier: str) -> None:
        self._pictures = tuple(p for p in self._pictures if p.picture_id != identifier)

    def submission_text(self) -> str:
        if not self._pictures:
            return self._draft
        lines = ([self._draft] if self._draft else []) + [p.prompt_line() for p in self._pictures]
        return "\n".join(lines)

    def _clear_sent(self, text: str) -> None:
        if self.submission_text() == text:
            self._draft, self._pictures = "", ()

    def export_draft(self) -> dict[str, Any]:
        """Snapshot the draft + staged markers for pause/death persistence
        (additive). The caller stores the snapshot per the
        lifecycle storage policy (secure storage: drafts may contain
        commands, paths, and pasted secrets). Shape: `{"draft": str,
        "markers": [{"token": str, "resolved": bool}]}`."""
        return {
            "draft": self._draft,
            **({"pictures": [p.document() for p in self._pictures]} if self._pictures else {}),
            "markers": [
                {"token": marker.token, "resolved": marker.resolved} for marker in self._markers
            ],
        }

    def import_draft(self, doc: dict[str, Any]) -> None:
        """Restore a snapshot from `export_draft` (additive).

        Explicitly overwrites the current draft and markers: the resume
        flow calls this only for the route it restores. Raises ValueError
        on a bad shape; nothing is half-applied."""
        draft = doc.get("draft", "")
        raw_pictures = doc.get("pictures", [])
        if not isinstance(raw_pictures, list) or len(raw_pictures) > 8:
            raise ValueError("bad draft snapshot shape")
        pictures = tuple(Picture.from_wire(p) for p in raw_pictures)
        markers = doc.get("markers", [])
        if not isinstance(draft, str) or not isinstance(markers, list):
            raise ValueError("bad draft snapshot shape")
        rebuilt: list[StagedMarker] = []
        for entry in markers:
            if (
                not isinstance(entry, dict)
                or "token" not in entry
                or "resolved" not in entry
                or not isinstance(entry["token"], str)
                or not isinstance(entry["resolved"], bool)
            ):
                raise ValueError("bad draft snapshot shape")
            rebuilt.append(
                StagedMarker(token=str(entry["token"]), resolved=bool(entry["resolved"]))
            )
        self._draft = draft
        self._pictures = pictures
        self._markers = tuple(rebuilt)
        self._error = None

    def insert_into_draft(self, text: str) -> None:
        """Append text to the draft (the snippet/staged-path insertion
        point; own the backends). Nothing is submitted."""
        self._draft += text
        self._error = None

    def clear_error(self) -> None:
        self._error = None

    def leave_tools_keys(self) -> bool:
        """Reset rule: the user left the tools-keys surface, so
        restore composer mode. Returns True when a reset happened. This is
        the ONLY path that resets input_mode: no send, status, progress, or
        reconnect path touches it (verified by tests/ui/test_input_modes.py).
        Entering tools never calls this; see `InputModeState`."""
        if self._input_mode == INPUT_MODE_TOOLS_KEYS:
            self._input_mode = INPUT_MODE_COMPOSER
            return True
        return False

    # -- staged markers (local tokens only) -------------------------------
    def stage_marker(self, token: str) -> StagedMarker:
        marker = StagedMarker(token=token)
        self._markers += (marker,)
        return marker

    def resolve_marker(self, token: str, replacement: str) -> bool:
        """Record a local token replacement. Returns False when the token
        is unknown or already resolved. The first exact occurrence is
        replaced and later duplicates deleted; the attachment/file/snippet
        backends that will call this do not exist yet."""
        for marker in self._markers:
            if marker.token == token and not marker.resolved:
                break
        else:
            return False
        self._markers = tuple(
            StagedMarker(token=m.token, resolved=True) if m.token == token else m
            for m in self._markers
        )
        if token and token in self._draft:
            # First exact occurrence replaced, later duplicates deleted.
            head, _, tail = self._draft.partition(token)
            self._draft = head + replacement + tail.replace(token, "")
        self._error = None
        return True

    def unstage_marker(self, token: str) -> bool:
        before = len(self._markers)
        self._markers = tuple(m for m in self._markers if m.token != token)
        return len(self._markers) != before

    def unresolved_tokens(self) -> tuple[str, ...]:
        return tuple(m.token for m in self._markers if not m.resolved and m.token in self._draft)

    # -- validation --------------------------------------------------------
    @property
    def can_send(self) -> bool:
        """Non-whitespace draft with no unresolved staged tokens present
        ."""
        if any(p.expires_ms <= int(time.time() * 1000) for p in self._pictures):
            return False
        if not self._pictures and not any(not char.isspace() for char in self._draft):
            return False
        return not self.unresolved_tokens()

    def send_blockers(self) -> tuple[str, ...]:
        blockers: list[str] = []
        if any(p.expires_ms <= int(time.time() * 1000) for p in self._pictures):
            blockers.append("picture-expired: remove it and attach again")
        if not self._pictures and not any(not char.isspace() for char in self._draft):
            blockers.append(BLOCKER_EMPTY)
        blockers.extend(f"unresolved:{token}" for token in self.unresolved_tokens())
        return tuple(blockers)

    # -- writer binding ------------------------------------------------------
    def update_command_context(self, host_epoch: str, revision: int) -> None:
        """Fresh metadata governs the next submission, never an in-flight retry."""
        self._host_epoch = host_epoch
        self._expected_revision = revision

    def bind_writer(
        self,
        generation_probe: Callable[[], int | None],
        sender: Callable[[bytes], Awaitable[None]],
    ) -> None:
        self._probe = generation_probe
        self._sender = sender

    def unbind_writer(self) -> None:
        """Drop the live writer (disconnect/teardown). A dead writer must
        fail the blocked path rather than retain a stale session."""
        self._probe = None
        self._sender = None

    def _live_generation(self) -> int | None:
        if self._probe is None or self._sender is None:
            return None
        return self._probe()

    # -- send -----------------------------------------------------------------
    async def send(self) -> str:
        """Deliver the draft: exactly one `agent.prompt` command, or the
        blocked path when the agent is blocked (or the prompt races with
        `agent_blocked`). Validation failures and concurrent sends are
        ignored without touching transport."""
        if self._submission == SUBMISSION_SENDING or self._pending_blocked is not None:
            return SEND_IGNORED
        if not self.can_send:
            return SEND_IGNORED
        text = self.submission_text()
        self._message_seq += 1
        self._send_generation += 1
        message = ComposerMessage(
            id=f"m-{self._message_seq}",
            text=text,
            agent_was_working_at_send=self._agent_status == AgentStatus.WORKING,
            status_revision_at_send=self._status_revision,
        )
        self._messages.append(message)
        self._submission = SUBMISSION_SENDING
        self._error = None
        if self._agent_status == AgentStatus.BLOCKED:
            return await self._deliver_through_attach(message, text, self._send_generation)
        return await self._deliver_through_prompt(message, text, self._send_generation)

    async def _deliver_through_prompt(
        self, message: ComposerMessage, text: str, generation: int
    ) -> str:
        command_id = self._command_id_factory()
        self._last_command_id = command_id
        command = agent_prompt_command(
            command_id=command_id,
            host_id=self._host_id,
            host_epoch=self._host_epoch,
            agent_id=self._agent_id,
            text=text,
            expected_revision=self._expected_revision,
        )
        try:
            submitted = await self._submit_command(command)
        except Exception as exc:  # noqa: BLE001 - outcome mapping is the contract
            if _is_typed_rejection(exc) and not _is_ambiguous(exc):
                return self._mark_failed(
                    message, f"{GATEWAY_REFUSED_PREFIX} ({_exc_code(exc)}): {_exc_message(exc)}"
                )
            return self._mark_unknown(message, f"{UNKNOWN_DELIVERY_MESSAGE} ({_exc_code(exc)})")
        if generation != self._send_generation:
            return SEND_IGNORED
        try:
            if self._observe_command is not None:
                if submitted.get("command_id") != command_id:
                    raise RuntimeError("submitted command identity mismatch")
                await self._observe_command(self._host_id, submitted["command_id"])
            else:
                # Scripted source seam only; production observes the existing
                # journal identity and never makes a second upstream call.
                await self._prompt_waiting_out_launch(text)
        except Exception as exc:  # noqa: BLE001 - outcome mapping is the contract
            if generation != self._send_generation:
                return SEND_IGNORED
            if _exc_code(exc) == "agent_blocked":
                return await self._deliver_through_attach(message, text, generation)
            if _is_typed_rejection(exc) and not _is_ambiguous(exc):
                return self._mark_failed(message, f"{HERDR_REJECTED_PREFIX}: {_exc_message(exc)}")
            return self._mark_unknown(message, UNKNOWN_DELIVERY_MESSAGE)
        if generation != self._send_generation:
            return SEND_IGNORED
        progress = self._progress_after_acknowledgment(message)
        message.state = DeliveryState(kind=DELIVERY_DELIVERED, progress=progress)
        if progress in (PROGRESS_WORKING, PROGRESS_DONE):
            self._submission = SUBMISSION_PROGRESS
        else:
            self._submission = SUBMISSION_ACKED
        self._clear_sent(text)
        self._error = None
        return SEND_DELIVERED_VIA_PROMPT

    async def _prompt_waiting_out_launch(self, text: str) -> dict[str, Any]:
        """One prompt request, waiting out a fresh launch's `agent_not_ready`
        refusals first: that code alone
        retries at a fixed pace inside a bounded budget, because the pane id
        is correct and only time is missing (proven pre-delivery). Every
        other outcome returns or raises immediately."""
        deadline = self._monotonic() + self._not_ready_budget
        if self._prompt_text is None:
            raise RuntimeError("command observer is not configured")
        while True:
            try:
                return await self._prompt_text(self._target, text)
            except Exception as exc:  # noqa: BLE001 - code dispatch below
                if _exc_code(exc) != "agent_not_ready":
                    raise
                if self._monotonic() + self._not_ready_delay >= deadline:
                    raise
                await self._sleep(self._not_ready_delay)

    # -- blocked path ------------------------------------------------------------
    async def _deliver_through_attach(
        self, message: ComposerMessage, text: str, send_generation: int
    ) -> str:
        """Insert draft UTF-8 into the live PTY without Enter or framing
        (`deliverThroughAttach`), acknowledge
        insertion, track no agent progress, and switch to tools keys.
        Multiline drafts stop at explicit review."""
        if not contains_only_safe_scalars(text):
            return self._mark_failed(message, UNSAFE_TEXT_MESSAGE)
        generation = self._live_generation()
        if generation is None:
            return self._mark_failed(message, MISSING_WRITER_MESSAGE)
        if not is_multiline(text):
            try:
                for chunk in chunk_payload(blocked_insert_bytes(text)):
                    if send_generation != self._send_generation:
                        return SEND_IGNORED
                    assert self._sender is not None
                    await self._sender(chunk)
            except Exception as exc:  # noqa: BLE001 - explicit failure, draft kept
                if send_generation != self._send_generation:
                    return SEND_IGNORED
                mark = self._mark_failed if _exc_code(exc) == "no_writer" else self._mark_unknown
                return mark(message, f"{INSERT_FAILED_PREFIX}: {_exc_message(exc)}")
            if send_generation != self._send_generation:
                return SEND_IGNORED
            message.tracks_agent_progress = False
            message.state = DeliveryState(kind=DELIVERY_DELIVERED, progress=PROGRESS_ACKNOWLEDGED)
            self._submission = SUBMISSION_BLOCKED_INSERTED
            self._input_mode = INPUT_MODE_TOOLS_KEYS
            self._clear_sent(text)
            self._error = None
            return SEND_DELIVERED_VIA_ATTACH
        self._pending_blocked = PendingBlocked(
            message_id=message.id,
            text=text,
            generation=generation,
            review=PasteReview(
                line_count=line_count(text),
                character_count=character_count(text),
                preview=preview(text),
            ),
        )
        return SEND_BLOCKED_REVIEW

    async def confirm_blocked_insert(self) -> bool:
        """Insert the exact reviewed bytes iff the bound writer generation is
        still live. Stale generation, dead writer, or no pending review fails
        closed with nothing sent. Pending is cleared before the first byte so
        a mid-send failure cannot be retried implicitly. A mid-send failure
        keeps the draft but earlier chunks are already in the PTY - inherent
        to raw PTY input."""
        pending = self._pending_blocked
        if pending is None:
            self._error = NO_BLOCKED_MESSAGE
            return False
        message = next((m for m in self._messages if m.id == pending.message_id), None)
        if message is None:  # defensive: messages are append-only
            self._pending_blocked = None
            return False
        if self._live_generation() != pending.generation:
            self._pending_blocked = None
            message.state = DeliveryState(kind=DELIVERY_FAILED, message=STALE_BLOCKED_MESSAGE)
            self._submission = SUBMISSION_FAILED
            self._error = STALE_BLOCKED_MESSAGE
            return False
        chunks = chunk_payload(blocked_insert_bytes(pending.text))
        send_generation = self._send_generation
        self._pending_blocked = None
        try:
            for chunk in chunks:
                if send_generation != self._send_generation:
                    return False
                assert self._sender is not None
                await self._sender(chunk)
        except Exception as exc:  # noqa: BLE001 - explicit failure, draft kept
            if send_generation != self._send_generation:
                return False
            mark = self._mark_failed if _exc_code(exc) == "no_writer" else self._mark_unknown
            mark(message, f"{INSERT_FAILED_PREFIX}: {_exc_message(exc)}")
            return False
        if send_generation != self._send_generation:
            return False
        message.tracks_agent_progress = False
        message.state = DeliveryState(kind=DELIVERY_DELIVERED, progress=PROGRESS_ACKNOWLEDGED)
        self._submission = SUBMISSION_BLOCKED_INSERTED
        self._input_mode = INPUT_MODE_TOOLS_KEYS
        self._clear_sent(pending.text)
        self._error = None
        return True

    def cancel_blocked_insert(self) -> bool:
        """Drop the pending blocked review. Provably sends nothing; the
        draft is preserved and Send may be attempted again."""
        pending = self._pending_blocked
        if pending is None:
            return False
        self._pending_blocked = None
        message = next((m for m in self._messages if m.id == pending.message_id), None)
        if message is not None:
            message.state = DeliveryState(kind=DELIVERY_FAILED, message=CANCELLED_BLOCKED_MESSAGE)
        self._submission = SUBMISSION_IDLE
        return True

    # -- status / progress (source-backed) ------------------------------------------
    def _progress_after_acknowledgment(self, message: ComposerMessage) -> str:
        """Progress after acknowledgment, including the
        fast working-to-done pair that arrives as Done alone."""
        if message.observed_working_after_send:
            if self._agent_status == AgentStatus.WORKING:
                return PROGRESS_WORKING
            if self._agent_status in (AgentStatus.DONE, AgentStatus.IDLE):
                return PROGRESS_DONE
        elif (
            not message.agent_was_working_at_send
            and message.status_revision_at_send != self._status_revision
            and self._agent_status == AgentStatus.DONE
        ):
            return PROGRESS_DONE
        if message.agent_was_working_at_send:
            return PROGRESS_AGENT_BUSY
        return PROGRESS_ACKNOWLEDGED

    def agent_status_did_change(self, status: str, label: str = "") -> None:
        """Fold an agent status push into message progress.
        `label` carries human/terminal text that
        may look like an approval prompt; it is inert by design and can
        never trigger a response."""
        _ = label
        next_status = AgentStatus.coerce(status)
        if next_status == self._agent_status:
            return
        self._agent_status = next_status
        self._status_revision += 1
        for message in self._messages:
            if not message.tracks_agent_progress:
                continue
            if (
                next_status == AgentStatus.WORKING
                and message.status_revision_at_send != self._status_revision
            ):
                message.observed_working_after_send = True
            if message.state.kind != DELIVERY_DELIVERED:
                continue
            progress = message.state.progress
            if (
                next_status == AgentStatus.WORKING
                and progress != PROGRESS_DONE
                and message.observed_working_after_send
            ):
                message.state = DeliveryState(kind=DELIVERY_DELIVERED, progress=PROGRESS_WORKING)
                self._lift_submission(message)
            elif next_status == AgentStatus.DONE and (
                message.observed_working_after_send or not message.agent_was_working_at_send
            ):
                message.state = DeliveryState(kind=DELIVERY_DELIVERED, progress=PROGRESS_DONE)
                self._lift_submission(message)
            elif next_status == AgentStatus.IDLE and message.observed_working_after_send:
                message.state = DeliveryState(kind=DELIVERY_DELIVERED, progress=PROGRESS_DONE)
                self._lift_submission(message)

    def _lift_submission(self, message: ComposerMessage) -> None:
        if (
            self._submission == SUBMISSION_ACKED
            and self._messages
            and self._messages[-1] is message
        ):
            self._submission = SUBMISSION_PROGRESS

    def note_blocked_frame(self, generation: int, sequence: int) -> None:
        """Retain bounded identities for explicit insertions; never retain bytes to replay."""
        if self._submission != SUBMISSION_SENDING or not self._messages:
            return
        self._blocked_frames[(generation, sequence)] = (self._messages[-1], self._send_generation)
        while len(self._blocked_frames) > 1024:
            old_generation, old_sequence = next(iter(self._blocked_frames))
            self.note_input_uncertain(old_generation, old_sequence)

    def clear_sensitive(self) -> None:
        """Fence receipts and in-flight closures before dropping account-owned text."""
        self._send_generation += 1
        self._blocked_binding = None
        self._blocked_frames.clear()
        self._messages.clear()
        self._markers = ()
        self._pictures = ()
        self._pending_blocked = None
        self._probe = self._sender = None
        self._draft = ""
        self._error = None
        self._submission = SUBMISSION_IDLE

    def note_input_uncertain(self, generation: int, sequence: int) -> None:
        receipt = self._blocked_frames.pop((generation, sequence), None)
        if receipt is None:
            return
        message, send_generation = receipt
        message.state = DeliveryState(kind=DELIVERY_UNKNOWN, message=LOST_DELIVERY_MESSAGE)
        if not self._draft and not self._pictures:
            self._draft = message.text
        if self._messages[-1] is message:
            if send_generation == self._send_generation:
                self._send_generation += 1
            self._submission = SUBMISSION_UNKNOWN
            self._error = LOST_DELIVERY_MESSAGE

    # -- reconnect ----------------------------------------------------------------------
    def reconnect(self) -> None:
        """Settle reconnect without replaying anything.

        An in-flight send becomes `unknown` (a late outcome is discarded by
        the send generation and can never overwrite it); a pending blocked
        review is dropped because its writer generation is stale. The draft
        is preserved byte-identical and no prompt or PTY byte is emitted.
        Rebinding the live writer afterwards is the owner's job."""
        self._send_generation += 1
        if self._pending_blocked is not None:
            pending = self._pending_blocked
            self._pending_blocked = None
            message = next((m for m in self._messages if m.id == pending.message_id), None)
            if message is not None:
                message.state = DeliveryState(kind=DELIVERY_FAILED, message=REVIEW_LOST_MESSAGE)
            self._submission = SUBMISSION_FAILED
            self._error = REVIEW_LOST_MESSAGE
        if self._submission == SUBMISSION_SENDING:
            for message in reversed(self._messages):
                if message.state.kind == DELIVERY_SENDING:
                    message.state = DeliveryState(
                        kind=DELIVERY_UNKNOWN, message=LOST_DELIVERY_MESSAGE
                    )
                    break
            self._submission = SUBMISSION_UNKNOWN
            self._error = LOST_DELIVERY_MESSAGE

    # -- terminal outcomes ------------------------------------------------------------------
    def _mark_failed(self, message: ComposerMessage, text: str) -> str:
        message.state = DeliveryState(kind=DELIVERY_FAILED, message=text)
        self._submission = SUBMISSION_FAILED
        self._error = text
        return SEND_FAILED

    def _mark_unknown(self, message: ComposerMessage, text: str) -> str:
        message.state = DeliveryState(kind=DELIVERY_UNKNOWN, message=text)
        self._submission = SUBMISSION_UNKNOWN
        self._error = text
        return SEND_UNKNOWN


def bind_blocked_writer(store: ComposerStore, controller: TerminalController) -> None:
    """Wire the store's blocked path to the sole current writer: the
    controller's live control-lease generation probe plus its text-frame
    sender (the same pipe as paste and the tools keyboard)."""
    binding = object()
    store._blocked_binding = binding
    store._blocked_frames.clear()

    async def send(data: bytes) -> None:
        store.note_blocked_frame(*controller.next_input_identity())
        await controller.send_paste_chunk(data)

    def failed(generation: int, sequence: int) -> None:
        if store._blocked_binding is binding:
            store.note_input_uncertain(generation, sequence)

    controller.on_input_failure = failed
    store.bind_writer(
        generation_probe=controller.paste_generation,
        sender=send,
    )


__all__ = [
    "BLOCKER_EMPTY",
    "CANCELLED_BLOCKED_MESSAGE",
    "DELIVERY_DELIVERED",
    "DELIVERY_FAILED",
    "DELIVERY_SENDING",
    "DELIVERY_UNKNOWN",
    "GATEWAY_REFUSED_PREFIX",
    "HERDR_REJECTED_PREFIX",
    "INPUT_MODE_COMPOSER",
    "INPUT_MODE_TOOLS_KEYS",
    "INSERT_FAILED_PREFIX",
    "LOST_DELIVERY_MESSAGE",
    "MISSING_WRITER_MESSAGE",
    "NOT_READY_BUDGET_S",
    "NOT_READY_DELAY_S",
    "NO_BLOCKED_MESSAGE",
    "PROGRESS_ACKNOWLEDGED",
    "PROGRESS_AGENT_BUSY",
    "PROGRESS_DONE",
    "PROGRESS_WORKING",
    "REVIEW_LOST_MESSAGE",
    "SEND_BLOCKED_REVIEW",
    "SEND_DELIVERED_VIA_ATTACH",
    "SEND_DELIVERED_VIA_PROMPT",
    "SEND_FAILED",
    "SEND_IGNORED",
    "SEND_UNKNOWN",
    "STALE_BLOCKED_MESSAGE",
    "SUBMISSION_ACKED",
    "SUBMISSION_BLOCKED_INSERTED",
    "SUBMISSION_FAILED",
    "SUBMISSION_IDLE",
    "SUBMISSION_PROGRESS",
    "SUBMISSION_SENDING",
    "SUBMISSION_UNKNOWN",
    "STAGED_NOTE",
    "UNSAFE_TEXT_MESSAGE",
    "UNKNOWN_DELIVERY_MESSAGE",
    "ComposerMessage",
    "ComposerStore",
    "DeliveryState",
    "PendingBlocked",
    "StagedMarker",
    "bind_blocked_writer",
    "blocked_insert_bytes",
]

#: Staged-marker boundary reminder for importers: markers are local tokens only.
STAGED_NOTE = "markers-only: no attachment/file/snippet backends"
