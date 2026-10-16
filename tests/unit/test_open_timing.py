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
        self._frames = frames
        self._listeners = []

    @property
    def _view_report_count(self):
        return self._frames

    @_view_report_count.setter
    def _view_report_count(self, frames):
        self._frames = frames
        for listener in self._listeners:
            listener(None)

    def add_view_state_listener(self, listener):
        self._listeners.append(listener)


def _fields(line: str) -> dict[str, str]:
    return dict(part.split("=", 1) for part in line.split()[1:])


def test_a_cold_open_is_reported_once_painted_and_input_ready(lines: list[str]) -> None:
    clock = _Clock()
    timing = OpenTiming("agent", clock)
    timing.mark("hold_armed")
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
    timing.mark("unfenced")
    timing.mark("hold_released")
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
    timing.mark("hold_released")
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
    term = _Term(acked=9000)
    session = _Session(_Controller())
    timing.poll(term, session)
    assert lines == []
    term._view_report_count = 1
    timing.poll(term, session)
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
    fields = _fields(lines[0])
    assert fields["outcome"] == "timeout"
    assert all(fields[step] == "-" for step in STEPS)
    timing.finish("timeout")
    assert len(lines) == 1


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
    timing = OpenTiming("weird kind with spaces", _Clock())
    timing.path = "synthetic bearer/grant/cookie/path"
    timing.mark("synthetic output/draft")
    timing.finish("arbitrary text")
    fields = _fields(lines[0])
    assert fields["kind"] == "other" and fields["outcome"] == "other"
    assert fields["path"] == "cold" and "synthetic" not in lines[0]
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


def test_cold_timeline_waits_for_settle_and_emits_all_elapsed_marks(lines: list[str]) -> None:
    clock = _Clock()
    timing = OpenTiming("agent", clock)
    session = _Session(_Controller())
    term = _Term()
    events = (
        "hold_armed",
        "acquired",
        "published",
        "attach",
        "lease",
        "ticket",
        "socket",
        "first_byte",
        "probe_start",
        "probe_sent",
        "verified",
        "probe_verified",
        "first_resize",
    )
    for index, step in enumerate(events, 1):
        clock.now = 100 + index / 100
        timing.mark(step)
    timing.poll(term, session)
    clock.now = 100.2
    term._acked_bytes = 64
    timing.poll(term, session)
    clock.now = 100.3
    term._view_report_count = 1
    timing.poll(term, session)
    assert lines == []
    clock.now = 100.6
    timing.mark("hold_released")
    timing.poll(term, session)
    fields = _fields(lines[0])
    assert [fields[step] for step in events] == [str(index * 10) for index in range(1, 14)]
    assert fields["first_output"] == fields["first_ack"] == "200"
    assert fields["painted"] == "300"
    assert fields["hold_released"] == fields["fully_ready"] == fields["total_ms"] == "600"
    assert fields["outcome"] == "ready"
    assert list(fields) == ["kind", "path", "cache", "outcome", "total_ms", *STEPS, *COUNTS]
    timing.poll(term, session)
    timing.finish("timeout")
    assert len(lines) == 1


@pytest.mark.parametrize("blocker", ["hold", "fence", "writer", "phase", "renderer"])
def test_fully_ready_requires_current_readiness(blocker: str, lines: list[str]) -> None:
    clock = _Clock()
    timing = OpenTiming("agent", clock)
    controller = _Controller()
    session = _Session(controller)
    term = _Term()
    timing.mark("hold_armed")
    timing.poll(term, session)
    term._acked_bytes, term._view_report_count = 64, 1
    timing.poll(term, session)
    timing.mark("hold_released")
    if blocker == "hold":
        timing.mark("hold_armed")
    elif blocker == "fence":
        controller.ui_input_fenced = True
    elif blocker == "writer":
        controller.writer_eligible = False
    elif blocker == "phase":
        session.phase = "connecting"
    clock.now += 0.4
    timing.poll(None if blocker == "renderer" else term, session)
    assert lines == []
    clock.now += 0.2
    timing.mark("hold_released")
    controller.ui_input_fenced = False
    controller.writer_eligible = True
    session.phase = "live"
    if blocker == "renderer":
        timing.observe(term)
        term._view_report_count += 1
    timing.poll(term, session)
    assert _fields(lines[0])["fully_ready"] == "600"


def test_timeout_identifies_the_missing_gate_without_losing_reached_marks(lines: list[str]) -> None:
    clock = _Clock()
    timing = OpenTiming("agent", clock)
    timing.mark("hold_armed")
    timing.mark("probe_sent")
    term = _Term()
    session = _Session(_Controller(fenced=True))
    timing.poll(term, session)
    term._acked_bytes = 64
    term._view_report_count = 1
    timing.poll(term, session)
    clock.now += open_timing.TIMEOUT_S + 1
    timing.poll(term, session)
    fields = _fields(lines[0])
    assert fields["outcome"] == "timeout" and fields["probe_sent"] == "0"
    assert fields["first_output"] == fields["painted"] == "0"
    for step in ("probe_verified", "unfenced", "input_ready", "hold_released", "fully_ready"):
        assert fields[step] == "-"


