"""Web-vs-android shell scenario parity.

The SAME full shell scenario runs through the REAL gateway (live uvicorn
+ `FakeBackend`), the REAL `TerminalController` + `ShellSession`, and
REAL UI state (`ShellViewState` + rendered `ShellScreen`), under two
platform adapter configs:

- web: desktop physical keyboard (direct text commits), web clipboard.
- android: soft keyboard (typing flows through `ImeState` commit), android
  clipboard, keyboard toggle available.

Every platform-independent observable must be IDENTICAL across configs:
output bytes, input frames (generation/seq/kind/data), resize log, paste
framing, clipboard writes, phases, generations, ticket/attach call shapes,
and rendered UI keys/values. Each config's deliberate input-path
difference is asserted (web commits direct, android commits via IME) and
the outcome equality proves the paths converge to the same bytes.

PINNED DIVERGENCES (the only allowed config differences):
1. Input path: web `commit_text` direct vs android `ImeState.commit` +
   `route_ime_commit`. Pinned: both produce byte-identical PTY frames.
2. Keyboard plumbing: web (`physical`) renders Text mode with no Keys
   dock, android (`soft`) renders Keys mode with the dock. Pinned and
   asserted in `test_shell_ui_keyboard_plumbing_diverges_but_state_matches`.

The recorded demo on a non-production real host + browser/device
matrix is ENVIRONMENT-BLOCKED: no provisioned gateway host, no office
browser matrix, no Android device/toolchain here. This file is
the headless parity evidence; real-device/browser proof stays open.
"""

from __future__ import annotations

import asyncio
import json
import struct
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx

from relaydeck.client.adapters.fake import FakeClipboard
from relaydeck.client.features.create.commands import shell_create_command
from relaydeck.client.features.shell import (
    DISCONNECT_MESSAGE,
    EXIT_MESSAGE,
    SELF_TAKEOVER_NOTICE,
    SHELL_CONFLICT,
    SHELL_DISCONNECTED,
    SHELL_EXITED,
    SHELL_LIVE,
    HttpxShellGatewayOps,
    ShellSession,
    ShellViewState,
)
from relaydeck.client.features.shell.screen import (
    SHELL_CONFLICT_OWNER_KEY,
    SHELL_INPUT_ROW_KEY,
    SHELL_RECONNECT_KEY,
    SHELL_TAKEOVER_KEY,
    ShellScreen,
)
from relaydeck.client.features.sync.transport import (
    HttpxGatewayTransport,
    WebsocketTerminalSocket,
)
from relaydeck.client.features.terminal.input_modes import (
    KEYBOARD_KEYS,
    KEYBOARD_TEXT,
    WORKFLOW_SHELL,
    ImeState,
    InputModeState,
    route_ime_commit,
)
from relaydeck.client.features.terminal.keys import KEYS_DOCK_KEY
from relaydeck.client.features.terminal.models import TerminalViewState
from relaydeck.client.features.terminal.paste_policy import BRACKET_END, BRACKET_START
from relaydeck.client.features.terminal.paste_review import REQUEST_REVIEW
from relaydeck.client.features.terminal.selection_model import open_selection
from relaydeck.client.features.terminal.stream import INCOMPLETE_HISTORY
from relaydeck.client.features.terminal.surfaces import DRIVE_REFUSED_EXITED
from relaydeck.gateway.app import GatewayState, create_app
from relaydeck.gateway.backend import FakeBackend
from tests.support.host_harness import button_label, render_component, run
from tests.support.live_server import LiveGateway

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "gateway"
ALL_CAPS = frozenset(
    {
        "sessions.read",
        "terminal.view",
        "terminal.control",
        "agents.prompt",
        "agents.start",
        "shells.create",
        "terminal.attach",
        "hosts.enroll",
    }
)


@dataclass
class AdapterConfig:
    """One platform adapter configuration (headless fakes)."""

    name: str
    keyboard: str  # "physical" (web) or "soft" (android)
    clipboard: FakeClipboard = field(default_factory=FakeClipboard)
    ime_hops: int = 0


