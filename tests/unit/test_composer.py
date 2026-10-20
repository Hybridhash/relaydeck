"""Composer validation, submission state
machine, blocked path, and the declarative composer box.

All transport is scripted in-process (fake submit/prompt/writer); the
gateway intent record, real terminal writer, and scripted Herdr stub live
in tests/integration/test_blocked_composer.py.
"""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from typing import Any

from relaydeck.client.features.composer.commands import agent_prompt_command
from relaydeck.client.features.composer.components import (
    BLOCKED_NOTICE,
    COMPOSER_KEY,
    DRAFT_KEY,
    EMPTY_HINT,
    ERROR_KEY,
    MARKERS_KEY,
    MODE_KEY,
    NOTICE_KEY,
    REVIEW_CANCEL_KEY,
    REVIEW_CONFIRM_KEY,
    REVIEW_COUNTS_KEY,
    REVIEW_PREVIEW_KEY,
    REVIEW_TITLE,
    SEND_KEY,
    UNKNOWN_NOTICE,
    UNRESOLVED_HINT,
    VALIDATION_KEY,
    ComposerBox,
    markers_line,
)
from relaydeck.client.features.composer.domain import (
    BLOCKER_EMPTY,
    CANCELLED_BLOCKED_MESSAGE,
    DELIVERY_DELIVERED,
    DELIVERY_FAILED,
    DELIVERY_UNKNOWN,
    HERDR_REJECTED_PREFIX,
    INPUT_MODE_COMPOSER,
    INPUT_MODE_TOOLS_KEYS,
    INSERT_FAILED_PREFIX,
    MISSING_WRITER_MESSAGE,
    NO_BLOCKED_MESSAGE,
    NOT_READY_BUDGET_S,
    NOT_READY_DELAY_S,
    PROGRESS_ACKNOWLEDGED,
    PROGRESS_AGENT_BUSY,
    PROGRESS_DONE,
    PROGRESS_WORKING,
    SEND_BLOCKED_REVIEW,
    SEND_DELIVERED_VIA_ATTACH,
    SEND_DELIVERED_VIA_PROMPT,
    SEND_FAILED,
    SEND_IGNORED,
    SEND_UNKNOWN,
    STALE_BLOCKED_MESSAGE,
    SUBMISSION_ACKED,
    SUBMISSION_BLOCKED_INSERTED,
    SUBMISSION_FAILED,
    SUBMISSION_IDLE,
    SUBMISSION_PROGRESS,
    SUBMISSION_SENDING,
    SUBMISSION_UNKNOWN,
    UNKNOWN_DELIVERY_MESSAGE,
    UNSAFE_TEXT_MESSAGE,
    ComposerStore,
    StagedMarker,
)
from relaydeck.client.features.sync.transport import TransportError
from relaydeck.client.features.terminal.paste_policy import PasteReview
from relaydeck.connector.herdr import HerdrError
from relaydeck.contracts.schemas import validate
from tests.support.host_harness import render_component, run

ACK = {"type": "agent_prompted"}

APPROVAL_STRINGS = (
    "Approve? [y/N]",
    "Do you want to proceed? (Y/n)",
    "Agent needs approval: delete 3 files? [Approve] [Deny]",
    "Permission request: run `deploy.sh` -- reply APPROVE to continue",
    "blocked: waiting for input -- press Enter to confirm",
)


class _Script:
    """Scripted gateway submit + upstream prompt + PTY writer with counters."""

    def __init__(self) -> None:
        self.submits: list[dict[str, Any]] = []
        self.submit_plan: list[dict[str, Any] | Exception] = []
        self.prompts: list[tuple[str, str]] = []
        self.prompt_plan: list[dict[str, Any] | Exception] = []
        self.sent: list[bytes] = []
        self.generation: int | None = 7
        self.fail_send: Exception | None = None

    async def submit(self, command: dict[str, Any]) -> dict[str, Any]:
        self.submits.append(command)
        if self.submit_plan:
            outcome = self.submit_plan.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        return {"status": "accepted", "command_id": command["command_id"]}

    async def prompt(self, target: str, text: str) -> dict[str, Any]:
        self.prompts.append((target, text))
        if self.prompt_plan:
            outcome = self.prompt_plan.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        return dict(ACK)

    def probe(self) -> int | None:
        return self.generation

    async def sender(self, data: bytes) -> None:
        if self.fail_send is not None:
            raise self.fail_send
        self.sent.append(data)


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class _Ids:
    def __init__(self) -> None:
        self.n = 0

    def __call__(self) -> str:
        self.n += 1
        return f"cmd-test-{self.n:04d}"


def _store(
    script: _Script,
    clock: _Clock | None = None,
    status: str = "idle",
    prompt_text: Any = None,
) -> ComposerStore:
    clock = clock or _Clock()
    return ComposerStore(
        target="w1:p1",
        submit_command=script.submit,
        prompt_text=prompt_text or script.prompt,
        host_id="home-mac",
        host_epoch="epoch-7",
        agent_id="w1:p1",
        command_id_factory=_Ids(),
        initial_status=status,
        not_ready_delay_s=0.01,
        not_ready_budget_s=0.05,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        generation_probe=script.probe,
        sender=script.sender,
    )


