"""Input modes, routing, dedupe, IME, and the
input_mode reset rule.

Covers all six input axes (Composer/Direct/Shell x text/keys): mode
switches preserve the hidden composer draft (multiline + active IME
composition) byte-identically, never autosent or silently cleared; normal
typing edits only the draft in Composer mode and goes to the PTY in Direct
mode; tools-keys target the live terminal even when a draft exists;
desktop keydown/text events never double-send; IME finishes before
submission; target changes drop queued input and clear modifiers so no
previous terminal receives a delayed key; cancelled gestures emit nothing.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

from relaydeck.client.features.composer.domain import (
    INPUT_MODE_COMPOSER,
    INPUT_MODE_TOOLS_KEYS,
    SEND_DELIVERED_VIA_ATTACH,
    ComposerStore,
)
from relaydeck.client.features.terminal.input_modes import (
    AXIS_LABELS,
    IME_COMPOSING,
    IME_IDLE,
    KEY_DROP,
    KEY_HOLD,
    KEY_SEND_KEY,
    KEYBOARD_KEYS,
    KEYBOARD_TEXT,
    ROUTE_DRAFT,
    ROUTE_DROPPED,
    ROUTE_NONE,
    ROUTE_PTY,
    TEXT_DROP,
    TEXT_SEND,
    TEXT_SUPPRESSED,
    WORKFLOW_AGENT_COMPOSER,
    WORKFLOW_AGENT_DIRECT,
    WORKFLOW_SHELL,
    ImeState,
    InputModeState,
    KeyEventDeduper,
    route_committed_text,
    route_ime_commit,
    submit_allowed,
)
from relaydeck.client.features.terminal.keys import (
    DOCK_TITLE,
    KEYS_DISMISS_KEY,
    KEYS_DOCK_KEY,
    KEYS_TITLE_KEY,
    MOD_ALT,
    MOD_CTRL,
    MOD_SHIFT,
    KeyInputSession,
    ToolsKeysDock,
    key_button_key,
    modifier_button_key,
)
from tests.support.host_harness import render_component, run

DOMAIN = (
    Path(__file__).resolve().parent.parent.parent
    / "src"
    / "relaydeck"
    / "client"
    / "features"
    / "composer"
    / "domain.py"
)

DRAFT = "unsent draft\nsecond line \u00e9\u4e2d\u6587 \t end"


class _Writer:
    def __init__(self, generation: int | None = 3) -> None:
        self.generation = generation
        self.sent: list[bytes] = []

    def probe(self) -> int | None:
        return self.generation

    async def send(self, data: bytes) -> None:
        self.sent.append(data)

    def session(self, target_id: str = "term-a") -> KeyInputSession:
        return KeyInputSession(target_id=target_id, generation_probe=self.probe, sender=self.send)


def _store(**kwargs: Any) -> ComposerStore:
    async def submit(command: dict[str, Any]) -> dict[str, Any]:
        return {"command_id": command["command_id"]}

    async def prompt(target: str, text: str) -> dict[str, Any]:
        return {"ok": True}

    return ComposerStore(
        target="sess:1",
        submit_command=submit,
        prompt_text=prompt,
        host_id="host-1",
        host_epoch="epoch-1",
        agent_id="agent-1",
        **kwargs,
    )


# -- axes ------------------------------------------------------------------


def test_six_axes_have_labels_and_defaults() -> None:
    assert len(AXIS_LABELS) == 6
    state = InputModeState()
    assert (state.workflow, state.keyboard) == (WORKFLOW_AGENT_COMPOSER, KEYBOARD_TEXT)
    assert state.axis == "Agent Composer with text keyboard"
    assert InputModeState(workflow=WORKFLOW_AGENT_DIRECT, keyboard=KEYBOARD_KEYS).axis == (
        "Agent Direct Input with terminal tools"
    )
    assert InputModeState(workflow=WORKFLOW_SHELL, keyboard=KEYBOARD_TEXT).axis == "Shell Text"
    assert InputModeState(workflow=WORKFLOW_SHELL, keyboard=KEYBOARD_KEYS).axis == "Shell Keys"


def test_unknown_workflow_or_keyboard_rejected() -> None:
    state = InputModeState()
    for bad in ("composer", "agent-tools", "", "shell-terminal"):
        try:
            state.set_workflow(bad)
        except ValueError:
            pass
        else:
            raise AssertionError(bad)
    for bad in ("tools-keys", "tools", ""):
        try:
            state.set_keyboard(bad)
        except ValueError:
            pass
        else:
            raise AssertionError(bad)


def test_mode_switches_preserve_hidden_draft() -> None:
    store = _store()
    store.replace_draft(DRAFT)
    state = InputModeState(store=store)
    writer = _Writer()
    session = writer.session()
    for workflow in (WORKFLOW_AGENT_DIRECT, WORKFLOW_SHELL, WORKFLOW_AGENT_COMPOSER):
        state.set_workflow(workflow)
        for keyboard in (KEYBOARD_KEYS, KEYBOARD_TEXT):
            state.set_keyboard(keyboard)
            assert store.draft == DRAFT
            assert store.draft.encode("utf-8") == DRAFT.encode("utf-8")
    assert writer.sent == []
    assert session.outbox == ()


def test_mode_switches_preserve_active_ime_composition() -> None:
    store = _store()
    store.replace_draft(DRAFT)
    state = InputModeState(store=store)
    ime = ImeState()
    ime.begin()
    ime.update("\u65e5\u672c\u8a9e")
    writer = _Writer()
    session = writer.session()
    for workflow in (WORKFLOW_AGENT_DIRECT, WORKFLOW_SHELL, WORKFLOW_AGENT_COMPOSER):
        state.set_workflow(workflow)
        state.set_keyboard(KEYBOARD_KEYS)
        state.set_keyboard(KEYBOARD_TEXT)
        assert ime.is_composing
        assert ime.state == IME_COMPOSING
        assert ime.marked_text == "\u65e5\u672c\u8a9e"
        assert not ime.has_committed
        assert store.draft == DRAFT
    assert writer.sent == []
    assert session.outbox == ()


def test_mode_switches_preserve_unrouted_ime_commit() -> None:
    state = InputModeState()
    ime = ImeState()
    ime.update("x")
    ime.commit("\u00e9")
    state.set_workflow(WORKFLOW_AGENT_DIRECT)
    state.set_keyboard(KEYBOARD_KEYS)
    assert ime.take_committed() == "\u00e9"
    assert ime.take_committed() is None


# -- routing: draft vs PTY -------------------------------------------------------------


def test_composer_typing_edits_only_the_draft() -> None:
    async def scenario() -> None:
        store = _store()
        store.replace_draft("hello")
        state = InputModeState(store=store)
        writer = _Writer()
        session = writer.session()
        assert (
            route_committed_text(state, store=store, session=session, text=" world") == ROUTE_DRAFT
        )
        assert store.draft == "hello world"
        assert session.outbox == ()
        assert await session.flush() == 0
        assert writer.sent == []

    run(scenario())


def test_direct_and_shell_typing_go_to_the_pty() -> None:
    async def scenario() -> None:
        store = _store()
        store.replace_draft(DRAFT)
        writer = _Writer()
        session = writer.session()
        direct = InputModeState(workflow=WORKFLOW_AGENT_DIRECT, store=store)
        assert route_committed_text(direct, store=store, session=session, text="ls\n") == ROUTE_PTY
        shell = InputModeState(workflow=WORKFLOW_SHELL)
        assert route_committed_text(shell, store=None, session=session, text="pwd") == ROUTE_PTY
        assert store.draft == DRAFT
        assert await session.flush() == 2
        assert writer.sent == [b"ls\n", b"pwd"]

    run(scenario())


def test_routing_drops_without_writer_or_store() -> None:
    store = _store()
    direct = InputModeState(workflow=WORKFLOW_AGENT_DIRECT, store=store)
    dead = _Writer(generation=None)
    assert (
        route_committed_text(direct, store=store, session=dead.session(), text="x") == ROUTE_DROPPED
    )
    composer = InputModeState(store=store)
    assert (
        route_committed_text(composer, store=None, session=dead.session(), text="x")
        == ROUTE_DROPPED
    )
    writer = _Writer()
    assert (
        route_committed_text(direct, store=store, session=writer.session(), text="")
        == ROUTE_DROPPED
    )


def test_composer_empty_text_drops_like_direct() -> None:
    store = _store()
    store.replace_draft("hi")
    composer = InputModeState(store=store)
    writer = _Writer()
    assert (
        route_committed_text(composer, store=store, session=writer.session(), text="")
        == ROUTE_DROPPED
    )
    assert store.draft == "hi"


def test_tools_keys_target_terminal_with_draft_present() -> None:
    async def scenario() -> None:
        store = _store()
        store.replace_draft(DRAFT)
        state = InputModeState(store=store, keyboard=KEYBOARD_KEYS)
        assert state.axis == "Agent Composer with terminal tools"
        writer = _Writer()
        session = writer.session()
        emission = session.press_key("escape")
        assert emission is not None
        assert store.draft == DRAFT
        assert await session.flush() == 1
        assert writer.sent == [b"\x1b"]
        assert store.draft == DRAFT

    run(scenario())


# -- desktop keydown/text dedupe ----------------------------------------------------------


def test_printable_keydown_holds_and_text_sends_once() -> None:
    async def scenario() -> None:
        deduper = KeyEventDeduper()
        writer = _Writer()
        session = writer.session()
        assert deduper.key_down("k1", printable=True) == KEY_HOLD
        assert writer.sent == []
        assert deduper.text(b"g") == TEXT_SEND
        assert session.commit_text("g") is not None
        assert await session.flush() == 1
        assert writer.sent == [b"g"]

    run(scenario())


def test_keydown_then_cancel_emits_nothing() -> None:
    async def scenario() -> None:
        deduper = KeyEventDeduper()
        writer = _Writer()
        session = writer.session()
        assert deduper.key_down("k1", printable=True) == KEY_HOLD
        deduper.cancel()
        assert not deduper.has_hold
        # A late text for the cancelled press still flows once (fresh text),
        # but the cancelled gesture itself emitted nothing.
        assert session.outbox == ()
        assert await session.flush() == 0
        assert writer.sent == []

    run(scenario())


def test_consumed_keydown_suppresses_its_echo() -> None:
    async def scenario() -> None:
        deduper = KeyEventDeduper()
        writer = _Writer()
        session = writer.session()
        session.toggle_modifier(MOD_CTRL)
        assert deduper.key_down("k1", printable=True) == KEY_HOLD
        assert session.press_key("c") is not None
        deduper.consumed_as_key(echo=b"c")
        assert deduper.text(b"c") == TEXT_SUPPRESSED
        assert await session.flush() == 1
        assert writer.sent == [b"\x03"]  # no duplicated "c"

    run(scenario())


def test_nonmatching_text_clears_suppression_and_flows() -> None:
    deduper = KeyEventDeduper()
    assert deduper.key_down("k1", printable=True) == KEY_HOLD
    deduper.consumed_as_key(echo=b"c")
    assert deduper.text(b"x") == TEXT_SEND
    assert not deduper.has_echo_suppression
    assert deduper.text(b"c") == TEXT_SEND


def test_double_consumed_key_overwrites_echo_and_both_flow() -> None:
    """Pinned single-slot limitation: a second `consumed_as_key` overwrites
    the armed echo, so both echoes flow as fresh text (echo-overwrite can
    double-flow)."""
    deduper = KeyEventDeduper()
    assert deduper.key_down("k1", printable=True) == KEY_HOLD
    deduper.consumed_as_key(echo=b"c")
    deduper.consumed_as_key(echo=b"d")  # overwrites the "c" suppression
    assert deduper.text(b"c") == TEXT_SEND  # no longer matches: flows
    assert not deduper.has_echo_suppression
    assert deduper.text(b"d") == TEXT_SEND  # suppression cleared: flows too


def test_enter_keydown_suppresses_lf_echo_only() -> None:
    deduper = KeyEventDeduper()
    assert deduper.key_down("enter", printable=False) == KEY_SEND_KEY
    assert deduper.has_echo_suppression
    assert deduper.text(b"\n") == TEXT_SUPPRESSED
    assert deduper.key_down("enter", printable=False) == KEY_SEND_KEY
    assert deduper.text(b"\r") == TEXT_SUPPRESSED
    assert deduper.key_down("f1", printable=False) == KEY_SEND_KEY
    assert not deduper.has_echo_suppression
    assert deduper.text(b"q") == TEXT_SEND


def test_deduper_stale_token_and_reset_drop() -> None:
    deduper = KeyEventDeduper()
    token = deduper.token
    assert deduper.key_down("k1", printable=True, token=token) == KEY_HOLD
    deduper.reset()
    assert deduper.key_down("k2", printable=True, token=token) == KEY_DROP
    assert deduper.text(b"x", token=token) == TEXT_DROP
    assert not deduper.has_hold


# -- IME ---------------------------------------------------------------------------------


def test_ime_updates_send_nothing_and_gate_submission() -> None:
    ime = ImeState()
    assert ime.state == IME_IDLE
    assert submit_allowed(ime)
    ime.begin()
    ime.update("a")
    ime.update("ab")
    assert ime.is_composing
    assert ime.marked_text == "ab"
    assert not submit_allowed(ime)
    assert not ime.has_committed


def test_ime_commit_routes_once_per_mode() -> None:
    async def scenario() -> None:
        store = _store()
        store.replace_draft(">")
        writer = _Writer()
        session = writer.session()
        ime = ImeState()
        ime.update("x")
        ime.commit("\u00e9")
        assert submit_allowed(ime)
        composer = InputModeState(store=store)
        assert route_ime_commit(composer, store=store, session=session, ime=ime) == ROUTE_DRAFT
        assert store.draft == ">\u00e9"
        assert session.outbox == ()
        ime.commit("ls")
        direct = InputModeState(workflow=WORKFLOW_AGENT_DIRECT, store=store)
        assert route_ime_commit(direct, store=store, session=session, ime=ime) == ROUTE_PTY
        assert store.draft == ">\u00e9"
        assert await session.flush() == 1
        assert writer.sent == [b"ls"]
        assert route_ime_commit(direct, store=store, session=session, ime=ime) == ROUTE_NONE

    run(scenario())


def test_ime_cancel_routes_nothing() -> None:
    async def scenario() -> None:
        store = _store()
        writer = _Writer()
        session = writer.session()
        ime = ImeState()
        ime.update("ab")
        ime.cancel()
        assert submit_allowed(ime)
        direct = InputModeState(workflow=WORKFLOW_AGENT_DIRECT, store=store)
        assert route_ime_commit(direct, store=store, session=session, ime=ime) == ROUTE_NONE
        assert session.outbox == ()
        assert await session.flush() == 0
        assert writer.sent == []

    run(scenario())


# -- no delayed key ---------------------------------------------------------------------------


def test_target_switch_mid_queue_delivers_nothing_stale() -> None:
    async def scenario() -> None:
        first = _Writer()
        session = first.session()
        session.toggle_modifier(MOD_CTRL)
        assert session.press_key("c") is not None
        assert session.commit_text("rm -rf /") is not None
        assert len(session.outbox) == 2
        second = _Writer(generation=9)
        session.retarget("term-b", second.probe, second.send)
        assert await session.flush() == 0
        assert first.sent == []
        assert second.sent == []
        # A generation bump on the same target also invalidates the queue.
        assert session.press_key("a") is not None
        second.generation = 10
        assert await session.flush() == 0
        assert second.sent == []

    run(scenario())


def test_deduper_and_ime_reset_on_target_change() -> None:
    deduper = KeyEventDeduper()
    ime = ImeState()
    token = deduper.token
    assert deduper.key_down("k1", printable=True, token=token) == KEY_HOLD
    ime.update("ab")
    deduper.reset()
    ime.reset()
    assert deduper.key_down("k1", printable=True, token=token) == KEY_DROP
    assert not ime.is_composing
    assert ime.take_committed() is None


# -- input_mode reset rule -----------------------------------------------------------------------


async def _deliver_blocked(store: ComposerStore, writer: _Writer) -> None:
    store.bind_writer(writer.probe, writer.send)
    store.replace_draft("run it")
    assert await store.send() == SEND_DELIVERED_VIA_ATTACH
    assert store.input_mode == INPUT_MODE_TOOLS_KEYS


def test_leaving_tools_keys_resets_to_composer() -> None:
    async def scenario() -> None:
        store = _store(initial_status="blocked")
        writer = _Writer()
        await _deliver_blocked(store, writer)
        state = InputModeState(store=store, keyboard=KEYBOARD_KEYS)
        assert state.set_keyboard(KEYBOARD_TEXT) is True
        assert store.input_mode == INPUT_MODE_COMPOSER
        assert state.set_keyboard(KEYBOARD_TEXT) is False

    run(scenario())


def test_entering_tools_and_switching_workflows_never_touch_the_store() -> None:
    async def scenario() -> None:
        store = _store(initial_status="blocked")
        writer = _Writer()
        await _deliver_blocked(store, writer)
        state = InputModeState(store=store)
        assert state.set_keyboard(KEYBOARD_KEYS) is False
        assert store.input_mode == INPUT_MODE_TOOLS_KEYS
        state.set_workflow(WORKFLOW_AGENT_DIRECT)
        state.set_workflow(WORKFLOW_SHELL)
        state.set_workflow(WORKFLOW_AGENT_COMPOSER)
        assert store.input_mode == INPUT_MODE_TOOLS_KEYS
        fresh = _store()
        plain = InputModeState(store=fresh)
        plain.set_keyboard(KEYBOARD_KEYS)
        plain.set_workflow(WORKFLOW_AGENT_DIRECT)
        assert fresh.input_mode == INPUT_MODE_COMPOSER
        assert plain.set_keyboard(KEYBOARD_TEXT) is False

    run(scenario())


def test_sync_from_store_honors_blocked_handoff() -> None:
    async def scenario() -> None:
        store = _store(initial_status="blocked")
        writer = _Writer()
        await _deliver_blocked(store, writer)
        state = InputModeState(store=store)
        assert state.sync_from_store() is True
        assert state.keyboard == KEYBOARD_KEYS
        assert state.sync_from_store() is False
        fresh = _store()
        plain = InputModeState(store=fresh)
        assert plain.sync_from_store() is False
        shell = InputModeState(workflow=WORKFLOW_SHELL)
        assert shell.sync_from_store() is False

    run(scenario())


def test_store_resets_only_through_leave_tools_keys() -> None:
    """AST proof: `INPUT_MODE_COMPOSER` is assigned only in `__init__` and
    `leave_tools_keys`; no send/status/reconnect path resets the mode."""
    tree = ast.parse(DOMAIN.read_text())
    writes: dict[str, list[str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "ComposerStore":
            for item in node.body:
                if not isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef):
                    continue
                for child in ast.walk(item):
                    if not isinstance(child, ast.Assign):
                        continue
                    for target in child.targets:
                        if isinstance(target, ast.Attribute) and target.attr == "_input_mode":
                            value = child.value
                            name = value.id if isinstance(value, ast.Name) else "?"
                            writes.setdefault(item.name, []).append(name)
    assert writes == {
        "__init__": ["INPUT_MODE_COMPOSER"],
        "_deliver_through_attach": ["INPUT_MODE_TOOLS_KEYS"],
        "confirm_blocked_insert": ["INPUT_MODE_TOOLS_KEYS"],
        "leave_tools_keys": ["INPUT_MODE_COMPOSER"],
    }


# -- declarative dock (mount-ready; mounts) ----------------------


def test_dock_renders_keys_modifiers_and_dismiss() -> None:
    async def scenario() -> None:
        keys: list[str] = []
        mods: list[str] = []
        dismissed: list[bool] = []
        app = await render_component(
            ToolsKeysDock,
            armed=frozenset({MOD_CTRL}),
            enabled=True,
            on_key=keys.append,
            on_toggle_modifier=mods.append,
            on_dismiss=lambda: dismissed.append(True),
        )
        assert app.find(KEYS_DOCK_KEY) is not None
        assert app.find(KEYS_TITLE_KEY).value == DOCK_TITLE
        assert app.find(key_button_key("escape")).content == "Esc"
        assert app.find(key_button_key("f12")).content == "F12"
        assert app.find(key_button_key("left")).content == "\u2190"
        assert app.find(key_button_key("shift-enter")).content == "Shift Enter"
        assert app.find(modifier_button_key(MOD_CTRL)).content == "\u25cf Ctrl"
        assert app.find(modifier_button_key(MOD_ALT)).content == "Alt"
        assert app.find(modifier_button_key(MOD_SHIFT)).content == "Shift"
        await app.set_props_and_fire(key_button_key("escape"), {}, "click")
        await app.set_props_and_fire(modifier_button_key(MOD_ALT), {}, "click")
        await app.set_props_and_fire(KEYS_DISMISS_KEY, {}, "click")
        assert keys == ["escape"]
        assert mods == [MOD_ALT]
        assert dismissed == [True]

    run(scenario())


def test_dock_buttons_expose_accessibility_tooltips() -> None:
    async def scenario() -> None:
        app = await render_component(
            ToolsKeysDock,
            armed=frozenset(),
            enabled=True,
            on_key=lambda name: None,
            on_toggle_modifier=lambda mod: None,
            on_dismiss=lambda: None,
        )
        assert app.find(key_button_key("left")).tooltip == "Left Arrow"
        assert app.find(key_button_key("delete")).tooltip == "Forward Delete"
        assert app.find(key_button_key("shift-tab")).tooltip == "Shift Tab"
        assert app.find(modifier_button_key(MOD_CTRL)).tooltip == "Control modifier"
        assert app.find(modifier_button_key(MOD_ALT)).tooltip == "Option modifier"
        assert app.find(modifier_button_key(MOD_SHIFT)).tooltip == "Shift modifier"

    run(scenario())


def test_dock_disabled_without_live_writer() -> None:
    async def scenario() -> None:
        app = await render_component(
            ToolsKeysDock,
            armed=frozenset(),
            enabled=False,
            on_key=lambda name: None,
            on_toggle_modifier=lambda mod: None,
            on_dismiss=lambda: None,
        )
        assert app.find(key_button_key("enter")).disabled is True
        assert app.find(modifier_button_key(MOD_CTRL)).disabled is True
        # Dismiss always works: leaving tools must always be possible.
        assert app.find(KEYS_DISMISS_KEY).disabled is False

    run(scenario())
