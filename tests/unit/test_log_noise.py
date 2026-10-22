"""Routine log noise: a closing tab is one INFO line, other failures keep their traceback."""

from __future__ import annotations

import logging

from starlette.websockets import WebSocketDisconnect
from websockets.exceptions import ConnectionClosedOK

from relaydeck.gateway.ws_close import PeerClosedFilter


def _emit(exc: BaseException) -> logging.LogRecord:
    try:
        raise exc
    except BaseException as caught:
        import sys

        record = logging.LogRecord(
            "uvicorn.error",
            logging.ERROR,
            __file__,
            1,
            "Exception in ASGI application\n",
            None,
            sys.exc_info(),
        )
        del caught
    PeerClosedFilter().filter(record)
    return record


def test_a_closing_tab_logs_one_info_line_without_a_traceback() -> None:
    for exc in (WebSocketDisconnect(1006), ConnectionClosedOK(None, None)):
        record = _emit(exc)
        assert record.levelno == logging.INFO
        assert record.getMessage() == "flet_ws_closed code=1006"
        assert record.exc_info is None


def test_other_failures_keep_their_traceback() -> None:
    record = _emit(RuntimeError("boom"))
    assert record.levelno == logging.ERROR and record.exc_info is not None
    assert record.getMessage().startswith("Exception in ASGI application")


def test_the_filter_does_not_swallow_the_exception() -> None:
    seen: list[logging.LogRecord] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            seen.append(record)

    logger = logging.getLogger("uvicorn.error")
    handler, peer_filter, level = Capture(), PeerClosedFilter(), logger.level
    logger.addHandler(handler)
    logger.addFilter(peer_filter)
    logger.setLevel(logging.INFO)
    try:
        try:
            raise WebSocketDisconnect(1001)
        except WebSocketDisconnect:
            logger.exception("Exception in ASGI application")
    finally:
        logger.removeHandler(handler)
        logger.removeFilter(peer_filter)
        logger.setLevel(level)
    assert [r.getMessage() for r in seen] == ["flet_ws_closed code=1001"]