def _quiet() -> tuple[_Script, ComposerStore]:
    script = _Script()
    return script, _store(script)


# -- validation and staged markers -------------------------------------------


def test_empty_and_whitespace_cannot_send() -> None:
    _, store = _quiet()
    assert store.can_send is False
    assert store.send_blockers() == (BLOCKER_EMPTY,)
    store.replace_draft("  \n\t ")
    assert store.can_send is False
    assert store.send_blockers() == (BLOCKER_EMPTY,)
    store.replace_draft("hi")
    assert store.can_send is True
    assert store.send_blockers() == ()


def test_unresolved_markers_block_send_and_resolve_unblocks() -> None:
    _, store = _quiet()
    store.replace_draft("explain \ue000img:abc123\ue001 please")
    assert store.can_send is True  # unknown tokens are plain text
    store.stage_marker("\ue000img:abc123\ue001")
    assert store.can_send is False
    assert store.send_blockers() == ("unresolved:\ue000img:abc123\ue001",)
    assert store.resolve_marker("\ue000img:abc123\ue001", "/tmp/pic.png ") is True
    assert store.draft == "explain /tmp/pic.png  please"
    assert store.can_send is True


def test_marker_boundary_local_tokens_only() -> None:
    _, store = _quiet()
    assert store.resolve_marker("nope", "x") is False
    store.stage_marker("T")
    store.replace_draft("a T b T")
    assert store.resolve_marker("T", "R") is True
    # First occurrence replaced, later duplicates deleted.
    assert store.draft == "a R b "
    assert store.resolve_marker("T", "R") is False  # already resolved
    assert store.unstage_marker("T") is True
    assert store.unstage_marker("T") is False
    assert store.markers == ()


def test_send_ignored_touches_nothing() -> None:
    async def scenario() -> None:
        script, store = _quiet()
        store.replace_draft("   ")
        assert await store.send() == SEND_IGNORED
        assert script.submits == [] and script.prompts == [] and script.sent == []
        assert store.submission == SUBMISSION_IDLE
        assert store.messages == ()
        assert store.last_command_id is None

    run(scenario())


# -- normal submission ----------------------------------------------------------


def test_exactly_one_prompt_per_send() -> None:
    async def scenario() -> None:
        script, store = _quiet()
        store.replace_draft("Explain the failing test")
        assert await store.send() == SEND_DELIVERED_VIA_PROMPT
        assert len(script.submits) == 1
        (command,) = script.submits
        assert command["kind"] == "agent.prompt"
        assert command["target"] == {"agent_id": "w1:p1"}
        assert command["payload"] == {"text": "Explain the failing test"}
        assert command["command_id"] == store.last_command_id
        assert script.prompts == [("w1:p1", "Explain the failing test")]
        assert script.sent == []
        assert store.submission == SUBMISSION_ACKED
        assert store.draft == ""
        assert store.error is None
        assert store.input_mode == INPUT_MODE_COMPOSER
        (message,) = store.messages
        assert message.state.kind == DELIVERY_DELIVERED
        assert message.state.progress == PROGRESS_ACKNOWLEDGED
        assert message.tracks_agent_progress is True

    run(scenario())


def test_blank_edge_lines_never_reach_the_agent() -> None:
    async def scenario() -> None:
        script, store = _quiet()
        store.replace_draft("\n\r\nfirst line\n  second\n\n")
        assert await store.send() == SEND_DELIVERED_VIA_PROMPT
        assert script.prompts == [("w1:p1", "first line\n  second")]
        assert store.draft == ""

    run(scenario())


def test_submission_sending_observable_midflight() -> None:
    async def scenario() -> None:
        script = _Script()
        box: dict[str, ComposerStore] = {}
        seen: list[str] = []

        async def prompt(target: str, text: str) -> dict[str, Any]:
            script.prompts.append((target, text))
            seen.append(box["store"].submission)
            return dict(ACK)

        store = _store(script, prompt_text=prompt)
        box["store"] = store
        store.replace_draft("hi")
        assert await store.send() == SEND_DELIVERED_VIA_PROMPT
        assert seen == [SUBMISSION_SENDING]

    run(scenario())


def test_agent_busy_when_was_working_at_send() -> None:
    async def scenario() -> None:
        script, store = _quiet()
        store.agent_status_did_change("working")
        store.replace_draft("hi")
        assert await store.send() == SEND_DELIVERED_VIA_PROMPT
        (message,) = store.messages
        assert message.state.progress == PROGRESS_AGENT_BUSY
        assert store.submission == SUBMISSION_ACKED

    run(scenario())