@dataclass
class ScenarioOutcome:
    create_kind: str
    create_status: str
    output: bytes
    inputs: tuple[tuple[int, int, str, bytes], ...]
    resizes: tuple[tuple[int, int], ...]
    clipboard_writes: tuple[str, ...]
    phases: tuple[str, ...]
    generations: tuple[int, ...]
    attach_takeovers: tuple[bool, ...]
    lease_modes: tuple[str, ...]
    notices: tuple[str, ...]
    statuses: tuple[str, ...]
    attach_shapes: tuple[tuple[str, int], ...]
    exit_refused_drive: bool


async def _wait_for(predicate: Callable[[], bool], timeout_s: float = 30.0) -> None:
    deadline = time.monotonic() + timeout_s
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("timed out waiting for gateway stream")
        await asyncio.sleep(0.01)


async def _cancel(task: asyncio.Task[Any]) -> None:
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception:  # noqa: BLE001 - teardown races TransportError freely
        pass


async def _stop(task: asyncio.Task[Any], session: ShellSession) -> None:
    """Cancel the supervised run task AND close the stream socket (bye):
    a bare cancel leaves the connection open and the server-side viewer
    subscribed, which would stall the viewer gates below."""
    await _cancel(task)
    if session.controller is not None:
        await session.controller.close()


def _seed() -> tuple[GatewayState, FakeBackend]:
    backend = FakeBackend(now_ms=lambda: int(time.time() * 1000))
    snapshot: dict[str, Any] = json.loads((FIXTURES / "snapshot.json").read_text())
    backend.snapshots["home-mac"] = snapshot
    state = GatewayState(
        backend=backend,
        cursor_secret=b"terminal-parity-test-secret-32byte!",
        now_ms=lambda: int(time.time() * 1000),
    )
    return state, backend


async def _type_text(session: ShellSession, config: AdapterConfig, text: str) -> str:
    """Config input path: web commits direct; android commits via IME."""
    if config.keyboard == "soft":
        ime = ImeState()
        ime.update("marked")
        ime.commit(text)
        config.ime_hops += 1
        route = route_ime_commit(
            session.sessions.modes,
            store=None,
            session=session.sessions.keys,
            ime=ime,
        )
        await session.sessions.keys.flush()
        return route
    return await session.type_text(text)


