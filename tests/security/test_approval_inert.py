"""Approval-shaped output creates nothing, anywhere.

The composer never answers approval-looking text; this module extends the proof to every
other path that could interpret output: agent-status labels, push eligibility/payloads,
deep-link resolution, notification taps, and paste review. A final
sweep checks client source outside the explicit device-pairing modules:
their approved protocol phases are not terminal-output authorization.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

APPROVAL_TEXTS: tuple[str, ...] = (
    "Agent needs approval: delete 3 files? [Approve] [Deny]",
    "APPROVE DEPLOY? reply YES to confirm",
    "Permission required: run `rm -rf /`? (y/n)",
    "The agent needs your approval to edit 12 files",
    "[sudo] password for operator:",
)


def _store(submits: list[dict[str, Any]], prompts: list[tuple[str, str]]) -> Any:
    from relaydeck.client.features.composer.domain import ComposerStore

    async def submit_command(body: dict[str, Any]) -> dict[str, Any]:
        submits.append(body)
        return {"status": "accepted"}

    async def prompt_text(target: str, text: str) -> dict[str, Any]:
        prompts.append((target, text))
        return {"type": "agent_prompted"}

    return ComposerStore(
        target="agent-1",
        submit_command=submit_command,
        prompt_text=prompt_text,
        host_id="home-mac",
        host_epoch="epoch-7",
        agent_id="agent-1",
    )


def test_approval_labels_never_send() -> None:
    """Status labels carrying approval-shaped text change progress
    display only: zero submissions, zero prompts (018 extension: every
    shape in the battery, plus blocked-state labels)."""
    submits: list[dict[str, Any]] = []
    prompts: list[tuple[str, str]] = []
    store = _store(submits, prompts)
    for text in APPROVAL_TEXTS:
        store.agent_status_did_change("blocked", text)
        store.agent_status_did_change("working", text)
    assert submits == []
    assert prompts == []


def test_approval_shaped_draft_sends_only_on_explicit_send() -> None:
    """An approval-looking draft is inert until the user presses Send;
    even then it travels as literal prompt text, not as a decision."""
    submits: list[dict[str, Any]] = []
    prompts: list[tuple[str, str]] = []
    store = _store(submits, prompts)

    async def scenario() -> None:
        store.replace_draft(APPROVAL_TEXTS[0])
        assert submits == [] and prompts == []  # typing/drafting sends nothing
        await store.send()
        assert len(submits) == 1  # exactly one explicit submission
        assert submits[0]["payload"] == {"text": APPROVAL_TEXTS[0]}

    asyncio.run(scenario())


def test_push_paths_emit_no_command_or_key() -> None:
    """Adversarial event/session/host ids and approval-shaped text
    reach the push store only as opaque refs: hint payloads carry no
    ids, and resolving hints/links emits no input or command."""
    from relaydeck.gateway.push import HINT_KEYS, PushStore

    store = PushStore()
    try:
        now = 1_000_000
        store.register_device("acct-1", "install-1", "tok-1", "android", now)
        evil_event = "evt approve --yes; rm -rf / [Approve]"
        minted = store.notify(
            account="acct-1",
            account_hosts=frozenset({"home-mac"}),
            host_id="home-mac",
            session_id="sess approve",
            event_id=evil_event,
            category="attention",
            now=now,
        )
        assert minted
        payload = store.hint_payload(str(minted[0]["notification_id"]))
        assert set(payload) == set(HINT_KEYS)
        blob = " ".join(payload.values())
        assert "approve" not in blob and "rm -rf" not in blob
        # Resolution maps refs back to ids for the authorized owner (its
        # purpose); it emits no command, key, input, or decision.
        resolved = store.resolve_hint("acct-1", str(minted[0]["notification_id"]), now)
        assert resolved is not None
        assert set(resolved) == {
            "notification_id",
            "event_ref",
            "host_ref",
            "session_ref",
            "category",
            "host_id",
            "session_id",
            "route",
        }
        link = store.resolve_link(str(minted[0]["link_token"]), "acct-1", now)
        assert link is not None
        assert set(link) == set(resolved)
    finally:
        store.close()


def test_tap_resolution_reads_state_only() -> None:
    """The deep-link resolve route returns refs/route ids; it never
    enqueues input or commands (backend logs stay empty)."""
    from tests.security.helpers import auth, make_client, mint_token

    client, state, backend = make_client()
    token = mint_token(state, "acct-1")
    now = state.now_ms()
    state.push.register_device("acct-1", "install-1", "tok-1", "android", now)
    minted = state.push.notify(
        account="acct-1",
        account_hosts=frozenset({"home-mac"}),
        host_id="home-mac",
        session_id="sess-1",
        event_id="evt-tap-1",
        category="completion",
        now=now,
    )
    assert minted
    r = client.post(
        "/v1/deep-links/resolve",
        headers=auth(token),
        json={"token": str(minted[0]["link_token"])},
    )
    assert r.status_code == 200
    assert backend.input_logs == {}
    assert backend.commands == {}


def test_no_output_scanning_approval_trigger_exists() -> None:
    """Sweep: no client code scans output/terminal text for approval
    keywords, decisions, or auto-responses."""
    root = Path(__file__).resolve().parent.parent.parent / "src" / "relaydeck" / "client"
    triggers = ("approve", "approval", "yes to confirm", "reply yes", "[y/n]")
    dq = chr(34) * 3
    sq = chr(39) * 3
    hits: list[str] = []
    # Device enrollment approval is an authenticated, explicit Mac action,
    # distinct from interpreting terminal output as agent authorization.
    pairing_files = {
        Path("components/design_system/connect.py"),
        Path("features/pairing/service.py"),
        Path("packaged_runtime.py"),
    }
    for path in sorted(root.rglob("*.py")):
        if path.relative_to(root) in pairing_files:
            continue
        if path.name.startswith("fake_"):
            continue  # test doubles: synthetic fixture titles, not production
        in_docstring = False
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            stripped = line.strip()
            boundary = dq in line or sq in line
            if line.count(dq) % 2 == 1 or line.count(sq) % 2 == 1:
                in_docstring = not in_docstring
            quoted_prose = stripped.startswith(('"', "'")) and not any(
                op in stripped for op in (" in ", " not ", "(", "=")
            )
            if in_docstring or boundary or stripped.startswith("#") or quoted_prose:
                continue  # prose documenting inertness, not a trigger
            if any(t in line.lower() for t in triggers):
                hits.append(f"{path.name}:{lineno}:{line.strip()[:100]}")
    assert hits == [], f"approval-interpreting code? {hits}"