def test_progress_transitions_and_submission_lift() -> None:
    async def scenario() -> None:
        _, store = _quiet()
        store.replace_draft("hi")
        assert await store.send() == SEND_DELIVERED_VIA_PROMPT
        store.agent_status_did_change("working")
        (message,) = store.messages
        assert message.state.progress == PROGRESS_WORKING
        assert store.submission == SUBMISSION_PROGRESS
        store.agent_status_did_change("done")
        assert message.state.progress == PROGRESS_DONE
        assert store.submission == SUBMISSION_PROGRESS

    run(scenario())


def test_status_churn_between_send_and_ack_is_observed() -> None:
    async def scenario() -> None:
        script = _Script()
        box: dict[str, ComposerStore] = {}

        async def prompt(target: str, text: str) -> dict[str, Any]:
            script.prompts.append((target, text))
            box["store"].agent_status_did_change("working")
            box["store"].agent_status_did_change("done")
            return dict(ACK)

        store = _store(script, prompt_text=prompt)
        box["store"] = store
        store.replace_draft("hi")
        assert await store.send() == SEND_DELIVERED_VIA_PROMPT
        (message,) = store.messages
        assert message.state.progress == PROGRESS_DONE
        assert store.submission == SUBMISSION_PROGRESS

    run(scenario())


def test_done_arriving_alone_after_send_still_completes() -> None:
    async def scenario() -> None:
        # The status stream keeps only the newest event: a fast
        # working-to-done pair can arrive as Done alone.
        script = _Script()
        box: dict[str, ComposerStore] = {}

        async def prompt(target: str, text: str) -> dict[str, Any]:
            script.prompts.append((target, text))
            box["store"].agent_status_did_change("done")
            return dict(ACK)

        store = _store(script, prompt_text=prompt)
        box["store"] = store
        store.replace_draft("hi")
        assert await store.send() == SEND_DELIVERED_VIA_PROMPT
        (message,) = store.messages
        assert message.state.progress == PROGRESS_DONE

    run(scenario())


def test_idle_after_working_means_done() -> None:
    async def scenario() -> None:
        _, store = _quiet()
        store.replace_draft("hi")
        assert await store.send() == SEND_DELIVERED_VIA_PROMPT
        store.agent_status_did_change("working")
        store.agent_status_did_change("idle")
        (message,) = store.messages
        assert message.state.progress == PROGRESS_DONE

    run(scenario())


def test_same_status_push_is_noop() -> None:
    async def scenario() -> None:
        _, store = _quiet()
        store.replace_draft("hi")
        assert await store.send() == SEND_DELIVERED_VIA_PROMPT
        store.agent_status_did_change("idle")
        (message,) = store.messages
        assert message.observed_working_after_send is False
        assert message.state.progress == PROGRESS_ACKNOWLEDGED

    run(scenario())


def test_concurrent_send_ignored() -> None:
    async def scenario() -> None:
        script = _Script()
        started = asyncio.Event()
        release = asyncio.Event()

        async def prompt(target: str, text: str) -> dict[str, Any]:
            script.prompts.append((target, text))
            started.set()
            await release.wait()
            return dict(ACK)

        store = _store(script, prompt_text=prompt)
        store.replace_draft("hi")
        task = asyncio.create_task(store.send())
        await started.wait()
        assert store.submission == SUBMISSION_SENDING
        assert await store.send() == SEND_IGNORED
        release.set()
        assert await task == SEND_DELIVERED_VIA_PROMPT
        assert len(script.submits) == 1 and len(script.prompts) == 1

    run(scenario())


# -- agent_not_ready bounded window ----------------------------------------------


def test_not_ready_window_defaults_are_source_derived() -> None:
    # 500 ms pace and 10 s budget, matching the transport shell-readiness wait.
    assert NOT_READY_DELAY_S == 0.5
    assert NOT_READY_BUDGET_S == 10.0


def test_not_ready_retries_within_budget_then_succeeds() -> None:
    async def scenario() -> None:
        script = _Script()
        clock = _Clock()
        script.prompt_plan = [
            HerdrError("agent_not_ready", "not an active named agent"),
            HerdrError("agent_not_ready", "not an active named agent"),
            dict(ACK),
        ]
        store = _store(script, clock)
        store.replace_draft("hi")
        assert await store.send() == SEND_DELIVERED_VIA_PROMPT
        assert len(script.submits) == 1  # one envelope; retries re-await only
        assert len(script.prompts) == 3
        assert clock.sleeps == [0.01, 0.01]
        assert store.submission == SUBMISSION_ACKED

    run(scenario())


def test_not_ready_budget_exhaustion_fails_bounded() -> None:
    async def scenario() -> None:
        script = _Script()
        clock = _Clock()
        script.prompt_plan = [HerdrError("agent_not_ready", "missing")] * 50
        store = _store(script, clock)
        store.replace_draft("hi there")
        assert await store.send() == SEND_FAILED
        # Budget 0.05 at pace 0.01: attempts at t=0..0.04, then stop.
        assert len(script.prompts) == 5
        assert clock.sleeps == [0.01] * 4
        assert len(script.submits) == 1
        assert store.submission == SUBMISSION_FAILED
        assert store.draft == "hi there"
        assert store.error is not None and store.error.startswith(HERDR_REJECTED_PREFIX)

    run(scenario())