async def _run_shell_scenario(config: AdapterConfig) -> ScenarioOutcome:
    state, backend = _seed()
    phases: list[str] = []
    generations: list[int] = []
    attach_takeovers: list[bool] = []
    lease_modes: list[str] = []
    notices: list[str] = []
    statuses: list[str] = []
    attach_shapes: list[tuple[str, int]] = []

    def viewers(attachment_id: str) -> int:
        fanout = backend.rings.get(attachment_id)
        return fanout.viewer_count if fanout is not None else 0

    with LiveGateway(create_app(state)) as live:
        ws_base = f"ws://127.0.0.1:{live.port}"
        access, _ = state.sessions.create(
            "acct-1", ALL_CAPS, frozenset({"home-mac"}), state.now_ms(), "dev-1"
        )
        other_access, _ = state.sessions.create(
            "acct-1", ALL_CAPS, frozenset({"home-mac"}), state.now_ms(), "dev-2"
        )

        async def token() -> str:
            return access

        async def other_token() -> str:
            return other_access

        async with httpx.AsyncClient(base_url=live.base_url, timeout=10.0) as http:
            transport = HttpxGatewayTransport(base_url=live.base_url, token=token, client=http)
            ops = HttpxShellGatewayOps(base_url=live.base_url, token=token, client=http)
            other_ops = HttpxShellGatewayOps(base_url=live.base_url, token=other_token, client=http)
            # 1. Create: shell.create only, never agent.start.
            record = await transport.submit_command(
                shell_create_command(
                    command_id="cmd-shell-parity",
                    host_id="home-mac",
                    host_epoch="epoch-7",
                    workspace_id="w1",
                    label="parity",
                )
            )
            assert record["kind"] == "shell.create"
            assert record["status"] == "accepted"
            assert backend.commands["cmd-shell-parity"]["kind"] == "shell.create"

            holders: dict[str, ShellSession] = {}

            def factory(ticket: str) -> WebsocketTerminalSocket:
                session = holders["session"]
                assert session.controller is not None
                query = session.controller.resume_query()
                return WebsocketTerminalSocket(
                    f"{ws_base}/v1/terminal/{session.controller.lease_id}?ticket={ticket}&{query}"
                )

            session = ShellSession(
                ops=ops,
                host_id="home-mac",
                # The app always passes the host epoch; closing the pane needs it.
                host_epoch=str(backend.snapshots["home-mac"]["host"]["epoch"]),
                device_id="dev-1",
                socket_factory=factory,
                clipboard=config.clipboard,
            )
            holders["session"] = session

            def snap() -> None:
                """Record the compared UI-adjacent surface at a phase edge:
                notice text, controller status, and attach shape."""
                notices.append(session.notice)
                statuses.append(
                    session.controller.state.status if session.controller is not None else ""
                )
                att = session.attachment
                shapes = (att.attachment_id, att.generation) if att is not None else ("", 0)
                attach_shapes.append(shapes)

            async def publish_synced(data: bytes) -> None:
                """Publish only once the single expected viewer subscribed:
                publishing into the accept/subscribe race would miss the
                fanout (the ring keeps bytes, but live push needs the
                subscription). Disconnect steps gate on zero first so a
                stale viewer can never satisfy the wait."""
                assert session.attachment is not None
                att_id = session.attachment.attachment_id
                epoch = session.attachment.epoch
                await _wait_for(lambda: viewers(att_id) == 1)
                backend.publish_output(att_id, epoch, data)

            # 2. Open: attach without takeover + control lease + stream.
            assert await session.open("term-shell-1") == SHELL_LIVE
            assert session.attachment is not None and session.lease is not None
            attach_takeovers.append(False)
            lease_modes.append(session.lease.mode)
            phases.append(session.phase)
            snap()
            generations.append(session.attachment.generation)
            run_task = asyncio.ensure_future(session.run())
            try:
                await publish_synced(b"$ ")
                await _wait_for(lambda: bytes(session.output_log) == b"$ ")

                # 3. Type through the config input path.
                assert await _type_text(session, config, "ls\n") == "pty"
                lease_id = session.lease.lease_id
                await _wait_for(lambda: len(backend.input_logs.get(lease_id, [])) == 1)
                backend.publish_output(
                    session.attachment.attachment_id, session.attachment.epoch, b"file1\n"
                )
                await _wait_for(lambda: bytes(session.output_log) == b"$ file1\n")

                # 4. Select/copy through the config clipboard.
                session.update_term_state(open_selection(session.term_state))
                text = bytes(session.output_log).decode("utf-8")
                written = await session.copy_selection(text, 0, len(text))
                assert written == text

                # 5. Reviewed paste (bracketed engine mode): one framed chunk.
                session.set_bracketed_paste(True)
                paste_text = "echo a\necho b"
                assert await session.request_paste(paste_text) == REQUEST_REVIEW
                assert session.pending_review is not None
                assert session.pending_review.preview == paste_text
                assert await session.confirm_paste() is True
                await _wait_for(lambda: len(backend.input_logs.get(lease_id, [])) == 2)

                # 6. Resize (control only).
                assert await session.resize(100, 30) is True
                await _wait_for(lambda: len(backend.resize_log) == 1)
                assert (backend.resize_log[0].cols, backend.resize_log[0].rows) == (100, 30)
            finally:
                await _stop(run_task, session)

            # 7. Disconnect keeps the process; reconnect self-takeovers.
            assert await session.disconnect() == SHELL_DISCONNECTED
            phases.append(session.phase)
            snap()
            assert session.attachment is not None
            att_id = session.attachment.attachment_id
            await _wait_for(lambda: viewers(att_id) == 0)
            assert await session.reconnect() == SHELL_LIVE
            attach_takeovers.extend([False, True])
            assert session.attachment is not None and session.lease is not None
            lease_modes.append(session.lease.mode)
            phases.append(session.phase)
            snap()
            generations.append(session.attachment.generation)
            assert session.attachment.generation == 2
            # One supervised run task spans the reconnect stream and the
            # foreign takeover below: cancelling/closing here would leave
            # step 8 listening on a dead controller.
            run_task = asyncio.ensure_future(session.run())
            await publish_synced(b"$ ")
            await _wait_for(lambda: bytes(session.output_log).endswith(b"$ "))

            # 8. Foreign takeover displaces us (reset + close) while we
            # listen; explicit take-over returns with a generation bump.
            foreign = await other_ops.attach("home-mac", "term-shell-1", "shell", True)
            assert foreign.generation == 3
            await run_task
            assert session.phase == SHELL_DISCONNECTED
            assert session.controller is not None
            assert session.controller.reattach_needed is True
            phases.append(session.phase)
            snap()
            assert session.attachment is not None
            att_id = session.attachment.attachment_id
            await _wait_for(lambda: viewers(att_id) == 0)
            assert await session.take_over() == SHELL_LIVE
            attach_takeovers.append(True)
            assert session.attachment is not None and session.lease is not None
            lease_modes.append(session.lease.mode)
            phases.append(session.phase)
            snap()
            generations.append(session.attachment.generation)
            assert session.attachment.generation == 4
            run_task = asyncio.ensure_future(session.run())
            try:
                await publish_synced(b"back\n")
                await _wait_for(lambda: bytes(session.output_log).endswith(b"back\n"))
            finally:
                await _stop(run_task, session)

            inputs: list[tuple[int, int, str, bytes]] = []
            for frames in backend.input_logs.values():
                inputs.extend(frames)
            inputs.sort()
            resizes = tuple((r.cols, r.rows) for r in backend.resize_log)

            # 9. Explicit close is terminal: no resurrect, dead lease unused.
            session.request_close()
            assert session.view().confirm_close is True
            assert await session.close_terminal() == SHELL_EXITED
            phases.append(session.phase)
            snap()
            with patch.object(ops, "terminal_ticket", side_effect=AssertionError("must not mint")):
                refused = await session.drive_recovery() == DRIVE_REFUSED_EXITED
                assert await session.reconnect() == SHELL_EXITED
            assert session.view().phase == SHELL_EXITED

    return ScenarioOutcome(
        create_kind="shell.create",
        create_status="accepted",
        output=bytes(session.output_log),
        inputs=tuple(inputs),
        resizes=resizes,
        clipboard_writes=tuple(config.clipboard.writes),
        phases=tuple(phases),
        generations=tuple(generations),
        attach_takeovers=tuple(attach_takeovers),
        lease_modes=tuple(lease_modes),
        notices=tuple(notices),
        statuses=tuple(statuses),
        attach_shapes=tuple(attach_shapes),
        exit_refused_drive=refused,
    )


