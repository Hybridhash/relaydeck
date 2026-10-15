"""Terminal open timing: one content-free line per open, marked once per step."""

import asyncio
import logging
import re

import pytest

from relaydeck.client import open_timing
from relaydeck.client.features.terminal.diagnostics import diagnostics_logger
from relaydeck.client.open_timing import COUNTS, STEPS, OpenTiming


class _Lines(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        message = record.getMessage()
        if message.startswith("open_timing "):
            self.lines.append(message)


@pytest.fixture
def lines() -> list[str]:
    handler = _Lines()
    logger = diagnostics_logger()
    logger.addHandler(handler)
    yield handler.lines
    logger.removeHandler(handler)


class _Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


class _Term:
    def __init__(self, acked: int = 0, frames: int = 0) -> None:
        self._acked_bytes = acked
        self._view_report_count = frames


def _fields(line: str) -> dict[str, str]:
    return dict(part.split("=", 1) for part in line.split()[1:])


def test_a_cold_open_is_reported_once_painted_and_input_ready(lines: list[str]) -> None:
    clock = _Clock()
    timing = OpenTiming("agent", clock)
    clock.now += 0.040
    timing.mark("attach")
    clock.now += 0.010
    timing.mark("attach")  # a second attach keeps the first time
    term = _Term()
    timing.poll(term)
    clock.now += 0.200
    term._acked_bytes = 512
    timing.poll(term)
    clock.now += 0.050
    timing.mark("input_ready")
    assert lines == []  # applied and input-ready, but no frame drawn yet
    clock.now += 0.030
    term._view_report_count = 1
    timing.poll(term)
    assert len(lines) == 1
    fields = _fields(lines[0])
    assert fields["kind"] == "agent" and fields["path"] == "cold" and fields["outcome"] == "ready"
    assert fields["attach"] == "40" and fields["first_ack"] == "250"
    assert fields["input_ready"] == "300" and fields["painted"] == "330"
    assert fields["socket"] == "-"
    timing.mark("ticket")
    assert len(lines) == 1  # nothing after the report


class _Controller:
    def __init__(self, fenced: bool = False, eligible: bool = True) -> None:
        self.ui_input_fenced = fenced
        self.writer_eligible = eligible


class _Session:
    def __init__(self, controller: _Controller | None, phase: str = "live") -> None:
        self.controller = controller
        self.phase = phase


def test_a_cold_open_with_the_engine_input_gate_off_still_completes_ready(
    lines: list[str],
) -> None:
    # The composer workflow keeps the engine's input gate off; readiness is the writer's.
    clock = _Clock()
    timing = OpenTiming("agent", clock)
    term = _Term()
    term._values = {"input_enabled": False}
    session = _Session(_Controller())
    timing.poll(term, session)
    term._acked_bytes = 64
    clock.now += 0.1
    timing.poll(term, session)
    term._view_report_count = 1
    timing.poll(term, session)
    fields = _fields(lines[0])
    assert fields["outcome"] == "ready"
    assert fields["input_ready"] != "-" and fields["first_ack"] != "-" and fields["painted"] != "-"


@pytest.mark.parametrize(
    "session",
    [
        _Session(_Controller(fenced=True)),
        _Session(_Controller(eligible=False)),
        _Session(None),
        _Session(_Controller(), phase="connecting"),
    ],
)
def test_a_fenced_or_ineligible_writer_is_not_input_ready(
    session: _Session, lines: list[str]
) -> None:
    timing = OpenTiming("agent", _Clock())
    timing.poll(_Term(), session)
    timing.finish("superseded")
    assert _fields(lines[0])["input_ready"] == "-"


def test_a_resumed_terminal_ignores_earlier_acknowledgements(lines: list[str]) -> None:
    timing = OpenTiming("shell", _Clock())
    timing.path = "warm"
    timing.poll(_Term(acked=9000))
    timing.mark("input_ready")
    fields = _fields(lines[0])
    assert fields["first_ack"] == "-" and fields["outcome"] == "ready"


def test_a_new_open_reports_the_previous_one_as_superseded(lines: list[str]) -> None:
    first = open_timing.begin("agent")
    second = open_timing.begin("shell")
    assert first.done and not second.done
    assert _fields(lines[0])["outcome"] == "superseded"
    second.finish("failed")


def test_an_open_that_never_completes_times_out(lines: list[str]) -> None:
    clock = _Clock()
    timing = OpenTiming("agent", clock)
    clock.now += open_timing.TIMEOUT_S + 1
    timing.poll(None)
    assert _fields(lines[0])["outcome"] == "timeout"


def test_counts_and_unknown_steps(lines: list[str]) -> None:
    timing = OpenTiming("shell", _Clock())
    timing.count("size_reports")
    timing.count("size_reports")
    timing.count("resizes")
    timing.mark("not_a_step")
    timing.count("not_a_count")
    timing.finish("ready")
    fields = _fields(lines[0])
    assert fields["size_reports"] == "2" and fields["resizes"] == "1"
    assert "not_a_step" not in fields and "not_a_count" not in fields


def test_the_line_has_a_fixed_content_free_schema(lines: list[str]) -> None:
    OpenTiming("weird kind with spaces", _Clock()).finish("arbitrary text")
    fields = _fields(lines[0])
    assert fields["kind"] == "other" and fields["outcome"] == "other"
    assert set(fields) == {"kind", "path", "cache", "outcome", "total_ms", *STEPS, *COUNTS}
    assert all(re.fullmatch(r"[a-z_]+|-?\d+|-", value) for value in fields.values())


def test_tasks_started_by_the_open_report_to_it(lines: list[str]) -> None:
    timing = OpenTiming("agent", _Clock())

    async def opening() -> None:
        open_timing.bind(timing)

        async def stream() -> None:
            open_timing.mark_current("socket")

        await asyncio.create_task(stream())

    asyncio.run(opening())
    open_timing.mark_current("ticket")  # outside the open: no record
    timing.finish("ready")
    fields = _fields(lines[0])
    assert fields["socket"] != "-" and fields["ticket"] == "-"


def test_steps_before_the_control_attach_count_as_the_view_connection(lines: list[str]) -> None:
    clock = _Clock()
    timing = OpenTiming("agent", clock)
    clock.now += 0.010
    timing.mark("view_lease")
    timing.mark("ticket")
    timing.mark("socket")
    clock.now += 0.100
    timing.mark("attach")
    clock.now += 0.020
    timing.mark("ticket")
    timing.mark("socket")
    timing.finish("ready")
    fields = _fields(lines[0])
    assert fields["view_ticket"] == "10" and fields["view_socket"] == "10"
    assert fields["attach"] == "110" and fields["ticket"] == "130" and fields["socket"] == "130"