def test_not_found_fails_immediately_without_retry() -> None:
    async def scenario() -> None:
        script = _Script()
        clock = _Clock()
        script.prompt_plan = [HerdrError("agent_not_found", "no such agent: w9:p9")]
        store = _store(script, clock)
        store.replace_draft("hi")
        assert await store.send() == SEND_FAILED
        assert len(script.prompts) == 1
        assert clock.sleeps == []
        assert store.submission == SUBMISSION_FAILED
        assert store.draft == "hi"

    run(scenario())


# -- unknown outcomes and failures ---------------------------------------------------


def test_timeout_after_dispatch_is_unknown_without_retry() -> None:
    async def scenario() -> None:
        script = _Script()
        script.prompt_plan = [TimeoutError("upstream silent")]
        store = _store(script)
        store.replace_draft("do not lose me")
        assert await store.send() == SEND_UNKNOWN
        assert len(script.submits) == 1
        assert len(script.prompts) == 1  # no blind retry
        assert store.submission == SUBMISSION_UNKNOWN
        assert store.draft == "do not lose me"
        assert store.error == UNKNOWN_DELIVERY_MESSAGE
        (message,) = store.messages
        assert message.state.kind == DELIVERY_UNKNOWN

    run(scenario())


def test_retryable_submit_failure_is_unknown_prompt_never_called() -> None:
    async def scenario() -> None:
        script = _Script()
        script.submit_plan = [TransportError("host_offline", "down", True)]
        store = _store(script)
        store.replace_draft("kept")
        assert await store.send() == SEND_UNKNOWN
        assert len(script.submits) == 1
        assert script.prompts == []
        assert store.submission == SUBMISSION_UNKNOWN
        assert store.draft == "kept"
        assert store.error is not None and "host_offline" in store.error

    run(scenario())


def test_submit_rejection_fails_prompt_never_called() -> None:
    async def scenario() -> None:
        script = _Script()
        script.submit_plan = [TransportError("forbidden", "denied", False)]
        store = _store(script)
        store.replace_draft("kept")
        assert await store.send() == SEND_FAILED
        assert len(script.submits) == 1
        assert script.prompts == []
        assert store.submission == SUBMISSION_FAILED
        assert store.draft == "kept"
        assert store.error is not None and "forbidden" in store.error

    run(scenario())


def test_unexpected_prompt_error_is_unknown_not_success() -> None:
    async def scenario() -> None:
        script = _Script()
        script.prompt_plan = [RuntimeError("socket reset")]
        store = _store(script)
        store.replace_draft("kept")
        assert await store.send() == SEND_UNKNOWN
        assert store.submission == SUBMISSION_UNKNOWN
        assert store.draft == "kept"

    run(scenario())


def test_draft_error_survive_and_send_again_uses_new_command() -> None:
    async def scenario() -> None:
        script = _Script()
        script.prompt_plan = [HerdrError("agent_not_found", "gone")]
        store = _store(script)
        store.replace_draft("try again")
        assert await store.send() == SEND_FAILED
        first_id = store.last_command_id
        assert store.draft == "try again"
        assert store.error is not None
        store.replace_draft("try again!")
        assert store.error is None  # editing clears the stale error
        assert await store.send() == SEND_DELIVERED_VIA_PROMPT
        assert store.last_command_id != first_id
        assert len(script.submits) == 2

    run(scenario())


def test_draft_edit_midflight_is_preserved_on_ack() -> None:
    async def scenario() -> None:
        script = _Script()
        box: dict[str, ComposerStore] = {}

        async def prompt(target: str, text: str) -> dict[str, Any]:
            script.prompts.append((target, text))
            box["store"].replace_draft("newer edits")
            return dict(ACK)

        store = _store(script, prompt_text=prompt)
        box["store"] = store
        store.replace_draft("hi")
        assert await store.send() == SEND_DELIVERED_VIA_PROMPT
        assert store.draft == "newer edits"
        (message,) = store.messages
        assert message.text == "hi"

    run(scenario())


# -- blocked path -----------------------------------------------------------------------


def test_blocked_status_single_line_golden_bytes() -> None:
    async def scenario() -> None:
        script, store = _quiet()
        store.agent_status_did_change("blocked")
        store.replace_draft("answer the question")
        assert await store.send() == SEND_DELIVERED_VIA_ATTACH
        assert script.submits == [] and script.prompts == []
        assert script.sent == [b"answer the question"]
        assert store.submission == SUBMISSION_BLOCKED_INSERTED
        assert store.input_mode == INPUT_MODE_TOOLS_KEYS
        assert store.draft == ""
        assert store.error is None
        (message,) = store.messages
        assert message.tracks_agent_progress is False
        assert message.state.kind == DELIVERY_DELIVERED
        assert message.state.progress == PROGRESS_ACKNOWLEDGED

    run(scenario())