def test_shell_scenario_identical_across_adapter_configs() -> None:
    async def scenario() -> None:
        web = AdapterConfig(name="web", keyboard="physical")
        android = AdapterConfig(name="android", keyboard="soft")
        web_out = await _run_shell_scenario(web)
        android_out = await _run_shell_scenario(android)
        # Both configs walked their own input path (pinned divergence 1).
        assert web.ime_hops == 0
        assert android.ime_hops == 1
        assert web.keyboard != android.keyboard
        # ...with identical observable outcomes.
        assert android_out.create_kind == web_out.create_kind == "shell.create"
        assert android_out.create_status == web_out.create_status == "accepted"
        assert android_out.output == web_out.output
        assert android_out.inputs == web_out.inputs
        assert android_out.resizes == web_out.resizes == ((100, 30),)
        assert android_out.clipboard_writes == web_out.clipboard_writes
        assert android_out.phases == web_out.phases
        # 3 is the foreign takeover generation, never owned by this session.
        assert android_out.generations == web_out.generations == (1, 2, 4)
        assert (
            android_out.attach_takeovers
            == web_out.attach_takeovers
            == (
                False,
                False,
                True,
                True,
            )
        )
        assert set(web_out.lease_modes) == {"control"}
        assert android_out.lease_modes == web_out.lease_modes
        assert android_out.notices == web_out.notices
        assert android_out.statuses == web_out.statuses
        assert android_out.attach_shapes == web_out.attach_shapes
        assert web_out.notices == (
            "",
            DISCONNECT_MESSAGE,
            SELF_TAKEOVER_NOTICE,
            INCOMPLETE_HISTORY,
            "",
            EXIT_MESSAGE,
        )
        # Snaps 0/2/4 ride fresh controllers before their first chunk:
        # honestly "connecting" (only apply_live() on the first
        # established chunk may claim "live").
        # Snap 1 follows an explicit Disconnect, which projects "disconnected" at once.
        assert web_out.statuses == (
            "connecting",
            "disconnected",
            "connecting",
            "reset",
            "connecting",
            "live",
        )
        assert web_out.attach_shapes == (
            ("att-1", 1),
            ("att-1", 1),
            ("att-1", 2),
            ("att-1", 2),
            ("att-1", 4),
            ("att-1", 4),
        )
        assert android_out.exit_refused_drive is True
        assert web_out.exit_refused_drive is True
        # Exact pinned bytes: prompt, echo, paste framing, resize.
        assert web_out.output == b"$ file1\n$ back\n"
        assert web_out.inputs == (
            (1, 1, "text", b"ls\n"),
            (1, 2, "text", BRACKET_START + b"echo a\necho b" + BRACKET_END),
            (1, 3, "resize", struct.pack(">HH", 100, 30)),
        )
        assert web_out.clipboard_writes == ("$ file1\n",)
        assert web_out.phases == (
            SHELL_LIVE,
            SHELL_DISCONNECTED,
            SHELL_LIVE,
            SHELL_DISCONNECTED,
            SHELL_LIVE,
            SHELL_EXITED,
        )

    run(scenario())


