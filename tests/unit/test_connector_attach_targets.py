"""Attach targets must use the ids herdr 0.9.1 resolves.

`herdr agent attach <TARGET>` resolves pane ids (`w2:p5`); a terminal id
answers agent_not_found. `herdr terminal attach <TERMINAL_ID>` needs the
pane's `term_...` id; the inventory names shells by tab id.
"""

from __future__ import annotations

from tests.support.terminal.attach_targets import _routing_source, _source


def test_agent_attach_uses_pane_id() -> None:
    source = _source()
    assert source.agent_attach_target("term_agent5") == "w2:p5"
    assert source.agent_attach_target("term_unknown") == "term_unknown"


def test_shell_attach_uses_pane_terminal_id() -> None:
    source = _source()
    assert source.shell_attach_target("w1:t2") == "term_shell2"
    assert source.shell_attach_target("w1:p2") == "term_shell2"
    assert source.shell_attach_target("term_shell2") == "term_shell2"
    # Multi-pane tab: the focused pane wins.
    assert source.shell_attach_target("w1:t4") == "term_split_b"
    assert source.shell_attach_target("w9:t9") == "w9:t9"


def test_displaced_agent_is_never_retaken_without_a_new_epoch() -> None:
    """Another client (or a Herdr window) took the pane over: the attach died under
    the same epoch. RelayDeck must not run `agent attach --takeover` again
    until the user explicitly takes over (new epoch)."""
    source, attaches = _routing_source()
    route = {"term_agent5": {"epoch": "e1", "kind": "agent"}}
    assert source.note_epochs(route) == {}
    assert attaches == ["term_agent5"]
    for _ in range(5):  # repeated epoch notes, as the gateway sends them
        failures = source.note_epochs(route)
        assert "Take over" in failures["term_agent5"]
    assert attaches == ["term_agent5"]  # no silent re-takeover loop
    # Explicit Take over = new attachment epoch -> one fresh attach.
    assert source.note_epochs({"term_agent5": {"epoch": "e2", "kind": "agent"}}) == {}
    assert attaches == ["term_agent5", "term_agent5"]


def test_agent_prompt_targets_pane_id() -> None:
    import asyncio

    source = _source()
    sent: list[str] = []

    class Adapter:
        async def agent_prompt(self, target: str, text: str) -> dict[str, str]:
            sent.append(target)
            return {"type": "agent_prompted"}

    source._adapter = Adapter()
    asyncio.run(source.agent_prompt("term_agent5", "hello"))
    assert sent == ["w2:p5"]