def test_blocked_golden_bytes_cover_unicode_and_spacing() -> None:
    async def scenario() -> None:
        for draft in ("héllo 😀 done", "tab\there", "trailing space "):
            script, store = _quiet()
            store.agent_status_did_change("blocked")
            store.replace_draft(draft)
            assert await store.send() == SEND_DELIVERED_VIA_ATTACH
            # Exact draft UTF-8: no Enter appended, no bracketed framing.
            assert script.sent == [draft.encode("utf-8")]
            assert b"\x1b[200~" not in script.sent[0]

    run(scenario())


def test_racing_agent_blocked_selects_raw_insertion() -> None:
    async def scenario() -> None:
        script = _Script()
        script.prompt_plan = [HerdrError("agent_blocked", "waiting for input")]
        store = _store(script)
        store.replace_draft("race me")
        assert await store.send() == SEND_DELIVERED_VIA_ATTACH
        assert len(script.submits) == 1
        assert len(script.prompts) == 1
        assert script.sent == [b"race me"]
        assert store.submission == SUBMISSION_BLOCKED_INSERTED
        assert store.input_mode == INPUT_MODE_TOOLS_KEYS
        (message,) = store.messages
        assert message.tracks_agent_progress is False

    run(scenario())


def test_not_ready_then_racing_blocked_inserts() -> None:
    async def scenario() -> None:
        script = _Script()
        script.prompt_plan = [
            HerdrError("agent_not_ready", "booting"),
            HerdrError("agent_blocked", "waiting"),
        ]
        store = _store(script)
        store.replace_draft("hi")
        assert await store.send() == SEND_DELIVERED_VIA_ATTACH
        assert len(script.submits) == 1
        assert len(script.prompts) == 2
        assert script.sent == [b"hi"]

    run(scenario())


def test_multiline_blocked_requires_review_then_inserts_exact() -> None:
    async def scenario() -> None:
        script, store = _quiet()
        store.agent_status_did_change("blocked")
        draft = "first line\nsecond line 😀\n"
        store.replace_draft(draft)
        assert await store.send() == SEND_BLOCKED_REVIEW
        assert script.sent == []  # nothing inserted before explicit confirm
        pending = store.pending_blocked
        assert pending is not None
        assert pending.text == draft
        assert pending.generation == 7
        assert pending.review.line_count == 3
        assert pending.review.character_count == len(draft)
        assert pending.review.preview == draft
        assert await store.send() == SEND_IGNORED  # no stacked sends
        assert await store.confirm_blocked_insert() is True
        assert b"".join(script.sent) == draft.encode("utf-8")
        assert script.sent == [draft.encode("utf-8")]
        assert store.submission == SUBMISSION_BLOCKED_INSERTED
        assert store.input_mode == INPUT_MODE_TOOLS_KEYS
        assert store.draft == ""
        (message,) = store.messages
        assert message.tracks_agent_progress is False

    run(scenario())


def test_submission_stays_sending_while_blocked_review_pending() -> None:
    async def scenario() -> None:
        # Direct-blocked path: multiline draft stops at review, still sending.
        script, store = _quiet()
        store.agent_status_did_change("blocked")
        store.replace_draft("a\nb")
        assert await store.send() == SEND_BLOCKED_REVIEW
        assert store.pending_blocked is not None
        assert store.submission == SUBMISSION_SENDING
        assert await store.confirm_blocked_insert() is True
        assert store.submission == SUBMISSION_BLOCKED_INSERTED
        # Racing-blocked path: agent_blocked arrives mid-prompt, same state.
        racy = _Script()
        racy.prompt_plan = [HerdrError("agent_blocked", "waiting for input")]
        store2 = _store(racy)
        store2.replace_draft("x\ny")
        assert await store2.send() == SEND_BLOCKED_REVIEW
        assert store2.pending_blocked is not None
        assert store2.submission == SUBMISSION_SENDING

    run(scenario())


def test_blocked_review_cancel_sends_nothing() -> None:
    async def scenario() -> None:
        script, store = _quiet()
        store.agent_status_did_change("blocked")
        store.replace_draft("a\nb")
        assert await store.send() == SEND_BLOCKED_REVIEW
        assert store.cancel_blocked_insert() is True
        assert store.cancel_blocked_insert() is False
        assert script.sent == []
        assert store.draft == "a\nb"
        assert store.submission == SUBMISSION_IDLE
        (message,) = store.messages
        assert message.state.kind == DELIVERY_FAILED
        assert message.state.message == CANCELLED_BLOCKED_MESSAGE

    run(scenario())


def test_confirm_without_pending_fails_closed() -> None:
    async def scenario() -> None:
        script, store = _quiet()
        assert await store.confirm_blocked_insert() is False
        assert store.error == NO_BLOCKED_MESSAGE
        assert script.sent == []

    run(scenario())