def _shell_render_kwargs() -> dict[str, Any]:
    return {
        "selection_text": "",
        "review": None,
        "paste_error": None,
        "armed": frozenset(),
        "keys_enabled": True,
        "on_jump_to_bottom": lambda: None,
        "on_size_report": lambda c, r: None,
        "on_input": lambda o, d: None,
        "on_open_selection": lambda: None,
        "on_copy_selection": lambda a, f: None,
        "on_close_selection": lambda: None,
        "on_paste": lambda: None,
        "on_mode_change": lambda m: None,
        "on_key": lambda name: None,
        "on_toggle_modifier": lambda m: None,
        "on_dismiss_keys": lambda: None,
        "on_confirm_paste": lambda: None,
        "on_cancel_paste": lambda: None,
        "on_disconnect": lambda: None,
        "on_reconnect": lambda: None,
        "on_takeover": lambda: None,
        "on_back": lambda: None,
        "on_close_terminal": lambda: None,
        "on_confirm_close": lambda: None,
        "on_cancel_close": lambda: None,
        "on_new_terminal": lambda: None,
    }


def test_shell_ui_keyboard_plumbing_diverges_but_state_matches() -> None:
    """Config-driven UI parity: the keyboard plumbing follows the adapter
    (web/physical renders Text mode with no Keys dock, android/soft
    renders Keys mode with the dock), while every other rendered shell
    state is identical across configs."""

    async def scenario() -> None:
        snapshots: dict[str, dict[str, Any]] = {}
        for config in (
            AdapterConfig(name="web", keyboard="physical"),
            AdapterConfig(name="android", keyboard="soft"),
        ):
            modes = InputModeState(workflow=WORKFLOW_SHELL)
            modes.set_keyboard(KEYBOARD_KEYS if config.keyboard == "soft" else KEYBOARD_TEXT)
            live = ShellViewState(
                phase=SHELL_LIVE, terminal_id="term-shell-1", keyboard=modes.keyboard
            )
            app = await render_component(
                ShellScreen,
                view=live,
                terminal=TerminalViewState(lease_mode="control"),
                **_shell_render_kwargs(),
            )
            try:
                app.find(KEYS_DOCK_KEY)
                dock = True
            except KeyError:
                dock = False
            snapshots[config.name] = {
                "keyboard": modes.keyboard,
                "store": modes.store,
                "input_row": app.find(SHELL_INPUT_ROW_KEY) is not None,
                "surface": app.find("terminal-surface") is not None,
                "dock": dock,
            }

            conflict = ShellViewState(
                phase=SHELL_CONFLICT, terminal_id="term-shell-1", owner_label="dev-2"
            )
            app2 = await render_component(
                ShellScreen,
                view=conflict,
                terminal=TerminalViewState(),
                **_shell_render_kwargs(),
            )
            snapshots[config.name]["conflict_owner"] = app2.find(SHELL_CONFLICT_OWNER_KEY).value
            snapshots[config.name]["takeover"] = button_label(app2.find(SHELL_TAKEOVER_KEY))
            try:
                app2.find(SHELL_RECONNECT_KEY)
                reconnect_absent = False
            except KeyError:
                reconnect_absent = True
            snapshots[config.name]["reconnect_absent"] = reconnect_absent

        # Pinned divergence: the dock follows the adapter keyboard, and
        # shell keyboard choice stays draftless on both configs.
        assert snapshots["web"]["keyboard"] == KEYBOARD_TEXT
        assert snapshots["android"]["keyboard"] == KEYBOARD_KEYS
        assert snapshots["web"]["dock"] is False
        assert snapshots["android"]["dock"] is True
        assert snapshots["web"]["store"] is None
        assert snapshots["android"]["store"] is None
        # Every other rendered shell state is identical.
        for key in ("input_row", "surface", "conflict_owner", "takeover", "reconnect_absent"):
            assert snapshots["web"][key] == snapshots["android"][key], key
        assert snapshots["web"]["input_row"] is True
        assert snapshots["web"]["surface"] is True
        assert snapshots["web"]["conflict_owner"] == "Terminal owned by dev-2"
        assert snapshots["web"]["takeover"] == "Take over"
        assert snapshots["web"]["reconnect_absent"] is True

    run(scenario())


