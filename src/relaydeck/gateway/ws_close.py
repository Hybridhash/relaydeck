"""Log a browser tab closing under the Flet websocket as one INFO line, not a traceback.

The exception still propagates (reconnect handling relies on it); only its log record changes."""

from __future__ import annotations

import logging

_UVICORN_MESSAGE = "Exception in ASGI application"
_CLOSED = frozenset({"WebSocketDisconnect", "ClientDisconnected"})


def _closed_code(exc: BaseException | None) -> tuple[bool, object]:
    """Whether `exc` itself is a peer close (or a group of only those), and its close code."""
    if exc is None:
        return False, None
    names = {cls.__name__ for cls in type(exc).__mro__}
    if names & _CLOSED or "ConnectionClosed" in names:
        return True, getattr(exc, "code", None)
    members = getattr(exc, "exceptions", ())
    if members and all(_closed_code(member)[0] for member in members):
        return True, _closed_code(members[0])[1]
    return False, None


class PeerClosedFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if record.exc_info is None or not str(record.msg).startswith(_UVICORN_MESSAGE):
            return True
        closed, code = _closed_code(record.exc_info[1])
        if closed:
            record.levelno, record.levelname = logging.INFO, "INFO"
            record.msg, record.args = f"flet_ws_closed code={code}", None
            record.exc_info = record.exc_text = None
        return True


def install() -> None:
    logger = logging.getLogger("uvicorn.error")
    if not any(isinstance(item, PeerClosedFilter) for item in logger.filters):
        logger.addFilter(PeerClosedFilter())