def test_stale_generation_confirm_fails_closed() -> None:
    async def scenario() -> None:
        script, store = _quiet()
        store.agent_status_did_change("blocked")
        store.replace_draft("a\nb")
        assert await store.send() == SEND_BLOCKED_REVIEW
        script.generation = 8  # writer replaced under the pending review
        assert await store.confirm_blocked_insert() is False
        assert script.sent == []
        assert store.draft == "a\nb"
        assert store.submission == SUBMISSION_FAILED
        assert store.error == STALE_BLOCKED_MESSAGE

    run(scenario())


def test_missing_writer_fails_with_no_hidden_fallback() -> None:
    async def scenario() -> None:
        script = _Script()
        script.generation = None
        store = _store(script)
        store.agent_status_did_change("blocked")
        store.replace_draft("hi")
        assert await store.send() == SEND_FAILED
        assert script.sent == []
        assert store.submission == SUBMISSION_FAILED
        assert store.error == MISSING_WRITER_MESSAGE
        assert store.draft == "hi"
        assert store.input_mode == INPUT_MODE_COMPOSER
        # Unbound store behaves the same.
        script2 = _Script()
        bare = ComposerStore(
            target="w1:p1",
            submit_command=script2.submit,
            prompt_text=script2.prompt,
            host_id="h",
            host_epoch="e",
            agent_id="a",
            initial_status="blocked",
        )
        bare.replace_draft("hi")
        assert await bare.send() == SEND_FAILED
        assert bare.error == MISSING_WRITER_MESSAGE
        assert script2.sent == []

    run(scenario())


def test_dead_writer_send_failure_keeps_draft() -> None:
    async def scenario() -> None:
        script = _Script()
        script.fail_send = TransportError("host_offline", "dead", True)
        store = _store(script)
        store.agent_status_did_change("blocked")
        store.replace_draft("hi")
        assert await store.send() == SEND_UNKNOWN
        assert store.error is not None and store.error.startswith(INSERT_FAILED_PREFIX)
        assert store.draft == "hi"
        assert store.input_mode == INPUT_MODE_COMPOSER

    run(scenario())


def test_unsafe_blocked_text_rejected() -> None:
    async def scenario() -> None:
        script, store = _quiet()
        store.agent_status_did_change("blocked")
        store.replace_draft("hi\x07there")
        assert await store.send() == SEND_FAILED
        assert script.sent == []
        assert store.error == UNSAFE_TEXT_MESSAGE
        assert store.draft == "hi\x07there"

    run(scenario())


def test_blocked_insert_never_tracks_progress() -> None:
    async def scenario() -> None:
        _, store = _quiet()
        store.agent_status_did_change("blocked")
        store.replace_draft("hi")
        assert await store.send() == SEND_DELIVERED_VIA_ATTACH
        store.agent_status_did_change("working")
        store.agent_status_did_change("done")
        (message,) = store.messages
        assert message.state.progress == PROGRESS_ACKNOWLEDGED
        assert store.submission == SUBMISSION_BLOCKED_INSERTED

    run(scenario())


def test_large_blocked_insert_chunks_on_utf8_boundaries() -> None:
    async def scenario() -> None:
        script, store = _quiet()
        store.agent_status_did_change("blocked")
        draft = "é" * 9000  # 18000 bytes: two frames, split between chars
        store.replace_draft(draft)
        assert await store.send() == SEND_DELIVERED_VIA_ATTACH
        assert len(script.sent) == 2
        assert all(len(chunk) <= 16 * 1024 for chunk in script.sent)
        assert b"".join(script.sent) == draft.encode("utf-8")

    run(scenario())


def test_confirm_preserves_newer_draft_edits() -> None:
    async def scenario() -> None:
        script, store = _quiet()
        store.agent_status_did_change("blocked")
        store.replace_draft("a\nb")
        assert await store.send() == SEND_BLOCKED_REVIEW
        store.replace_draft("a\nb plus more")
        assert await store.confirm_blocked_insert() is True
        assert script.sent == [b"a\nb"]  # exact reviewed bytes
        assert store.draft == "a\nb plus more"  # newer edits never destroyed

    run(scenario())


def test_unbind_writer_then_blocked_fails() -> None:
    async def scenario() -> None:
        script, store = _quiet()
        store.unbind_writer()
        store.agent_status_did_change("blocked")
        store.replace_draft("hi")
        assert await store.send() == SEND_FAILED
        assert store.error == MISSING_WRITER_MESSAGE
        assert script.sent == []

    run(scenario())


# -- approval-looking strings are inert -------------------------------------------


def test_approval_looking_strings_never_respond() -> None:
    async def scenario() -> None:
        script, store = _quiet()
        store.replace_draft("my draft")
        statuses = ("idle", "working", "blocked", "done", "idle")
        for status, label in zip(statuses, APPROVAL_STRINGS, strict=True):
            store.agent_status_did_change(status, label)
        for label in APPROVAL_STRINGS:
            store.agent_status_did_change("blocked", label)
        assert script.submits == [] and script.prompts == [] and script.sent == []
        assert store.messages == ()
        # Only an explicit Send moves the draft — and inserts exactly it.
        assert await store.send() == SEND_DELIVERED_VIA_ATTACH
        assert script.sent == [b"my draft"]

    run(scenario())