def test_ownership_conflict_discloses_owner_end_to_end() -> None:
    """Disclosure end to end against the REAL gateway: a second live
    session's attach hits `lease_conflict` and the disclosed owner label
    flows to the conflict state (no takeover, no stream opened)."""

    async def scenario() -> None:
        state, _ = _seed()
        with LiveGateway(create_app(state)) as live:
            ws_base = f"ws://127.0.0.1:{live.port}"
            access1, _ = state.sessions.create(
                "acct-1", ALL_CAPS, frozenset({"home-mac"}), state.now_ms(), "dev-1"
            )
            access2, _ = state.sessions.create(
                "acct-1", ALL_CAPS, frozenset({"home-mac"}), state.now_ms(), "dev-2"
            )

            async def token1() -> str:
                return access1

            async def token2() -> str:
                return access2

            async with httpx.AsyncClient(base_url=live.base_url, timeout=10.0) as http:
                ops1 = HttpxShellGatewayOps(base_url=live.base_url, token=token1, client=http)
                ops2 = HttpxShellGatewayOps(base_url=live.base_url, token=token2, client=http)
                box: dict[str, ShellSession] = {}

                def factory(ticket: str) -> WebsocketTerminalSocket:
                    first = box["first"]
                    assert first.controller is not None
                    query = first.controller.resume_query()
                    return WebsocketTerminalSocket(
                        f"{ws_base}/v1/terminal/{first.controller.lease_id}?ticket={ticket}&{query}"
                    )

                first = ShellSession(
                    ops=ops1,
                    host_id="home-mac",
                    device_id="dev-1",
                    socket_factory=factory,
                    clipboard=FakeClipboard(),
                )
                box["first"] = first
                assert await first.open("term-shell-9") == SHELL_LIVE
                assert first.attachment is not None
                assert first.attachment.generation == 1

                def no_stream(ticket: str) -> WebsocketTerminalSocket:
                    raise AssertionError("conflict must precede streaming")

                second = ShellSession(
                    ops=ops2,
                    host_id="home-mac",
                    device_id="dev-2",
                    socket_factory=no_stream,
                    clipboard=FakeClipboard(),
                )
                assert await second.open("term-shell-9") == SHELL_CONFLICT
                # The gateway discloses the owner device as the label.
                assert second.owner_label == "dev-1"
                assert second.owner_label == first.attachment.owner_display
                assert second.controller is None
                assert second.lease is None
                # No takeover happened: the first holder is undisplaced.
                assert first.attachment.generation == 1
                assert first.phase == SHELL_LIVE
                if first.controller is not None:
                    await first.controller.close()

    run(scenario())
