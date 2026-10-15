"""Dead session and routing source."""

from __future__ import annotations

from relaydeck.connector.herdr import HerdrAdapter
from relaydeck.connector.source import HerdrSource


def _source() -> HerdrSource:
    source = HerdrSource(HerdrAdapter(socket_path="/private/tmp/relaydeck-unused.sock"))
    source._agents = [
        {"terminal_id": "term_agent5", "pane_id": "w2:p5", "tab_id": "w2:t5"},
    ]
    source._snapshot = {
        "panes": [
            {"pane_id": "w1:p2", "tab_id": "w1:t2", "terminal_id": "term_shell2"},
            {"pane_id": "w1:p4", "tab_id": "w1:t4", "terminal_id": "term_split_a"},
            {
                "pane_id": "w1:p5",
                "tab_id": "w1:t4",
                "terminal_id": "term_split_b",
                "focused": True,
            },
        ]
    }
    return source


class _DeadSession:
    running = False
    exit_status = 1

    def read(self, timeout: float = 0.0) -> bytes:
        return b""


class _Pool:
    def close(self, key: str) -> None:
        pass


def _routing_source() -> tuple[HerdrSource, list[str]]:
    source = _source()
    source._epochs, source._epoch_kinds, source._agent_lost = {}, {}, {}
    source._attached, source._pool = {}, _Pool()
    attaches: list[str] = []

    def attach_agent(target: str, **_: int) -> None:
        attaches.append(target)
        source._attached[f"agent:{target}"] = (_DeadSession(), target, "")

    source.attach_agent = attach_agent  # type: ignore[method-assign]
    return source, attaches