def test_approval_looking_draft_moves_only_on_explicit_send() -> None:
    async def scenario() -> None:
        script, store = _quiet()
        store.replace_draft("yes, approve it")
        store.agent_status_did_change("working", "Approve? [y/N]")
        assert script.submits == [] and script.prompts == [] and script.sent == []
        assert await store.send() == SEND_DELIVERED_VIA_PROMPT
        assert script.prompts == [("w1:p1", "yes, approve it")]

    run(scenario())


# -- reconnect -----------------------------------------------------------------------------


def test_reconnect_never_replays_inflight() -> None:
    async def scenario() -> None:
        script = _Script()
        started = asyncio.Event()
        release = asyncio.Event()

        async def prompt(target: str, text: str) -> dict[str, Any]:
            script.prompts.append((target, text))
            started.set()
            await release.wait()
            return dict(ACK)

        store = _store(script, prompt_text=prompt)
        store.replace_draft("in flight")
        task = asyncio.create_task(store.send())
        await started.wait()
        store.reconnect()
        assert store.submission == SUBMISSION_UNKNOWN
        assert store.draft == "in flight"
        release.set()
        # The late ack lands after reconnect and is discarded.
        assert await task == SEND_IGNORED
        assert store.submission == SUBMISSION_UNKNOWN
        assert store.draft == "in flight"
        assert len(script.submits) == 1 and len(script.prompts) == 1
        assert script.sent == []
        (message,) = store.messages
        assert message.state.kind == DELIVERY_UNKNOWN

    run(scenario())


def test_reconnect_mid_attach_insert_is_discarded() -> None:
    async def scenario() -> None:
        script = _Script()
        started = asyncio.Event()
        release = asyncio.Event()

        async def gated_sender(data: bytes) -> None:
            started.set()
            await release.wait()
            script.sent.append(data)

        store = _store(script)
        store.bind_writer(script.probe, gated_sender)
        store.agent_status_did_change("blocked")
        store.replace_draft("in flight")
        task = asyncio.create_task(store.send())
        await started.wait()
        store.reconnect()
        assert store.submission == SUBMISSION_UNKNOWN
        release.set()
        # The late insert settlement lands after reconnect and is discarded.
        assert await task == SEND_IGNORED
        assert store.submission == SUBMISSION_UNKNOWN
        assert store.draft == "in flight"
        (message,) = store.messages
        assert message.state.kind == DELIVERY_UNKNOWN

    run(scenario())


def test_reconnect_drops_pending_review() -> None:
    async def scenario() -> None:
        script, store = _quiet()
        store.agent_status_did_change("blocked")
        store.replace_draft("a\nb")
        assert await store.send() == SEND_BLOCKED_REVIEW
        store.reconnect()
        assert store.pending_blocked is None
        assert store.submission == SUBMISSION_FAILED
        assert script.sent == []
        assert store.draft == "a\nb"
        assert await store.confirm_blocked_insert() is False
        assert script.sent == []

    run(scenario())


def test_reconnect_idle_is_noop_and_send_still_works() -> None:
    async def scenario() -> None:
        script, store = _quiet()
        store.replace_draft("hi")
        store.reconnect()
        assert store.submission == SUBMISSION_IDLE
        assert store.draft == "hi"
        assert await store.send() == SEND_DELIVERED_VIA_PROMPT
        assert len(script.prompts) == 1

    run(scenario())


def test_unknown_status_coerces_without_crash() -> None:
    async def scenario() -> None:
        _, store = _quiet()
        store.agent_status_did_change("super-working")
        assert store.agent_status == "unknown"
        store.replace_draft("hi")
        assert await store.send() == SEND_DELIVERED_VIA_PROMPT

    run(scenario())


# -- envelope and boundaries -----------------------------------------------------------------


def test_prompt_command_envelope_shape() -> None:
    command = agent_prompt_command(
        command_id="cmd-test-0001",
        host_id="home-mac",
        host_epoch="epoch-7",
        agent_id="w1:p1",
        text="hi",
    )
    assert command["protocol"] == "relaydeck.gateway.v1"
    assert command["kind"] == "agent.prompt"
    assert command["target"] == {"agent_id": "w1:p1"}
    assert command["payload"] == {"text": "hi"}
    validate("relaydeck/gateway/v1/command-submit", command)


def test_composer_domain_is_flet_free() -> None:
    root = Path(__file__).resolve().parent.parent.parent / "src" / "relaydeck"
    for name in ("domain.py", "commands.py"):
        tree = ast.parse((root / "client" / "features" / "composer" / name).read_text())
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
            elif isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
        assert not any(n == "flet" or n.startswith("flet.") for n in imported), imported


# -- composer box component --------------------------------------------------------------------