def test_explicit_session_marks_do_not_follow_an_unrelated_task_context(lines: list[str]) -> None:
    from types import SimpleNamespace

    async def scenario() -> None:
        stale = OpenTiming("agent", _Clock())
        current = OpenTiming("agent", _Clock())
        open_timing.bind(stale)
        session = SimpleNamespace(open_timing=current)
        open_timing.mark_current("hold_armed", session=session)
        current.finish("superseded")
        assert _fields(lines[-1])["hold_armed"] == "0"
        stale.finish("superseded")
        assert _fields(lines[-1])["hold_armed"] == "-"

    asyncio.run(scenario())


def _native_pair():
    from types import SimpleNamespace

    from relaydeck_terminal import RelayDeckTerminal

    term = RelayDeckTerminal()
    term._test_mark_ready([])
    session = SimpleNamespace(controller=_Controller(), phase="live", current_term=term)
    return term, session


def _native_ack(term, count=1):
    from relaydeck_terminal import protocol

    term.write(b"x" * (count - term._sent_bytes))
    term._test_inject_dart_message(protocol.encode_ack(count))


def _native_frame(term):
    from relaydeck_terminal import protocol

    term._test_inject_dart_message(
        protocol.encode_view_report(protocol.ViewState(False, True, False))
    )


def test_frame_before_ack_cannot_complete_ready(lines):
    term, session = _native_pair()
    timing = OpenTiming("agent", _Clock())
    timing.mark("hold_released")
    timing.poll(term, session)
    _native_frame(term)
    timing.poll(term, session)
    _native_ack(term)
    timing.poll(term, session)
    assert not timing.done
    _native_frame(term)
    timing.poll(term, session)
    assert _fields(lines[-1])["outcome"] == "ready"


@pytest.mark.parametrize("change", ["replace", "reset", "reopen"])
def test_current_renderer_must_reestablish_paint(change, lines):
    term, session = _native_pair()
    timing = OpenTiming("agent", _Clock())
    timing.mark("hold_armed")
    timing.poll(term, session)
    _native_ack(term)
    timing.poll(term, session)
    _native_frame(term)
    timing.poll(term, session)
    assert "painted" in timing._marks and not timing.done
    if change == "replace":
        term, session = _native_pair()
    elif change == "reset":
        term.reset()
    else:
        term._viewer_generation += 1
    timing.mark("hold_released")
    timing.poll(term, session)
    assert not timing.done
    if change != "reopen":
        _native_ack(term)
    _native_frame(term)
    timing.poll(term, session)
    assert _fields(lines[-1])["outcome"] == "ready"


def test_fast_quiet_warm_frame_before_first_tick_completes(lines):
    term, session = _native_pair()
    _native_ack(term)
    _native_frame(term)
    timing = OpenTiming("agent", _Clock())
    timing.path = "warm"
    # The open boundary precedes preconnect or a request for another frame.
    timing.observe(term)
    timing.mark("hold_released")
    _native_frame(term)
    timing.poll(term, session)
    assert timing.done
    assert _fields(lines[-1])["first_ack"] == "-"


def test_ack_and_frame_before_first_tick_keep_event_order(lines):
    term, session = _native_pair()
    timing = OpenTiming("agent", _Clock())
    timing.observe(term)
    timing.mark("hold_released")
    _native_frame(term)  # A mount report is not paint of subsequently applied output.
    _native_ack(term)
    timing.poll(term, session)
    assert not timing.done
    _native_frame(term)
    timing.poll(term, session)
    assert timing.done


def test_later_ack_needs_a_new_frame_even_if_first_paint_was_recorded(lines):
    term, session = _native_pair()
    timing = OpenTiming("agent", _Clock())
    timing.observe(term)
    timing.mark("hold_armed")
    _native_ack(term)
    _native_frame(term)
    timing.poll(term, session)
    assert "painted" in timing._marks
    _native_ack(term, 2)
    timing.mark("hold_released")
    timing.poll(term, session)
    assert not timing.done
    _native_frame(term)
    timing.poll(term, session)
    assert timing.done


def test_replaced_renderers_late_frame_is_ignored(lines):
    old, session = _native_pair()
    timing = OpenTiming("agent", _Clock())
    timing.observe(old)
    _native_ack(old)
    _native_frame(old)
    replacement, session = _native_pair()
    timing.observe(replacement)
    timing.mark("hold_released")
    _native_frame(old)
    timing.poll(replacement, session)
    assert not timing.done
    _native_ack(replacement)
    _native_frame(replacement)
    timing.poll(replacement, session)
    assert timing.done


def test_repeated_warm_opens_share_one_weak_frame_listener(lines):
    import gc
    import weakref

    term, session = _native_pair()
    _native_ack(term)
    for _ in range(3):
        timing = OpenTiming("agent", _Clock())
        timing.path = "warm"
        timing.observe(term)
        timing.mark("hold_released")
        _native_frame(term)
        timing.poll(term, session)
        assert timing.done
    assert len(term._view_listeners) == 1
    record = weakref.ref(timing)
    del timing
    gc.collect()
    assert record() is None
    _native_frame(term)
    assert len(lines) == 3