def _box_props(**over: Any) -> dict[str, Any]:
    props: dict[str, Any] = {
        "draft": "",
        "markers": (),
        "can_send": False,
        "blockers": (BLOCKER_EMPTY,),
        "submission": SUBMISSION_IDLE,
        "error": None,
        "review": None,
        "input_mode": INPUT_MODE_COMPOSER,
        "on_draft_change": lambda value: None,
        "on_send": lambda: None,
        "on_confirm_blocked": lambda: None,
        "on_cancel_blocked": lambda: None,
        "on_clear_error": lambda: None,
    }
    props.update(over)
    return props


def test_composerbox_empty_shows_hint_and_disabled_send() -> None:
    async def scenario() -> None:
        app = await render_component(ComposerBox, **_box_props())
        assert app.find(COMPOSER_KEY) is not None
        assert app.find(DRAFT_KEY).value == ""
        assert app.find(DRAFT_KEY).multiline is True
        assert app.find(SEND_KEY).disabled is True
        assert app.find(VALIDATION_KEY).value == EMPTY_HINT

    run(scenario())


def test_composerbox_draft_events_fire() -> None:
    async def scenario() -> None:
        drafts: list[str] = []
        sends: list[bool] = []
        app = await render_component(
            ComposerBox,
            **_box_props(
                draft="hi",
                can_send=True,
                blockers=(),
                on_draft_change=lambda value: drafts.append(value),
                on_send=lambda: sends.append(True),
            ),
        )
        assert app.find(SEND_KEY).disabled is False
        await app.set_props_and_fire(DRAFT_KEY, {"value": "hi!"}, "change", "hi!")
        assert drafts == ["hi!"]
        await app.set_props_and_fire(SEND_KEY, {}, "click")
        assert sends == [True]

    run(scenario())


def test_composerbox_markers_and_unresolved_hint() -> None:
    async def scenario() -> None:
        markers = (StagedMarker(token="img:1"), StagedMarker(token="img:2", resolved=True))
        assert markers_line(()) == ""
        assert markers_line(markers) == "Staged: img:1 (resolving…), img:2 (ready)"
        app = await render_component(
            ComposerBox,
            **_box_props(
                draft="see img:1",
                markers=markers,
                can_send=False,
                blockers=("unresolved:img:1",),
            ),
        )
        assert app.find(MARKERS_KEY).value == "Staged: img:1 (resolving…), img:2 (ready)"
        assert app.find(VALIDATION_KEY).value == UNRESOLVED_HINT
        assert app.find(SEND_KEY).disabled is True

    run(scenario())


def test_composerbox_error_and_dismiss() -> None:
    async def scenario() -> None:
        cleared: list[bool] = []
        app = await render_component(
            ComposerBox,
            **_box_props(
                draft="kept",
                can_send=True,
                blockers=(),
                submission=SUBMISSION_FAILED,
                error="boom",
                on_clear_error=lambda: cleared.append(True),
            ),
        )
        assert app.find(ERROR_KEY).value == "boom"
        await app.set_props_and_fire("composer-error-dismiss", {}, "click")
        assert cleared == [True]

    run(scenario())


def test_composerbox_unknown_and_blocked_notices() -> None:
    async def scenario() -> None:
        app = await render_component(
            ComposerBox,
            **_box_props(
                draft="kept",
                can_send=True,
                blockers=(),
                submission=SUBMISSION_UNKNOWN,
                error="uncertain",
            ),
        )
        assert app.find(ERROR_KEY).value == "uncertain"
        assert app.find(NOTICE_KEY).value == UNKNOWN_NOTICE
        app = await render_component(
            ComposerBox,
            **_box_props(submission=SUBMISSION_BLOCKED_INSERTED, input_mode=INPUT_MODE_TOOLS_KEYS),
        )
        assert app.find(NOTICE_KEY).value == BLOCKED_NOTICE
        assert app.find(MODE_KEY).value == "Tools keys"

    run(scenario())


def test_composerbox_blocked_review_surface() -> None:
    async def scenario() -> None:
        confirmed: list[bool] = []
        cancelled: list[bool] = []
        review = PasteReview(line_count=2, character_count=3, preview="a\nb")
        app = await render_component(
            ComposerBox,
            **_box_props(
                draft="a\nb",
                can_send=True,
                blockers=(),
                submission=SUBMISSION_SENDING,
                review=review,
                on_confirm_blocked=lambda: confirmed.append(True),
                on_cancel_blocked=lambda: cancelled.append(True),
            ),
        )
        assert app.find("composer-blocked-title").value == REVIEW_TITLE
        assert app.find(REVIEW_COUNTS_KEY).value == "2 lines · 3 characters"
        preview_control = app.find(REVIEW_PREVIEW_KEY)
        assert preview_control.value == "a\nb"
        assert preview_control.selectable is True
        assert app.find(SEND_KEY).disabled is True
        await app.set_props_and_fire(REVIEW_CONFIRM_KEY, {}, "click")
        assert confirmed == [True]
        await app.set_props_and_fire(REVIEW_CANCEL_KEY, {}, "click")
        assert cancelled == [True]

    run(scenario())
